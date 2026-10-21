"""Training kit for the CardioAI Label & Annotation Training Program — SYNTHETIC DATA ONLY.

Trainees learn on the real curation workbench, inside the Mayo Clinic Platform workspace, using a separate
training data folder that holds only synthetic data. Nothing here may point at a live data folder: every
command refuses to run unless the folder carries both the SYNTHETIC and the TRAINING marker.

Trainer files (answer keys, the clean copies used by `repair`, case maps) live in --trainer-dir, which must be
outside the training data folder and readable by the trainer only.

    # Session prep (trainer)
    python -m curation.training_kit setup        --data-dir train_env --trainer-dir trainer_only
    python -m curation.training_kit add-trainees --data-dir train_env --roster roster.csv
    # Module 2 exercise (DS + lead)
    python -m curation.training_kit explain      --data-dir train_env --near-threshold 3
    # Modules 3-4 exercise (DS + lead): planted faults
    python -m curation.training_kit plant-faults --data-dir train_env --out faults_env --trainer-dir trainer_only
    python -m curation.training_kit repair       --data-dir faults_env --check <audit check id> --trainer-dir trainer_only
    python -m curation.training_kit grade-faults --data-dir faults_env --trainer-dir trainer_only
    # Modules 5-6 (readers + lead): practice, calibration, seeded disagreements
    python -m curation.training_kit make-set     --data-dir train_env --set practice|calibration|disagreements --trainer-dir trainer_only
    python -m curation.training_kit key          --data-dir train_env --set calibration --trainer-dir trainer_only
    python -m curation.training_kit score        --data-dir train_env --set calibration --trainer-dir trainer_only
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import secrets
import shutil
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select

from cardioai_core.schema import FAMILIES
from cardioai_core.security import hash_password

from . import adjudication, governance
from .adjudication import cohen_kappa
from .runs import create_label_run, current, get_run_data, is_synthetic, latest_audit, raw_dir, run_audit
from .store import Approval, Case, LabelRun, Read, Review, User, check_reader_policy, log_event, open_store

TRAINER = "trainer"
SETS = {   # event cases per outcome family, report-value (annotation) cases, who receives the set
    "practice": {"per_family": 2, "annotation": 2, "pattern": "copy"},
    "calibration": {"per_family": 8, "annotation": 5, "pattern": "copy"},
    "disagreements": {"per_family": 2, "annotation": 0, "pattern": "seeded"},
}
# Certification standards (training program, Module 6). The lead cardiologist confirms them before session 1.
STANDARDS = {"overall_agreement": 0.85, "family_agreement": 0.75, "kappa": 0.70, "date_agreement": 0.90,
             "rationale_min_chars": 20}
PLANTED = {   # audit check id -> what was broken, and which raw table/columns `repair` restores
    "units": {"fault": "A batch of total cholesterol values was loaded in mmol/L, not mg/dL",
              "table": "features", "columns": ["total_chol"]},
    "plausibility": {"fault": "Same root cause as 'units': mmol/L cholesterol values fall below the clinical range",
                     "table": "features", "columns": ["total_chol"]},
    "evidence_tables": {"fault": "The clinical-notes extract (event_notes) came back empty",
                        "table": "event_notes", "columns": None},
    "events_after_death": {"fault": "Death dates were shifted for a group of patients (death-linkage error)",
                           "table": "features", "columns": ["death_days", "followup_days"]},
}
READER_ROLES = ("cardiologist", "lead_cardiologist")


class KitError(SystemExit):
    pass


# ------------------------------------------------------------------ guards and helpers
def _require_training_env(data_dir) -> None:
    d = Path(data_dir)
    if not is_synthetic(d) or not (d / "TRAINING").exists():
        raise KitError(f"{d} is not a training environment (needs the SYNTHETIC and TRAINING markers). "
                       "The training kit never runs on live or Mayo-derived data.")


def _trainer_dir(path: str, data_dir) -> Path:
    t = Path(path).resolve()
    if Path(data_dir).resolve() in t.parents or t == Path(data_dir).resolve():
        raise KitError("--trainer-dir must be outside the training data folder (it holds the answer keys)")
    t.mkdir(parents=True, exist_ok=True)
    return t


def _db(data_dir):
    return open_store(data_dir)()


def _run(db) -> LabelRun:
    r = current(db)
    if r is None:
        raise KitError("No active label run in this environment — run `setup` (or `curation.pipeline label`) first")
    return r


def _truth(data_dir):
    t = pd.read_parquet(raw_dir(data_dir) / "truth.parquet")
    t["date"] = pd.to_datetime(t["date"])
    ann = pd.read_parquet(raw_dir(data_dir) / "truth_annotations.parquet")
    ann["note_id"] = ann["note_id"].astype(str)
    return {k: v for k, v in t.groupby(["person_id", "family"])}, ann.set_index("note_id").to_dict("index")


def true_event(rd, tby, candidate_id: str) -> dict:
    """Ground truth for a candidate (synthetic generator): the true event within 7 days of the candidate date."""
    c = rd.by_id.loc[candidate_id]
    t = tby.get((c["person_id"], c["family"]))
    if t is not None:
        gap = (t["date"] - c["event_date"]).abs().dt.days
        if gap.min() <= 7:
            r = t.loc[gap.idxmin()]
            if bool(r["event"]):
                return {"outcome": "event", "subtype": str(r["subtype"]), "event_date": r["date"].date().isoformat()}
    return {"outcome": "not_event", "subtype": "", "event_date": ""}


def _num(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def _write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise KitError(f"Missing {path} — run make-set first")
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ setup
def setup(data_dir, trainer_dir, n: int = 20000, seed: int = 2026) -> dict:
    from training.synthetic_raw import generate_raw, write_raw
    d = Path(data_dir)
    if (d / "curation.db").exists():
        raise KitError(f"{d} already holds a curation store; use a new folder")
    _trainer_dir(trainer_dir, d)
    write_raw(raw_dir(d), generate_raw(n=n, seed=seed))
    cfg = governance.load(d)
    cfg["provenance"] = "synthetic-demo"
    governance.save(d, cfg)
    (d / ".secret").write_text(secrets.token_urlsafe(48))
    (d / "TRAINING").write_text("Training environment: synthetic data only. Created by curation.training_kit.\n")
    db = _db(d)
    log_event(db, TRAINER, "training.setup", str(d), {"n_patients": n, "seed": seed})
    db.commit()
    run = create_label_run(db, d, TRAINER)
    ar = run_audit(db, d, run, TRAINER)
    return {"label_run": run.id, "audit": ar.overall, "patients": n}


def add_trainees(data_dir, roster: str) -> list[dict]:
    _require_training_env(data_dir)
    db = _db(data_dir)
    gov = governance.load(data_dir)
    out = []
    for r in _read_csv(Path(roster)):
        role = r["role"].strip()
        if role not in ("data_scientist", *READER_ROLES):
            raise KitError(f"{r['username']}: role must be data_scientist, cardiologist or lead_cardiologist")
        if db.scalar(select(User).where(User.username == r["username"].strip())):
            print(f"  skip {r['username']}: already exists")
            continue
        check_reader_policy(db, gov, role, r.get("email", "").strip())
        pw = secrets.token_urlsafe(12)
        db.add(User(username=r["username"].strip(), full_name=r.get("full_name", "").strip(), email=r.get("email", "").strip(),
                    role=role, password_hash=hash_password(pw)))
        log_event(db, TRAINER, "training.trainee_added", r["username"].strip(), {"role": role})
        db.flush()
        out.append({"username": r["username"].strip(), "role": role, "one_time_password": pw})
    db.commit()
    return out


# ------------------------------------------------------------------ Module 2: why did a candidate get its label?
def explain(data_dir, candidate: str | None = None, near_threshold: int = 0) -> list[dict]:
    _require_training_env(data_dir)
    db = _db(data_dir)
    lab = pd.read_parquet(Path(_run(db).path) / "labels.parquet")
    if candidate:
        rows = lab[lab["candidate_id"] == candidate]
        if rows.empty:
            raise KitError(f"No candidate {candidate} in the active run")
    else:
        rel = lab[lab["label_relevant"]].copy()
        rel["dist"] = (rel["p_auto"] - 0.5).abs()
        rows = rel.sort_values("dist").groupby("family").head(max(near_threshold, 1))
    lf_cols = [c for c in lab.columns if c.startswith("lf_")]
    out = []
    for r in rows.to_dict("records"):
        votes = {c[3:]: int(r[c]) for c in lf_cols if r[c] != 0}
        out.append({"candidate_id": r["candidate_id"], "family": r["family"], "event_date": str(pd.Timestamp(r["event_date"]).date()),
                    "source": r["source"], "codes": r.get("codes", ""), "p_auto": round(float(r["p_auto"]), 3),
                    "auto_label": r["auto_label"], "subtype_auto": r.get("subtype_auto"), "votes": votes})
    return out


# ------------------------------------------------------------------ Modules 3-4: planted faults
def plant_faults(data_dir, out_dir, trainer_dir, seed: int = 7) -> dict:
    from training.labeling import build_episodes
    _require_training_env(data_dir)
    src, dst = Path(data_dir), Path(out_dir)
    if dst.exists():
        raise KitError(f"{dst} exists; choose a new folder")
    t = _trainer_dir(trainer_dir, dst)
    rng = np.random.default_rng(seed)
    shutil.copytree(raw_dir(src), raw_dir(dst))
    clean = t / "faults_clean_raw"
    if clean.exists():
        shutil.rmtree(clean)
    clean.mkdir(parents=True)
    feats = pd.read_parquet(raw_dir(dst) / "features.parquet")
    notes = pd.read_parquet(raw_dir(dst) / "event_notes.parquet")
    feats.to_parquet(clean / "features.parquet", index=False)
    notes.to_parquet(clean / "event_notes.parquet", index=False)

    # 1. units: 5% of total cholesterol values in mmol/L
    has = feats.index[feats["total_chol"].notna()]
    pick = rng.choice(has, size=max(1, int(0.05 * len(has))), replace=False)
    feats.loc[pick, "total_chol"] = (feats.loc[pick, "total_chol"] / 38.67).round(2)
    # 2. empty notes extract
    notes = notes.iloc[0:0]
    # 3. death dates moved before outcome candidates for ~3% of patients with a candidate
    codes = pd.read_parquet(raw_dir(dst) / "candidate_codes.parquet")
    procs = pd.read_parquet(raw_dir(dst) / "evidence_procedures.parquet")
    cand = build_episodes(codes, procs, feats)
    first = cand.sort_values("days_from_index").drop_duplicates("person_id")
    first = first[first["days_from_index"] > 90]
    k = max(1, int(0.03 * cand["person_id"].nunique()))
    who = first.sample(n=min(k, len(first)), random_state=seed)
    newd = dict(zip(who["person_id"], who["days_from_index"] - 60))
    m = feats["person_id"].isin(newd)
    feats.loc[m, "death_days"] = feats.loc[m, "person_id"].map(newd).astype(float)
    feats.loc[m, "followup_days"] = np.minimum(feats.loc[m, "followup_days"], feats.loc[m, "death_days"])

    feats.to_parquet(raw_dir(dst) / "features.parquet", index=False)
    notes.to_parquet(raw_dir(dst) / "event_notes.parquet", index=False)
    cfg = governance.load(src)
    governance.save(dst, cfg)
    (dst / ".secret").write_text(secrets.token_urlsafe(48))
    (dst / "TRAINING").write_text("Training environment (planted-fault exercise): synthetic data only.\n")
    db = _db(dst)
    for u in _db(src).scalars(select(User).where(User.role.in_(["data_scientist", "lead_cardiologist", "admin"]))):
        db.add(User(username=u.username, full_name=u.full_name, email=u.email, role=u.role, password_hash=u.password_hash))
    log_event(db, TRAINER, "training.faults_planted", str(dst), {"n_faults": len(PLANTED)})
    db.commit()
    key = {"planted": {cid: v["fault"] for cid, v in PLANTED.items()},
           "counts": {"units_rows": int(len(pick)), "deaths_moved": int(m.sum())},
           "expected": "Fix (repair + re-run label and audit) all three; never waive them. Acknowledge the remaining "
                       "warnings with a justification an auditor could follow, then approve the dataset."}
    (t / "faults_key.json").write_text(json.dumps(key, indent=2))
    return key


def repair(data_dir, check: str, trainer_dir) -> str:
    """Stands in for 'fix the extraction and re-extract': restores the clean data behind one planted fault."""
    _require_training_env(data_dir)
    t = _trainer_dir(trainer_dir, data_dir)
    if check not in PLANTED:
        raise KitError(f"'{check}' is not caused by a broken extract in this exercise. It reflects the data as it is: "
                       "acknowledge it (warning) or waive it (failure) with a justification in the workbench.")
    p = PLANTED[check]
    clean = pd.read_parquet(t / "faults_clean_raw" / f"{p['table']}.parquet")
    path = raw_dir(data_dir) / f"{p['table']}.parquet"
    if p["columns"] is None:
        clean.to_parquet(path, index=False)
    else:
        cur = pd.read_parquet(path).set_index("person_id")
        cl = clean.set_index("person_id")
        for c in p["columns"]:
            cur[c] = cl[c]
        cur.reset_index().to_parquet(path, index=False)
    db = _db(data_dir)
    log_event(db, "trainee", "training.repair", check, {"table": p["table"]})
    db.commit()
    return f"Re-extracted {p['table']} for '{check}'. Now re-run: python -m curation.pipeline --data-dir {data_dir} label  (then: audit)"


def grade_faults(data_dir, trainer_dir) -> dict:
    _require_training_env(data_dir)
    t = _trainer_dir(trainer_dir, data_dir)
    key = json.loads((t / "faults_key.json").read_text())
    db = _db(data_dir)
    runs = list(db.scalars(select(LabelRun).order_by(LabelRun.created_at)))
    run = current(db)
    ar = latest_audit(db, run.id) if run else None
    checks = {c["id"]: c for c in (ar.report["checks"] if ar else [])}
    waived = [(r.item_id.split(":", 1)[1], r.label_run_id) for r in db.scalars(select(Review).where(Review.action == "waive"))
              if r.item_id.split(":", 1)[-1] in PLANTED]
    found_in_first = []
    if runs:
        first = latest_audit(db, runs[0].id)
        found_in_first = [c["id"] for c in (first.report["checks"] if first else []) if c["id"] in PLANTED and c["status"] == "fail"]
    reviews = {}
    if run:
        for r in db.scalars(select(Review).where(Review.label_run_id == run.id).order_by(Review.id)):
            reviews[r.item_id] = r
    items = []
    for cid, c in checks.items():
        if c["status"] == "pass":
            continue
        r = reviews.get(f"audit:{cid}")
        ok = r is not None and r.action in ("acknowledge", "waive") and len((r.justification or "").strip()) >= 20
        items.append({"check": cid, "status": c["status"], "action": r.action if r else None,
                      "justification": r.justification if r else "", "ok": ok})
    approved = bool(run and db.scalar(select(Approval).where(Approval.label_run_id == run.id, Approval.stage == "ds_dataset_review",
                                                                Approval.decision == "approve")))
    fixed = {cid: checks.get(cid, {}).get("status") != "fail" for cid in PLANTED}
    result = {
        "label_runs": len(runs), "planted_faults_seen_in_first_audit": found_in_first,
        "fixed": fixed, "planted_faults_waived": waived, "remaining_items": items, "dataset_approved": approved,
        "pass": all(fixed.values()) and not waived and all(i["ok"] for i in items) and approved,
        "expected": key["expected"],
    }
    return result


# ------------------------------------------------------------------ Modules 5-6: practice, calibration, disagreements
def _pick(rd, tby, labels: pd.DataFrame, fam: str, n: int, used: set, rng) -> list[str]:
    rel = labels[(labels["family"] == fam) & labels["label_relevant"] & ~labels["candidate_id"].isin(used)].copy()
    if rel.empty:
        return []
    rel["truth"] = [true_event(rd, tby, c)["outcome"] for c in rel["candidate_id"]]
    picks: list[str] = []
    for outcome, k in (("event", (n + 1) // 2), ("not_event", n // 2)):
        pool = rel[rel["truth"] == outcome]
        hard = pool[pool["auto_label"] == "uncertain"]
        k_hard = min(len(hard), k // 2)
        a = hard.sample(n=k_hard, random_state=int(rng.integers(0, 2**31 - 1)))["candidate_id"].tolist() if k_hard else []
        rest = pool[~pool["candidate_id"].isin(a)]
        k_rest = min(len(rest), k - len(a))
        b = rest.sample(n=k_rest, random_state=int(rng.integers(0, 2**31 - 1)))["candidate_id"].tolist() if k_rest else []
        picks += a + b
    rng.shuffle(picks)
    return picks


def _wrong_read(key: dict, fam: str, rng, cand_date: str) -> dict:
    """A plausible but wrong independent read, for the seeded-disagreement exercise."""
    f = FAMILIES[fam]
    if key["outcome"] == "event":
        other = [s for s in f["subtypes"] if s not in (key["subtype"], "unknown", "unspecified")]
        mode = int(rng.integers(0, 3)) if other else int(rng.choice([0, 2]))
        if mode == 0:
            return {"outcome": "not_event", "confidence": "medium", "comment": "No clear evidence of a new event."}
        if mode == 1:
            st = str(rng.choice(other))
            return {"outcome": "event", "confidence": "medium", "subtype": st, "event_date": key["event_date"],
                    "criteria": [next(iter(f["criteria"]))], "comment": ""}
        d = (dt.date.fromisoformat(key["event_date"]) + dt.timedelta(days=int(rng.integers(14, 40)))).isoformat()
        return {"outcome": "event", "confidence": "low", "subtype": key["subtype"], "event_date": d,
                "criteria": [next(iter(f["criteria"]))], "comment": "Dated from the follow-up visit."}
    return {"outcome": "event", "confidence": "low", "subtype": "unknown" if "unknown" in f["subtypes"] else "unspecified",
            "event_date": cand_date, "criteria": [next(iter(f["criteria"]))], "comment": "Coded as an event."}


def _right_read(key: dict, fam: str) -> dict:
    f = FAMILIES[fam]
    if key["outcome"] == "event":
        return {"outcome": "event", "confidence": "high", "subtype": key["subtype"], "event_date": key["event_date"],
                "criteria": [next(iter(f["criteria"]))], "comment": ""}
    return {"outcome": "not_event", "confidence": "high", "comment": ""}


def make_set(data_dir, set_name: str, trainer_dir, usernames: list[str] | None = None, seed: int = 31) -> dict:
    _require_training_env(data_dir)
    if set_name not in SETS:
        raise KitError(f"--set must be one of {', '.join(SETS)}")
    t = _trainer_dir(trainer_dir, data_dir)
    if (t / f"{set_name}_cases.csv").exists():
        raise KitError(f"The {set_name} set already exists (see {t / f'{set_name}_cases.csv'})")
    spec = SETS[set_name]
    db = _db(data_dir)
    run = _run(db)
    gov = governance.load(data_dir)
    rd = get_run_data(data_dir, run)
    tby, ann_truth = _truth(data_dir)
    rng = np.random.default_rng(seed + sum(map(ord, set_name)))
    used = set()
    for other in SETS:
        p = t / f"{other}_key.csv"
        if p.exists():
            used |= {r["ref_id"] for r in _read_csv(p)}

    roles = ("lead_cardiologist",) if spec["pattern"] == "seeded" else READER_ROLES
    q = select(User).where(User.active, User.role.in_(roles))
    trainees = [u for u in db.scalars(q) if not usernames or u.username in usernames]
    if not trainees:
        raise KitError(f"No active {' / '.join(roles)} trainees — add them with add-trainees first")

    key_rows, case_rows = [], []
    for fam in FAMILIES:
        for cid in _pick(rd, tby, rd.labels, fam, spec["per_family"], used, rng):
            used.add(cid)
            k = true_event(rd, tby, cid)
            key_rows.append({"ref_id": cid, "case_type": "event", "family": fam, "key_outcome": k["outcome"],
                             "key_subtype": k["subtype"], "key_event_date": k["event_date"], "key_lvef": "", "key_cac_score": "",
                             "confirmed_by": "", "note": ""})
    if spec["annotation"]:
        notes = [n for n in rd.baseline_notes.index if n not in used]
        for nid in rng.choice(notes, size=min(spec["annotation"], len(notes)), replace=False):
            tr = ann_truth.get(str(nid), {})
            key_rows.append({"ref_id": str(nid), "case_type": "annotation", "family": "imaging", "key_outcome": "",
                             "key_subtype": "", "key_event_date": "", "key_lvef": _num(tr.get("lvef_true")) or "",
                             "key_cac_score": "" if _num(tr.get("cac_true")) is None else _num(tr.get("cac_true")),
                             "confirmed_by": "", "note": ""})

    if spec["pattern"] == "copy":
        for u in trainees:
            for k in key_rows:
                case = Case(id="TRN-" + secrets.token_hex(4).upper(), label_run_id=run.id, case_type=k["case_type"], ref_id=k["ref_id"],
                            family=k["family"], stratum=f"training:{set_name}", sampling_prob=1.0, reads_required=1)
                db.add(case)
                db.add(Read(case_id=case.id, reader_id=u.id, kind="independent"))
                case_rows.append({"case_id": case.id, "ref_id": k["ref_id"], "username": u.username})
    else:   # seeded disagreements: two simulated independent reads (one right, one plausibly wrong) -> lead queue
        sims = []
        for name in ("trainer-sim-a", "trainer-sim-b"):
            s = db.scalar(select(User).where(User.username == name))
            if s is None:
                s = User(username=name, full_name="Simulated reader (training)", role="data_scientist",
                         password_hash=hash_password(secrets.token_urlsafe(24)), active=False)
                db.add(s)
                db.flush()
            sims.append(s)
        now = dt.datetime.now(dt.timezone.utc)
        for k in key_rows:
            key = {"outcome": k["key_outcome"], "subtype": k["key_subtype"], "event_date": k["key_event_date"]}
            case = Case(id="TRN-" + secrets.token_hex(4).upper(), label_run_id=run.id, case_type="event", ref_id=k["ref_id"],
                        family=k["family"], stratum="training:disagreements", sampling_prob=1.0, reads_required=2)
            db.add(case)
            cand_date = pd.Timestamp(rd.by_id.loc[k["ref_id"], "event_date"]).date().isoformat()
            reads = [_right_read(key, k["family"]), _wrong_read(key, k["family"], rng, cand_date)]
            if rng.random() < 0.5:
                reads.reverse()
            for sim, dec in zip(sims, reads):
                db.add(Read(case_id=case.id, reader_id=sim.id, kind="independent",
                            decision=adjudication.validate_decision(case, dec), submitted_at=now))
            db.flush()
            adjudication._update_status(db, case, gov, TRAINER)
            for u in trainees:
                case_rows.append({"case_id": case.id, "ref_id": k["ref_id"], "username": u.username})

    log_event(db, TRAINER, "training.set_created", set_name,
              {"cases_per_trainee": len(key_rows), "trainees": [u.username for u in trainees]})
    db.commit()
    _write_csv(t / f"{set_name}_key.csv", key_rows, ["ref_id", "case_type", "family", "key_outcome", "key_subtype", "key_event_date",
                                                     "key_lvef", "key_cac_score", "confirmed_by", "note"])
    _write_csv(t / f"{set_name}_cases.csv", case_rows, ["case_id", "ref_id", "username"])
    return {"set": set_name, "cases_per_trainee": len(key_rows), "trainees": [u.username for u in trainees],
            "key": str(t / f"{set_name}_key.csv")}


def key_packets(data_dir, set_name: str, trainer_dir) -> Path:
    """Write the answer key with each case's evidence, for the lead cardiologist to confirm before scoring."""
    _require_training_env(data_dir)
    t = _trainer_dir(trainer_dir, data_dir)
    db = _db(data_dir)
    rd = get_run_data(data_dir, _run(db))
    lines = [f"# Answer key for review — {set_name}", "",
             "Draft key from the synthetic generator's ground truth. Read each case, then enter your name in `confirmed_by` "
             f"in {set_name}_key.csv, or correct the key columns and explain in `note`.", ""]
    for i, k in enumerate(_read_csv(t / f"{set_name}_key.csv"), 1):
        lines += [f"## {i}. {k['family']} · {k['ref_id']}", ""]
        if k["case_type"] == "annotation":
            p = rd.annotation_packet(k["ref_id"])
            lines += [f"Report ({p['date']}): {p['text']}", "",
                      f"**Key:** LVEF {k['key_lvef'] or 'not reported'} · CAC {k['key_cac_score'] or 'not reported'}", ""]
            continue
        lines += _packet_lines(rd.event_packet(k["ref_id"]))
        lines += [f"**Key:** {k['key_outcome']}" + (f" · {k['key_subtype']} · {k['key_event_date']}" if k["key_outcome"] == "event" else ""), ""]
    out = t / f"{set_name}_key_review.md"
    out.write_text("\n".join(lines))
    return out


