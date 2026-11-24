"""Label runs (auto-labelling), audit runs, and the per-run data cache that builds
blinded adjudication packets."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import secrets
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from cardioai_core.schema import FAMILIES
from cardioai_core.security import sha256_file
from training import audit as audit_mod
from training import labeling
from training.imaging_nlp import extract_cac, extract_lvef
from training.nlp import highlight_segments

from . import governance, notes_store
from .store import AuditReport, LabelRun, active_run, log_event

RAW_FILES = ["features", "candidate_codes", "evidence_procedures", "evidence_labs", "event_notes", "baseline_imaging_notes"]
NOTE_FILES = ("event_notes", "baseline_imaging_notes")   # too large to load whole on MCP: read in parts (notes_store)
DISPLAY_WINDOW = {"ami": (-2, 7), "stroke": (-2, 7), "hf": (-3, 14), "scd": (-1, 7), "af": (-30, 30)}


def raw_dir(data_dir) -> Path:
    return Path(data_dir) / "raw"


_SHA_CACHE = ".sha256.json"


def _file_sha(path: Path) -> str:
    """sha256 of a raw file, remembered next to it by size and modification time (event_notes is ~31 GB on MCP:
    hashing it on every check would take minutes)."""
    st = path.stat()
    key = f"{path.name}:{st.st_size}:{st.st_mtime_ns}"
    cache_path = path.parent / _SHA_CACHE
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    if key not in cache:
        cache = {k: v for k, v in cache.items() if not k.startswith(path.name + ":")}
        cache[key] = sha256_file(path)
        try:
            cache_path.write_text(json.dumps(cache, indent=1))
        except OSError:
            pass
    return cache[key]


def inputs_sha(data_dir) -> str:
    h = hashlib.sha256()
    for name in RAW_FILES:
        h.update(f"{name}:{_file_sha(raw_dir(data_dir) / f'{name}.parquet')}".encode())
    return h.hexdigest()


def load_raw(data_dir, include_notes: bool = False) -> dict[str, pd.DataFrame]:
    """The raw tables. The two note tables are left out unless include_notes (small data only): use
    notes_store / baseline_annotation, which read them in parts."""
    missing = [n for n in RAW_FILES if not (raw_dir(data_dir) / f"{n}.parquet").exists()]
    if missing:
        raise FileNotFoundError(f"Missing raw files in {raw_dir(data_dir)}: {', '.join(missing)} — run training.extract first")
    return {n: pd.read_parquet(raw_dir(data_dir) / f"{n}.parquet") for n in RAW_FILES
            if include_notes or n not in NOTE_FILES}


def baseline_annotation(data_dir) -> pd.DataFrame:
    """Per baseline imaging report: note_id, whether it mentions LVEF / CAC, and whether a value was extracted.
    Read in batches (4 GB of report text on MCP); saved next to the raw files and redone only if they change."""
    import pyarrow.parquet as pq
    from training.imaging_nlp import imaging_keywords
    src = raw_dir(data_dir) / "baseline_imaging_notes.parquet"
    out = raw_dir(data_dir) / ".baseline_annotation.parquet"
    stamp = f"{_file_sha(src)}"
    if out.exists():
        old = pq.ParquetFile(out).schema_arrow.metadata or {}
        if old.get(b"source_sha", b"").decode() == stamp:
            return pd.read_parquet(out)
    parts = []
    for b in pq.ParquetFile(src).iter_batches(batch_size=50_000, columns=["note_id", "note_text"]):
        df = b.to_pandas()
        kw_ef, kw_cac = imaging_keywords(df["note_text"])
        parts.append(pd.DataFrame({"note_id": df["note_id"].astype(str), "kw_ef": kw_ef, "kw_cac": kw_cac,
                                   "lvef": df["note_text"].map(extract_lvef).astype(float),
                                   "cac": df["note_text"].map(extract_cac).astype(float)}))
    cols = {"note_id": pd.Series(dtype=str), "kw_ef": pd.Series(dtype=bool), "kw_cac": pd.Series(dtype=bool),
            "lvef": pd.Series(dtype=float), "cac": pd.Series(dtype=float)}
    res = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(cols)
    import pyarrow as pa
    t = pa.Table.from_pandas(res, preserve_index=False)
    t = t.replace_schema_metadata({**(t.schema.metadata or {}), b"source_sha": stamp.encode()})
    pq.write_table(t, out)
    return res


def save_annotation_notes(db: Session, data_dir, run: LabelRun) -> None:
    """Keep the text of the reports sampled for annotation review with the label run, so the workbench never
    loads every baseline report (4 GB on MCP)."""
    from .store import Case
    ids = list(db.scalars(select(Case.ref_id).where(Case.label_run_id == run.id, Case.case_type == "annotation")))
    if ids:
        baseline_notes_by_id(data_dir, ids).to_parquet(Path(run.path) / "annotation_notes.parquet", index=False)


def baseline_notes_by_id(data_dir, note_ids) -> pd.DataFrame:
    """Full text of these baseline imaging reports (one scan of the file, keeping only these rows)."""
    import pyarrow.parquet as pq
    ids = sorted({str(n) for n in note_ids})
    t = pq.read_table(raw_dir(data_dir) / "baseline_imaging_notes.parquet", filters=[("note_id", "in", ids)])
    df = t.to_pandas()
    df["note_id"] = df["note_id"].astype(str)
    return df


def is_synthetic(data_dir) -> bool:
    return (raw_dir(data_dir) / "SYNTHETIC").exists()


def log_progress(msg: str) -> None:
    import logging
    logging.getLogger("cardioai.curation").info(msg)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


# ------------------------------------------------------------------ label run
class SequenceError(RuntimeError):
    """A step was attempted out of order (e.g. a review before the automatic audit)."""


AUDIT_REQUIRED = ("The automatic audit is a required step after automatic labelling and before any data scientist "
                  "or cardiologist review.")


def create_label_run(db: Session, data_dir, username: str, label_model_path: str | None = None) -> LabelRun:
    """Automatic labelling, followed immediately by the required automatic audit (one step; it cannot be skipped)."""
    gov = governance.load(data_dir)
    model = json.loads(Path(label_model_path).read_text()) if label_model_path else json.loads(json.dumps(labeling.DEFAULT_LABEL_MODEL))
    model["thresholds"] = gov["label_thresholds"]
    raw = load_raw(data_dir)
    cands = labeling.build_episodes(raw["candidate_codes"], raw["evidence_procedures"], raw["features"])
    ev = labeling.Evidence(raw["evidence_labs"], raw["evidence_procedures"], None,
                           dict(zip(raw["features"]["person_id"], raw["features"]["sex"])))
    # notes are read one patient bucket at a time (13.5 M notes on MCP); every candidate is labelled exactly once,
    # with all of its patient's notes, so the result is the same as labelling everything at once
    notes_store.ensure(raw_dir(data_dir))
    bucket = cands["person_id"].astype("int64") % notes_store.NOTE_BUCKETS
    parts = []
    for k in range(notes_store.NOTE_BUCKETS):
        mine = cands[bucket == k]
        if len(mine):
            parts.append(labeling.label_candidates(mine, ev.with_notes(notes_store.read_bucket(raw_dir(data_dir), k)),
                                                   raw["features"], model).set_index(mine.index))
        if (k + 1) % 8 == 0:
            log_progress(f"labelled patient bucket {k + 1} / {notes_store.NOTE_BUCKETS}")
    if parts:
        labelled = pd.concat(parts).loc[cands.index].reset_index(drop=True)
        lf_cols = [c for c in labelled.columns if c.startswith("lf_")]     # a bucket may lack a family's votes
        labelled[lf_cols] = labelled[lf_cols].fillna(0).astype("int8")
    else:
        labelled = labeling.label_candidates(cands, ev, raw["features"], model)
    labels = labeling.mark_relevance(labelled, gov["definitions"])

    run_id = _new_id("LR")
    out = Path(data_dir) / "label_runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    labels.to_parquet(out / "labels.parquet", index=False)
    (out / "label_model.json").write_text(json.dumps(model, indent=2))
    stats = labeling.lf_stats(labels)
    (out / "lf_stats.json").write_text(json.dumps(stats, indent=2))

    for old in db.scalars(select(LabelRun).where(LabelRun.status == "active")):
        old.status = "superseded"
        log_event(db, username, "label_run.superseded", old.id, {"by": run_id})
    run = LabelRun(id=run_id, created_by=username, label_model_version=model.get("version", "custom"),
                   label_model_sha=labeling.model_sha(model), inputs_sha=inputs_sha(data_dir),
                   governance_sha=governance.sha(gov), path=str(out),
                   summary={"n_patients": int(len(raw["features"])), "n_candidates": int(len(labels)),
                            "families": {f: {"auto": s["auto"], "n": s["n"], "source": s["source"]} for f, s in stats.items()},
                            "relevant": {f: int(g["label_relevant"].sum()) for f, g in labels.groupby("family")},
                            "relevant_auto": {f: {k: int(v) for k, v in g.loc[g["label_relevant"], "auto_label"].value_counts().items()}
                                              for f, g in labels.groupby("family")},
                            "synthetic": is_synthetic(data_dir), "provenance": gov["provenance"]})
    db.add(run)
    log_event(db, username, "label_run.created", run_id,
              {"inputs_sha": run.inputs_sha, "label_model": run.label_model_version, "label_model_sha": run.label_model_sha,
               "governance_sha": run.governance_sha, "n_candidates": len(labels)})
    db.commit()
    RunData.cache_clear()
    run_audit(db, data_dir, run, username)   # required step: every label run is audited before anyone reviews it
    return run


def run_audit(db: Session, data_dir, run: LabelRun, username: str) -> AuditReport:
    if inputs_sha(data_dir) != run.inputs_sha:
        raise SequenceError(f"The extracted data changed after label run {run.id} was created, so its labels no longer match "
                            "the data. Run automatic labelling again (python -m curation.pipeline label); it audits the new run.")
    gov = governance.load(data_dir)
    raw = load_raw(data_dir)
    labels = pd.read_parquet(Path(run.path) / "labels.parquet")
    counts = {k: int(len(raw[k])) for k in ["evidence_labs", "evidence_procedures"]}
    counts["event_notes"] = notes_store.note_count(raw_dir(data_dir))
    rep = audit_mod.run_audit(raw["features"], labels, counts, baseline_annotation(data_dir), gov)
    rep["label_run_id"] = run.id
    rep["inputs_sha"] = run.inputs_sha
    blob = json.dumps(rep, sort_keys=True, default=str)
    rid = _new_id("AU")
    ar = AuditReport(id=rid, label_run_id=run.id, overall=rep["overall"], sha=hashlib.sha256(blob.encode()).hexdigest(),
                     report=json.loads(blob))
    db.add(ar)
    (Path(run.path) / "audit.json").write_text(json.dumps(rep, indent=2, default=str))
    (Path(run.path) / "AUDIT_REPORT.md").write_text(audit_mod.to_markdown(rep, f"Automatic audit — {run.id}"))
    log_event(db, username, "audit.completed", rid, {"label_run": run.id, "overall": rep["overall"], "counts": rep["counts"], "sha": ar.sha})
    db.commit()
    return ar


def latest_audit(db: Session, run_id: str) -> AuditReport | None:
    return db.scalar(select(AuditReport).where(AuditReport.label_run_id == run_id).order_by(AuditReport.created_at.desc()).limit(1))


def audit_problem(db: Session, run: LabelRun, data_dir=None) -> str | None:
    """Why this label run may not be reviewed yet (None = the required automatic audit is done and current).
    With data_dir, also checks that the extracted data has not changed since labelling (hashes every raw file)."""
    ar = latest_audit(db, run.id)
    if ar is None:
        return f"{AUDIT_REQUIRED} Label run {run.id} has no audit: run python -m curation.pipeline audit."
    if ar.report.get("inputs_sha", run.inputs_sha) != run.inputs_sha:
        return f"{AUDIT_REQUIRED} The audit {ar.id} was run on different data from label run {run.id}: run automatic labelling again."
    if data_dir is not None and inputs_sha(data_dir) != run.inputs_sha:
        return (f"{AUDIT_REQUIRED} The extracted data changed after label run {run.id} was labelled and audited: "
                "run automatic labelling again (it audits the new run).")
    return None


def require_audit(db: Session, run: LabelRun, data_dir=None) -> AuditReport:
    p = audit_problem(db, run, data_dir)
    if p:
        raise SequenceError(p)
    return latest_audit(db, run.id)


# ------------------------------------------------------------------ data cache
def _clean(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(float(v)) else float(v)
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        return pd.Timestamp(v).isoformat()
    return v


class _RunData:
    def __init__(self, data_dir: str, run_path: str):
        self.data_dir = data_dir
        self.labels = pd.read_parquet(Path(run_path) / "labels.parquet")
        self.labels["event_date"] = pd.to_datetime(self.labels["event_date"])
        self.by_id = self.labels.set_index("candidate_id")
        raw = load_raw(data_dir)
        self.features = raw["features"].set_index("person_id")
        codes = raw["candidate_codes"].copy()
        codes["event_date"] = pd.to_datetime(codes["event_date"])
        self.codes = {k: v.sort_values("event_date") for k, v in codes.groupby(["person_id", "family"])}
        self.ev = labeling.Evidence(raw["evidence_labs"], raw["evidence_procedures"], None,
                                    dict(zip(raw["features"]["person_id"], raw["features"]["sex"])))
        self.run_path = Path(run_path)
        self._bn = None

    def _person_notes(self, pid):
        """One patient's notes as a time-sorted index (read from the per-patient note store)."""
        if not hasattr(self, "_note_cache"):
            self._note_cache = {}
        if pid not in self._note_cache:
            if len(self._note_cache) > 256:
                self._note_cache.clear()
            df = notes_store.notes_for(raw_dir(self.data_dir), [pid])
            df["note_date"] = pd.to_datetime(df["note_date"])
            self._note_cache[pid] = labeling.Evidence._index(df, "note_date").get(pid)
        return self._note_cache[pid]

    def all_baseline_note_ids(self) -> list[str]:
        ids = pd.read_parquet(raw_dir(self.data_dir) / "baseline_imaging_notes.parquet", columns=["note_id"])["note_id"]
        return ids.astype(str).tolist()

    @property
    def baseline_notes(self) -> pd.DataFrame:
        """Baseline imaging reports by note_id: the reports sampled for annotation review once the sample exists
        (saved with the label run), else the whole file (small data, e.g. the training kit)."""
        if self._bn is None:
            p = self.run_path / "annotation_notes.parquet"
            bn = pd.read_parquet(p) if p.exists() else pd.read_parquet(raw_dir(self.data_dir) / "baseline_imaging_notes.parquet")
            bn["note_id"] = bn["note_id"].astype(str)
            self._bn = bn.set_index("note_id")
        return self._bn

    # ---- event packet (BLINDED: no auto label, probability, votes or stratum)
    def event_packet(self, candidate_id: str) -> dict:
        c = self.by_id.loc[candidate_id]
        fam, pid, d = c["family"], c["person_id"], pd.Timestamp(c["event_date"])
        f = self.features.loc[pid]
        age = _clean(f.get("age"))
        w = DISPLAY_WINDOW[fam]
        lo, hi = d + pd.Timedelta(days=w[0]), d + pd.Timedelta(days=w[1])
        codes = self.codes.get((pid, fam))
        enc = []
        if codes is not None:
            sub = codes[(codes["event_date"] >= lo) & (codes["event_date"] <= hi)]
            from training.codesets import INPATIENT_ED_VISITS, PRIMARY_STATUS
            vt = {9201: "Inpatient", 9203: "Emergency", 262: "ED → Inpatient", 9202: "Outpatient"}
            for r in sub.to_dict("records"):
                prim = r["condition_status_concept_id"] in PRIMARY_STATUS or int(_clean(r.get("primary_native")) or 0) == 1
                enc.append({"date": r["event_date"].date().isoformat(), "day": (r["event_date"] - d).days, "code": r["code"],
                            "visit": vt.get(r["visit_concept_id"], "Other"),
                            "position": "Primary" if prim else "Secondary"})
        labs = []
        for x in self.ev.labs_for(pid, d, (w[0], min(w[1], 7))):
            url = x.get("url")
            flag, ref = None, _clean(x.get("range_high"))
            if isinstance(url, float) and not math.isnan(url) and url > 0:
                flag = "H" if x["value_n"] > url else "N"
                ref = ref if ref is not None else round(url, 1)
            elif x["analyte"] == "nt_probnp" and age is not None:
                from training.codesets import NT_PROBNP_REF
                a_ev = age + c["days_from_index"] / 365.25
                lim = next((m if f.get("sex") == "male" else fm for lo, hi, m, fm in NT_PROBNP_REF if lo <= a_ev <= hi), None)
                if lim is not None:
                    flag, ref = ("H" if x["value_n"] > lim else "N"), lim
            labs.append({"time": pd.Timestamp(x["measured_at"]).isoformat(timespec="minutes"),
                         "day": (pd.Timestamp(x["measured_at"]).normalize() - d).days, "analyte": x["analyte"],
                         "value": _clean(x["value"]), "unit": x["unit"], "ref_high": ref, "flag": flag})
        procs = [{"date": pd.Timestamp(x["procedure_date"]).date().isoformat(), "day": (pd.Timestamp(x["procedure_date"]) - d).days,
                  "category": x["category"], "code": x["code"]} for x in self.ev.procs_for(pid, d, (min(w[0], -30) if fam == "af" else w[0], w[1]))]
        keys = labeling.MENTION_KEYS[fam]
        notes_w = (w[0], 30) if fam == "hf" else w
        notes = []
        for x in labeling._window(self._person_notes(pid), d, notes_w):
            txt = x["note_text"] or ""
            notes.append({"note_id": str(x["note_id"]), "date": pd.Timestamp(x["note_date"]).date().isoformat(),
                          "day": (pd.Timestamp(x["note_date"]) - d).days, "title": x.get("note_title") or "Note",
                          "excerpts": highlight_segments(txt, keys), "text": txt})
        dd = _clean(f.get("death_days"))
        death_day = None if dd is None else int(dd - c["days_from_index"])
        age = _clean(f.get("age"))
        return {
            "family": fam, "family_name": FAMILIES[fam]["name"], "definition": FAMILIES[fam]["definition"],
            "criteria": FAMILIES[fam]["criteria"], "subtypes": FAMILIES[fam]["subtypes"],
            "candidate_date": d.date().isoformat(), "window_days": list(w),
            "patient": {"age": None if age is None else round(age + c["days_from_index"] / 365.25), "sex": f.get("sex"),
                        "history": {k: bool(f.get(k)) for k in ["prior_mi", "prior_stroke", "heart_failure", "afib", "hypertension"] if k in f},
                        "baseline_meds": {k: bool(f.get(k)) for k in ["anticoagulant", "beta_blocker", "statin"] if k in f},
                        "death_day": death_day if death_day is not None and -1 <= death_day <= 30 else None},
            "encounters": enc, "labs": labs, "procedures": procs, "notes": notes,
        }

    # ---- annotation packet (shows the extracted values: the task is to verify them)
    def annotation_packet(self, note_id: str) -> dict:
        bn = self.baseline_notes
        if str(note_id) not in bn.index:               # sampled after this cache was built
            self._bn = None
            bn = self.baseline_notes
        n = bn.loc[str(note_id)]
        txt = n["note_text"]
        return {"note_id": str(note_id), "date": pd.Timestamp(n["note_date"]).date().isoformat(), "text": txt,
                "extracted": {"lvef": extract_lvef(txt), "cac_score": extract_cac(txt)}}


@lru_cache(maxsize=2)
def RunData(data_dir: str, run_path: str) -> _RunData:  # noqa: N802
    return _RunData(data_dir, run_path)


def get_run_data(data_dir, run: LabelRun) -> _RunData:
    return RunData(str(data_dir), run.path)


def current(db: Session) -> LabelRun | None:
    return active_run(db)
