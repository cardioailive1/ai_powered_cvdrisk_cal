"""Simulated cardiologist readers — FOR PIPELINE TESTING ON SYNTHETIC DATA ONLY.

Refuses to run unless the raw data folder carries the SYNTHETIC marker written
by training.synthetic_raw. Readers answer from the hidden ground truth with
configurable error rates, so kappa, PPV/NPV and the gates can be exercised.
The label run and any release built from it are permanently flagged
`simulated_adjudication: true`.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from cardioai_core.schema import FAMILIES

from . import governance
from .adjudication import submit_read
from .runs import get_run_data, is_synthetic, raw_dir
from .store import Case, LabelRun, Read, User, log_event


def simulate(db: Session, data_dir, run: LabelRun, reader_accuracy: float = 0.93, lead_accuracy: float = 0.97, seed: int = 5,
             max_rounds: int = 3) -> dict:
    if not is_synthetic(data_dir):
        raise PermissionError("Refusing to simulate adjudication: data is not marked SYNTHETIC.")
    rng = np.random.default_rng(seed)
    gov = governance.load(data_dir)
    rd = get_run_data(data_dir, run)
    truth = pd.read_parquet(raw_dir(data_dir) / "truth.parquet")
    truth["date"] = pd.to_datetime(truth["date"])
    tby = {k: v for k, v in truth.groupby(["person_id", "family"])}
    ann_truth = pd.read_parquet(raw_dir(data_dir) / "truth_annotations.parquet")
    ann_truth["note_id"] = ann_truth["note_id"].astype(str)
    ann_truth = ann_truth.set_index("note_id").to_dict("index")

    def true_event(cid: str) -> dict:
        c = rd.by_id.loc[cid]
        t = tby.get((c["person_id"], c["family"]))
        if t is not None:
            gap = (t["date"] - c["event_date"]).abs().dt.days
            if gap.min() <= 7:
                r = t.loc[gap.idxmin()]
                return {"event": bool(r["event"]), "subtype": r["subtype"], "date": r["date"]}
        return {"event": False, "subtype": None, "date": None}

    def decide(case: Case, acc: float) -> dict:
        if case.case_type == "annotation":
            tr = ann_truth.get(case.ref_id, {})
            out = {}
            for fld, key in (("lvef", "lvef_true"), ("cac_score", "cac_true")):
                v = tr.get(key)
                v = None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
                if v is not None and rng.random() > 0.98:
                    v = v + (5 if fld == "lvef" else 50)
                out[fld] = {"value": v}
            return out
        t = true_event(case.ref_id)
        fam = FAMILIES[case.family]
        outcome = "event" if t["event"] else "not_event"
        if rng.random() > acc:
            outcome = "indeterminate" if rng.random() < 0.2 else ("not_event" if outcome == "event" else "event")
        d = {"outcome": outcome, "confidence": "medium", "criteria": [], "comment": "simulated read"}
        if outcome == "event":
            st = t["subtype"] if t["subtype"] in fam["subtypes"] else "unknown" if "unknown" in fam["subtypes"] else "unspecified"
            if rng.random() > 0.95:
                st = rng.choice([s for s in fam["subtypes"] if s != st])
            date = (t["date"] if t["date"] is not None else rd.by_id.loc[case.ref_id, "event_date"]) + pd.Timedelta(days=int(rng.integers(-1, 2)))
            d.update(subtype=str(st), event_date=pd.Timestamp(date).date().isoformat(), criteria=[next(iter(fam["criteria"]))])
        return d

    users = {u.id: u for u in db.scalars(select(User))}
    leads = [u for u in users.values() if u.role == "lead_cardiologist" and u.active]
    n_ind = n_lead = 0
    for _ in range(max_rounds):
        pending = db.execute(select(Read, Case).join(Case, Case.id == Read.case_id)
                             .where(Case.label_run_id == run.id, Read.submitted_at.is_(None))).all()
        if not pending:
            break
        for r, case in pending:
            if r.kind == "independent":
                submit_read(db, case, users[r.reader_id], decide(case, reader_accuracy), gov)
                n_ind += 1
            else:
                readers = {x.reader_id for x in db.scalars(select(Read).where(Read.case_id == case.id, Read.kind == "independent"))}
                lead = next((u for u in leads if u.id not in readers), None)
                if lead is None:
                    continue
                submit_read(db, case, lead, decide(case, lead_accuracy), gov)
                n_lead += 1
    s = dict(run.summary)
    s["simulated_adjudication"] = True
    run.summary = s
    log_event(db, "SIMULATOR", "adjudication.simulated", run.id,
              {"independent_reads": n_ind, "lead_reads": n_lead, "reader_accuracy": reader_accuracy,
               "at": dt.datetime.now(dt.timezone.utc).isoformat()})
    db.commit()
    return {"independent_reads": n_ind, "lead_reads": n_lead}
