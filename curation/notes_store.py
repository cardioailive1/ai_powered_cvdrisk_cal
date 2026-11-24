"""Clinical notes kept on disk and read a few patients at a time.

On Mayo Clinic Platform data/raw/event_notes.parquet holds ~13.5 million notes (31 GB on disk), far more than the
workspace machine can hold in memory. From it this module builds, once, a copy split by patient:

    data/raw/event_notes_by_person/bucket=NNN.parquet   (NOTE_BUCKETS files; patient p -> bucket p % NOTE_BUCKETS)

each sorted by person_id and note_date, zstd-compressed, in small row groups. Labelling reads one bucket at a
time; the workbench reads one patient's notes, which touches only the row groups holding that patient. The copy
is rebuilt automatically when event_notes.parquet changes (size or modification time), and never leaves data/raw.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pandas as pd

log = logging.getLogger("cardioai.notes")
NOTE_BUCKETS = 64
ROW_GROUP_ROWS = 2_000
STORE = "event_notes_by_person"
MANIFEST = "MANIFEST.json"
SOURCE = "event_notes.parquet"


def _source(raw: Path) -> Path:
    return Path(raw) / SOURCE


def _stamp(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "buckets": NOTE_BUCKETS}


def _store(raw: Path) -> Path:
    return Path(raw) / STORE


def is_current(raw: Path) -> bool:
    m = _store(raw) / MANIFEST
    try:
        return json.loads(m.read_text())["source"] == _stamp(_source(raw))
    except (OSError, ValueError, KeyError):
        return False


def note_count(raw: Path) -> int:
    import pyarrow.parquet as pq
    return int(pq.ParquetFile(_source(raw)).metadata.num_rows)


def ensure(raw: Path) -> Path:
    """Build the per-patient copy if it is missing or older than event_notes.parquet. Returns its folder."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    raw = Path(raw)
    out = _store(raw)
    if is_current(raw):
        return out
    src = pq.ParquetFile(_source(raw))
    total = src.metadata.num_rows
    log.info("Splitting %s (%d notes) by patient into %s/ (once; rebuilt only if the notes change)", SOURCE, total, STORE)
    tmp = raw / (STORE + ".building")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    schema = src.schema_arrow
    writers: dict[int, pq.ParquetWriter] = {}
    done = 0
    try:     # pass 1: append each batch's rows to its patient bucket
        for batch in src.iter_batches(batch_size=100_000):
            t = pa.Table.from_batches([batch]).cast(schema)
            buckets = t["person_id"].to_numpy() % NOTE_BUCKETS
            for k in pd.unique(buckets):
                part = t.filter(pa.array(buckets == k))
                w = writers.get(int(k))
                if w is None:
                    w = writers[int(k)] = pq.ParquetWriter(tmp / f"unsorted-{int(k):03d}.parquet", schema, compression="zstd")
                w.write_table(part)
            done += len(t)
            log.info("  split %d / %d notes", done, total)
    finally:
        for w in writers.values():
            w.close()
    for k in range(NOTE_BUCKETS):     # pass 2: sort each bucket by patient and date, small row groups
        u = tmp / f"unsorted-{k:03d}.parquet"
        t = pq.read_table(u) if u.exists() else schema.empty_table()
        t = t.sort_by([("person_id", "ascending"), ("note_date", "ascending")])
        pq.write_table(t, tmp / f"bucket={k:03d}.parquet", row_group_size=ROW_GROUP_ROWS, compression="zstd")
        u.unlink(missing_ok=True)
        log.info("  sorted bucket %d / %d", k + 1, NOTE_BUCKETS)
    rows = sum(pq.ParquetFile(f).metadata.num_rows for f in tmp.glob("bucket=*.parquet"))
    if rows != total:
        raise RuntimeError(f"Splitting notes lost rows ({rows} of {total}); {tmp} kept for inspection")
    (tmp / MANIFEST).write_text(json.dumps({"source": _stamp(_source(raw)), "rows": rows}, indent=2))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return out


def bucket_of(person_id: int) -> int:
    return int(person_id) % NOTE_BUCKETS


def read_bucket(raw: Path, k: int) -> pd.DataFrame:
    import pyarrow.parquet as pq
    return pq.read_table(ensure(raw) / f"bucket={k:03d}.parquet").to_pandas()


def notes_for(raw: Path, person_ids) -> pd.DataFrame:
    """All notes of these patients (reads only the row groups holding them)."""
    import pyarrow.parquet as pq
    store = ensure(raw)
    ids = sorted({int(p) for p in person_ids})
    parts = []
    for k in sorted({bucket_of(p) for p in ids}):
        mine = [p for p in ids if bucket_of(p) == k]
        parts.append(pq.read_table(store / f"bucket={k:03d}.parquet", filters=[("person_id", "in", mine)]).to_pandas())
    if not parts:
        return pq.read_schema(store / "bucket=000.parquet").empty_table().to_pandas()
    return pd.concat(parts, ignore_index=True)
