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

from . import governance
from .store import AuditReport, LabelRun, active_run, log_event

RAW_FILES = ["features", "candidate_codes", "evidence_procedures", "evidence_labs", "event_notes", "baseline_imaging_notes"]
DISPLAY_WINDOW = {"ami": (-2, 7), "stroke": (-2, 7), "hf": (-3, 14), "scd": (-1, 7), "af": (-30, 30)}


def raw_dir(data_dir) -> Path:
    return Path(data_dir) / "raw"


def inputs_sha(data_dir) -> str:
    h = hashlib.sha256()
    for name in RAW_FILES:
        h.update(f"{name}:{sha256_file(raw_dir(data_dir) / f'{name}.parquet')}".encode())
    return h.hexdigest()


def load_raw(data_dir) -> dict[str, pd.DataFrame]:
    missing = [n for n in RAW_FILES if not (raw_dir(data_dir) / f"{n}.parquet").exists()]
    if missing:
        raise FileNotFoundError(f"Missing raw files in {raw_dir(data_dir)}: {', '.join(missing)} — run training.extract first")
    return {n: pd.read_parquet(raw_dir(data_dir) / f"{n}.parquet") for n in RAW_FILES}


def is_synthetic(data_dir) -> bool:
    return (raw_dir(data_dir) / "SYNTHETIC").exists()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


# ------------------------------------------------------------------ label run
def create_label_run(db: Session, data_dir, username: str, label_model_path: str | None = None) -> LabelRun:
    gov = governance.load(data_dir)
    model = json.loads(Path(label_model_path).read_text()) if label_model_path else json.loads(json.dumps(labeling.DEFAULT_LABEL_MODEL))
    model["thresholds"] = gov["label_thresholds"]
    raw = load_raw(data_dir)
    cands = labeling.build_episodes(raw["candidate_codes"], raw["evidence_procedures"], raw["features"])
    ev = labeling.Evidence(raw["evidence_labs"], raw["evidence_procedures"], raw["event_notes"],
                           dict(zip(raw["features"]["person_id"], raw["features"]["sex"])))
    labels = labeling.mark_relevance(labeling.label_candidates(cands, ev, raw["features"], model), gov["definitions"])

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
    return run


def run_audit(db: Session, data_dir, run: LabelRun, username: str) -> AuditReport:
    gov = governance.load(data_dir)
    raw = load_raw(data_dir)
    labels = pd.read_parquet(Path(run.path) / "labels.parquet")
    counts = {k: int(len(raw[k])) for k in ["evidence_labs", "evidence_procedures", "event_notes"]}
    rep = audit_mod.run_audit(raw["features"], labels, counts, raw["baseline_imaging_notes"], gov)
    rep["label_run_id"] = run.id
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
        self.ev = labeling.Evidence(raw["evidence_labs"], raw["evidence_procedures"], raw["event_notes"],
                                    dict(zip(raw["features"]["person_id"], raw["features"]["sex"])))
        bn = raw["baseline_imaging_notes"].copy()
        bn["note_id"] = bn["note_id"].astype(str)
        self.baseline_notes = bn.set_index("note_id")

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
                enc.append({"date": r["event_date"].date().isoformat(), "day": (r["event_date"] - d).days, "code": r["code"],
                            "visit": vt.get(r["visit_concept_id"], "Other"),
                            "position": "Primary" if r["condition_status_concept_id"] in PRIMARY_STATUS else "Secondary"})
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
        for x in self.ev.notes_for(pid, d, notes_w):
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
        n = self.baseline_notes.loc[str(note_id)]
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
