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
    """Build the per-patient copy if it is missing or older than event_notes.parquet. Returns its folder.

    Resumable: if the run stops (session ended, out of memory), running it again keeps the work already done.
    Pass 1 splits the source one row group at a time (a marker per finished row group); pass 2 sorts one bucket
    at a time (a marker per finished bucket). Memory: one batch of notes, not a whole row group."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    raw = Path(raw)
    out = _store(raw)
    if is_current(raw):
        return out
    src = pq.ParquetFile(_source(raw), buffer_size=1 << 23)      # stream pages instead of whole column chunks
    total, n_rg = src.metadata.num_rows, src.metadata.num_row_groups
    schema = src.schema_arrow
    tmp = raw / (STORE + ".building")
    stamp = json.dumps(_stamp(_source(raw)), sort_keys=True)
    if not (tmp / "SOURCE.json").exists() or (tmp / "SOURCE.json").read_text() != stamp:
        shutil.rmtree(tmp, ignore_errors=True)       # different notes (or an older layout): start over
        (tmp / "split").mkdir(parents=True)
        (tmp / "SOURCE.json").write_text(stamp)
        log.info("Splitting %s (%d notes) by patient into %s/ (once; rebuilt only if the notes change)",
                 SOURCE, total, STORE)
    else:
        log.info("Resuming the split of %s by patient", SOURCE)
    split = tmp / "split"

    for i in range(n_rg):            # pass 1: each source row group -> one file per bucket
        if (split / f"rg{i:05d}.done").exists():
            continue
        for f in split.glob(f"b*-rg{i:05d}.parquet"):      # partial output of an interrupted row group
            f.unlink()
        writers: dict[int, pq.ParquetWriter] = {}
        try:
            for batch in src.iter_batches(batch_size=20_000, row_groups=[i]):
                buckets = batch.column(batch.schema.get_field_index("person_id")).to_numpy() % NOTE_BUCKETS
                for k in pd.unique(buckets):
                    k = int(k)
                    if k not in writers:
                        writers[k] = pq.ParquetWriter(split / f"b{k:03d}-rg{i:05d}.parquet", schema, compression="zstd")
                    writers[k].write_table(pa.Table.from_batches([batch.filter(buckets == k)]).cast(schema))
        finally:
            for w in writers.values():
                w.close()
        (split / f"rg{i:05d}.done").touch()
        log.info("  split part %d / %d (%d notes in all)", i + 1, n_rg, total)

    for k in range(NOTE_BUCKETS):     # pass 2: sort each bucket by patient and date, small row groups
        if (tmp / f"bucket={k:03d}.done").exists():
            continue
        files = sorted(split.glob(f"b{k:03d}-rg*.parquet"))
        t = pa_concat([pq.read_table(f) for f in files], schema)
        t = t.sort_by([("person_id", "ascending"), ("note_date", "ascending")])
        pq.write_table(t, tmp / f"bucket={k:03d}.parquet", row_group_size=ROW_GROUP_ROWS, compression="zstd")
        (tmp / f"bucket={k:03d}.done").touch()
        for f in files:
            f.unlink()
        log.info("  sorted bucket %d / %d", k + 1, NOTE_BUCKETS)

    rows = sum(pq.ParquetFile(f).metadata.num_rows for f in tmp.glob("bucket=*.parquet"))
    if rows != total:
        raise RuntimeError(f"Splitting notes lost rows ({rows} of {total}); {tmp} kept for inspection")
    shutil.rmtree(split, ignore_errors=True)
    for f in tmp.glob("*.done"):
        f.unlink()
    (tmp / "SOURCE.json").unlink()
    (tmp / MANIFEST).write_text(json.dumps({"source": _stamp(_source(raw)), "rows": rows}, indent=2))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return out


def pa_concat(tables, schema):
    import pyarrow as pa
    return pa.concat_tables([t.cast(schema) for t in tables]) if tables else schema.empty_table()


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