def _packet_lines(p: dict) -> list[str]:
    pt = p["patient"]
    out = [f"Patient: {pt['age']} y, {pt['sex']}. Candidate date {p['candidate_date']}.", ""]
    for e in p["encounters"]:
        out.append(f"- Day {e['day']:+d}: code {e['code']} ({e['visit']}, {e['position']})")
    for x in p["labs"]:
        out.append(f"- Day {x['day']:+d}: {x['analyte']} {x['value']} {x['unit']}" + (f" (ref ≤{x['ref_high']}, {x['flag']})" if x["flag"] else ""))
    for x in p["procedures"]:
        out.append(f"- Day {x['day']:+d}: procedure {x['category']} ({x['code']})")
    for x in p["notes"]:
        out.append(f"- Day {x['day']:+d}: {x['title']}: {x['text']}")
    if pt.get("death_day") is not None:
        out.append(f"- Died on day {pt['death_day']:+d}")
    return out + [""]


def _agrees(read: dict, key: dict, days: int) -> bool:
    if read["outcome"] != key["key_outcome"]:
        return False
    if read["outcome"] != "event":
        return True
    vague = {"unknown", "unspecified"}
    if read.get("subtype") != key["key_subtype"] and not ({read.get("subtype"), key["key_subtype"]} & vague):
        return False
    return abs((dt.date.fromisoformat(read["event_date"]) - dt.date.fromisoformat(key["key_event_date"])).days) <= days


def score(data_dir, set_name: str, trainer_dir, standards: dict | None = None) -> dict:
    _require_training_env(data_dir)
    S = {**STANDARDS, **(standards or {})}
    t = _trainer_dir(trainer_dir, data_dir)
    db = _db(data_dir)
    gov = governance.load(data_dir)
    days = gov["definitions"]["date_agreement_days"]
    keys = {k["ref_id"]: k for k in _read_csv(t / f"{set_name}_key.csv")}
    cases = _read_csv(t / f"{set_name}_cases.csv")
    unconfirmed = [r for r, k in keys.items() if not k["confirmed_by"].strip()]
    users = {u.id: u.username for u in db.scalars(select(User))}
    rd = get_run_data(data_dir, _run(db))

    per: dict[str, dict] = {}
    for row in cases:
        k = keys[row["ref_id"]]
        reads = list(db.scalars(select(Read).where(Read.case_id == row["case_id"])))
        if set_name == "disagreements":
            lead = next((r for r in reads if r.kind == "lead"), None)
            if lead is None or lead.submitted_at is None or users.get(lead.reader_id) != row["username"]:
                continue
            dec = lead.decision
        else:
            mine = next((r for r in reads if r.kind == "independent"), None)
            dec = mine.decision if mine and mine.submitted_at else None
        p = per.setdefault(row["username"], {"items": []})
        p["items"].append({"case_id": row["case_id"], "ref_id": row["ref_id"], "family": k["family"], "type": k["case_type"],
                           "key": k, "read": dec})

    out = {"set": set_name, "standards": S, "key_unconfirmed": len(unconfirmed), "trainees": {}}
    for user, p in per.items():
        ev = [i for i in p["items"] if i["type"] == "event"]
        done = [i for i in ev if i["read"]]
        ann = [i for i in p["items"] if i["type"] == "annotation" and i["read"]]
        res: dict = {"cases": len(ev), "submitted": len(done), "complete": len(done) == len(ev) and len(ev) > 0}
        if done:
            ok = [_agrees(i["read"], i["key"], days) for i in done]
            res["overall_agreement"] = sum(ok) / len(ok)
            res["kappa"] = cohen_kappa([i["read"]["outcome"] for i in done], [i["key"]["key_outcome"] for i in done])
            fam = {}
            for f in FAMILIES:
                fi = [o for o, i in zip(ok, done) if i["family"] == f]
                if fi:
                    fam[f] = sum(fi) / len(fi)
            res["family_agreement"] = fam
            both = [i for i in done if i["read"]["outcome"] == "event" and i["key"]["key_outcome"] == "event"]
            dates = [abs((dt.date.fromisoformat(i["read"]["event_date"]) - dt.date.fromisoformat(i["key"]["key_event_date"])).days) <= days
                     for i in both]
            res["date_agreement"] = (sum(dates) / len(dates)) if dates else None
            subs = [i["read"].get("subtype") == i["key"]["key_subtype"] for i in both]
            res["subtype_agreement"] = (sum(subs) / len(subs)) if subs else None
            res["discordant"] = [{"case_id": i["case_id"], "family": i["family"], "read": i["read"], "key": {
                "outcome": i["key"]["key_outcome"], "subtype": i["key"]["key_subtype"], "event_date": i["key"]["key_event_date"]}}
                for o, i in zip(ok, done) if not o]
        if ann:
            ok_a = []
            for i in ann:
                for fld in ("lvef", "cac_score"):
                    kv = i["key"][f"key_{fld}"]
                    kv = float(kv) if kv not in ("", None) else None
                    rv = (i["read"].get(fld) or {}).get("value")
                    ok_a.append(adjudication._ann_correct(fld, rv, kv))
            res["report_value_accuracy"] = sum(ok_a) / len(ok_a)
        if set_name == "disagreements":
            res["rationale_given"] = sum(len((i["read"] or {}).get("comment", "").strip()) >= S["rationale_min_chars"] for i in done)
            res["pass"] = res["complete"] and res.get("overall_agreement", 0) >= S["overall_agreement"] and res["rationale_given"] == len(ev)
        else:
            res["pass"] = bool(res["complete"] and res.get("overall_agreement", 0) >= S["overall_agreement"]
                               and all(v >= S["family_agreement"] for v in res.get("family_agreement", {}).values())
                               and (res.get("kappa") or 0) >= S["kappa"]
                               and (res.get("date_agreement") is None or res["date_agreement"] >= S["date_agreement"]))
        out["trainees"][user] = res
        if res.get("discordant"):
            lines = [f"# Discordant cases — {user} — {set_name}", ""]
            for d in res["discordant"]:
                lines += [f"## {d['case_id']} · {d['family']}", ""] + _packet_lines(rd.event_packet(next(
                    i["ref_id"] for i in p["items"] if i["case_id"] == d["case_id"])))
                lines += [f"**Your read:** {_fmt(d['read'])}", f"**Key:** {_fmt(d['key'])}", ""]
            (t / f"discordant_{set_name}_{user}.md").write_text("\n".join(lines))
    (t / f"scores_{set_name}.json").write_text(json.dumps(out, indent=2, default=str))
    (t / f"CERTIFICATION_{set_name}.md").write_text(_report_md(out))
    return out


def _fmt(d: dict) -> str:
    if not d:
        return "—"
    return d.get("outcome", "") + (f" · {d.get('subtype')} · {d.get('event_date')}" if d.get("outcome") == "event" else "")


def _pct(v):
    return "—" if v is None else f"{v:.0%}"


def _report_md(out: dict) -> str:
    S = out["standards"]
    lines = [f"# Training results — {out['set']}", "",
             f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. Synthetic training data only.", ""]
    if out["key_unconfirmed"]:
        lines += [f"> **Draft:** {out['key_unconfirmed']} key rows are not yet confirmed by the lead cardiologist. "
                  "Certification decisions need a confirmed key.", ""]
    lines += [f"Standards: overall agreement ≥ {S['overall_agreement']:.0%}, every family ≥ {S['family_agreement']:.0%}, "
              f"κ ≥ {S['kappa']:.2f}, event date within 7 days ≥ {S['date_agreement']:.0%}.", "",
              "| Trainee | Submitted | Overall | κ | Lowest family | Dates | Subtypes | Report values | Result |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for user, r in sorted(out["trainees"].items()):
        fam = r.get("family_agreement") or {}
        low = min(fam.items(), key=lambda x: x[1]) if fam else None
        k = r.get("kappa")
        lines.append(f"| {user} | {r['submitted']}/{r['cases']} | {_pct(r.get('overall_agreement'))} | "
                     f"{'—' if k is None else f'{k:.2f}'} | {'—' if not low else f'{low[0]} {low[1]:.0%}'} | "
                     f"{_pct(r.get('date_agreement'))} | {_pct(r.get('subtype_agreement'))} | {_pct(r.get('report_value_accuracy'))} | "
                     f"{'PASS' if r['pass'] else ('INCOMPLETE' if not r['complete'] else 'RETAKE')} |")
    lines += ["", "Discordant cases for each trainee are in `discordant_<set>_<user>.md` for the review session."]
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m curation.training_kit", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def p(name, help_):
        x = sub.add_parser(name, help=help_)
        x.add_argument("--data-dir", required=True)
        return x
    x = p("setup", "create a synthetic training environment with a label run and audit")
    x.add_argument("--trainer-dir", required=True)
    x.add_argument("--n", type=int, default=20000)
    x = p("add-trainees", "create trainee accounts from a CSV (username,full_name,email,role); prints one-time passwords")
    x.add_argument("--roster", required=True)
    x = p("explain", "show labelling-function votes behind auto-labels (Module 2)")
    x.add_argument("--candidate")
    x.add_argument("--near-threshold", type=int, default=3)
    x = p("plant-faults", "copy the environment with three planted extraction faults (Modules 3-4)")
    x.add_argument("--out", required=True)
    x.add_argument("--trainer-dir", required=True)
    x = p("repair", "re-extract the data behind one planted fault (stands in for fixing the extraction)")
    x.add_argument("--check", required=True)
    x.add_argument("--trainer-dir", required=True)
    x = p("grade-faults", "grade the data scientist's audit review in a planted-fault environment")
    x.add_argument("--trainer-dir", required=True)
    x = p("make-set", "assign practice / calibration cases or seed disagreements in the workbench")
    x.add_argument("--set", required=True, choices=list(SETS))
    x.add_argument("--trainer-dir", required=True)
    x.add_argument("--users", nargs="*")
    x = p("key", "write the answer key with each case's evidence for the lead to confirm")
    x.add_argument("--set", required=True, choices=list(SETS))
    x.add_argument("--trainer-dir", required=True)
    x = p("score", "score a set against the key and write the certification report")
    x.add_argument("--set", required=True, choices=list(SETS))
    x.add_argument("--trainer-dir", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "setup":
        print(json.dumps(setup(a.data_dir, a.trainer_dir, a.n), indent=2))
    elif a.cmd == "add-trainees":
        rows = add_trainees(a.data_dir, a.roster)
        print("One-time passwords (give each in person; shown only now):")
        for r in rows:
            print(f"  {r['username']:<28} {r['role']:<18} {r['one_time_password']}")
    elif a.cmd == "explain":
        for r in explain(a.data_dir, a.candidate, a.near_threshold):
            print(json.dumps(r, default=str))
    elif a.cmd == "plant-faults":
        plant_faults(a.data_dir, a.out, a.trainer_dir)
        print(f"Planted-fault environment ready in {a.out}. Key: {Path(a.trainer_dir) / 'faults_key.json'} (trainer only).")
    elif a.cmd == "repair":
        print(repair(a.data_dir, a.check, a.trainer_dir))
    elif a.cmd == "grade-faults":
        print(json.dumps(grade_faults(a.data_dir, a.trainer_dir), indent=2, default=str))
    elif a.cmd == "make-set":
        print(json.dumps(make_set(a.data_dir, a.set, a.trainer_dir, a.users), indent=2))
    elif a.cmd == "key":
        print(f"Key review written to {key_packets(a.data_dir, a.set, a.trainer_dir)}")
    elif a.cmd == "score":
        res = score(a.data_dir, a.set, a.trainer_dir)
        print((Path(a.trainer_dir) / f"CERTIFICATION_{a.set}.md").read_text())
        if res["key_unconfirmed"]:
            sys.exit(2)


if __name__ == "__main__":
    main()
