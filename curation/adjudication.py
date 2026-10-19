"""Cardiologist adjudication: sampling, blinded independent reads, lead
adjudication of disagreements, consensus, and agreement/accuracy metrics.

Sampling design (per outcome family, label-relevant candidates only):
  * uncertain       — ALL uncertain auto-labels (up to a cap) -> become the label
  * auto_confirmed  — simple random sample -> PPV of auto-confirmed labels
  * auto_rejected   — simple random sample -> NPV of auto-rejected labels
  * annotation      — random imaging reports -> accuracy of LVEF / CAC extraction
Event cases get two independent reads (blinded to the auto-label, the
sampling stratum and each other). Disagreements go to a lead cardiologist who
was not one of the two readers and who sees both reads.
"""
from __future__ import annotations

import datetime as dt
import math
import secrets
from collections import Counter

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cardioai_core.schema import FAMILIES

from .store import Case, LabelRun, Read, User, log_event, utcnow

OUTCOMES = ("event", "not_event", "indeterminate")
CONFIDENCE = ("low", "medium", "high")


class AdjudicationError(ValueError):
    pass


# ------------------------------------------------------------------ sampling
def _readers(db: Session) -> tuple[list[User], list[User]]:
    users = list(db.scalars(select(User).where(User.active, User.role.in_(["cardiologist", "lead_cardiologist"]))))
    return [u for u in users if u.role == "cardiologist"], [u for u in users if u.role == "lead_cardiologist"]


def create_sample(db: Session, run: LabelRun, labels: pd.DataFrame, baseline_notes: pd.DataFrame, gov: dict, username: str,
                  commit: bool = True) -> dict:
    if db.scalar(select(func.count()).select_from(Case).where(Case.label_run_id == run.id)):
        raise AdjudicationError("An adjudication sample already exists for this label run.")
    cards, leads = _readers(db)
    pool = cards if len(cards) >= 2 else cards + leads
    if not pool:
        raise AdjudicationError("No active cardiologist accounts. Add cardiologists before approving the dataset.")
    S = gov["sampling"]
    rng = np.random.default_rng(S["seed"])
    load: Counter = Counter({u.id: 0 for u in pool})
    summary: dict = {}

    def assign(n: int) -> list[int]:
        chosen = sorted(pool, key=lambda u: (load[u.id], rng.random()))[:n]
        for u in chosen:
            load[u.id] += 1
        return [u.id for u in chosen]

    rel = labels[labels["label_relevant"]] if "label_relevant" in labels else labels
    for fam in FAMILIES:
        g = rel[rel["family"] == fam]
        plan = [("uncertain", g[g["auto_label"] == "uncertain"], S["max_uncertain_per_family"]),
                ("auto_confirmed", g[g["auto_label"] == "confirmed"], S["audit_sample_per_stratum"]),
                ("auto_rejected", g[g["auto_label"] == "rejected"], S["audit_sample_per_stratum"])]
        summary[fam] = {}
        for stratum, frame, cap in plan:
            k = min(cap, len(frame))
            if k == 0:
                summary[fam][stratum] = {"population": int(len(frame)), "sampled": 0}
                continue
            pick = frame.sample(n=k, random_state=int(rng.integers(0, 2**31 - 1)))
            for cid in pick["candidate_id"]:
                double = rng.random() < S["double_read_fraction"] and len(pool) >= 2
                case = Case(id="ADJ-" + secrets.token_hex(4).upper(), label_run_id=run.id, case_type="event", ref_id=cid,
                            family=fam, stratum=stratum, sampling_prob=k / len(frame), reads_required=2 if double else 1)
                db.add(case)
                for rid in assign(case.reads_required):
                    db.add(Read(case_id=case.id, reader_id=rid, kind="independent"))
            summary[fam][stratum] = {"population": int(len(frame)), "sampled": int(k)}

    bn = baseline_notes
    if len(bn):
        from training.imaging_nlp import extract_cac, extract_lvef
        hit = bn["note_text"].map(lambda t: extract_lvef(t) is not None or extract_cac(t) is not None)
        k = S["annotation_sample"]
        k_hit, k_miss = min(int(k * 0.8), int(hit.sum())), min(k - int(k * 0.8), int((~hit).sum()))
        # stratified: reports with and without an extracted value (misses are over-sampled on purpose;
        # metrics re-weight by 1/sampling probability so accuracy reflects all reports)
        p_hit, p_miss = k_hit / max(int(hit.sum()), 1), k_miss / max(int((~hit).sum()), 1)
        parts = [bn[hit].sample(n=k_hit, random_state=S["seed"]).assign(samp_p=p_hit),
                 bn[~hit].sample(n=k_miss, random_state=S["seed"]).assign(samp_p=p_miss)]
        for r in pd.concat(parts).itertuples():
            case = Case(id="ANN-" + secrets.token_hex(4).upper(), label_run_id=run.id, case_type="annotation",
                        ref_id=str(r.note_id), family="imaging", stratum="annotation", sampling_prob=float(r.samp_p), reads_required=1)
            db.add(case)
            db.add(Read(case_id=case.id, reader_id=assign(1)[0], kind="independent"))
        summary["annotation"] = {"sampled": k_hit + k_miss, "with_extraction": k_hit, "without_extraction": k_miss}
    summary["readers"] = {u.username: load[u.id] for u in pool}
    log_event(db, username, "adjudication.sample_created", run.id, summary)
    if commit:
        db.commit()
    return summary


# ------------------------------------------------------------------ decisions
def validate_decision(case: Case, d: dict) -> dict:
    if case.case_type == "annotation":
        out = {}
        for fld in ("lvef", "cac_score"):
            v = (d.get(fld) or {}).get("value")
            if v in ("", None):
                out[fld] = {"value": None}
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise AdjudicationError(f"{fld}: enter a number or leave blank if not reported")
            lim = (5, 85) if fld == "lvef" else (0, 10000)
            if not lim[0] <= v <= lim[1]:
                raise AdjudicationError(f"{fld} must be between {lim[0]} and {lim[1]}")
            out[fld] = {"value": v}
        out["comment"] = str(d.get("comment", ""))[:2000]
        return out
    fam = FAMILIES[case.family]
    outcome = d.get("outcome")
    if outcome not in OUTCOMES:
        raise AdjudicationError("Choose an outcome: event, not an event, or cannot determine.")
    out = {"outcome": outcome, "confidence": d.get("confidence") if d.get("confidence") in CONFIDENCE else "medium",
           "criteria": [c for c in (d.get("criteria") or []) if c in fam["criteria"]],
           "comment": str(d.get("comment", ""))[:2000], "subtype": None, "event_date": None}
    if outcome == "event":
        st = d.get("subtype")
        if st not in fam["subtypes"]:
            raise AdjudicationError("Choose a subtype for a confirmed event.")
        out["subtype"] = st
        try:
            out["event_date"] = dt.date.fromisoformat(str(d.get("event_date"))).isoformat()
        except ValueError:
            raise AdjudicationError("Enter the event date (YYYY-MM-DD).")
        if not out["criteria"]:
            raise AdjudicationError("Tick at least one definition criterion supporting the event.")
    if outcome == "indeterminate" and not out["comment"].strip():
        raise AdjudicationError("Say what information is missing when the outcome cannot be determined.")
    return out


def agree(a: dict, b: dict, gov: dict) -> bool:
    if a["outcome"] != b["outcome"]:
        return False
    if a["outcome"] != "event":
        return True
    if a["subtype"] != b["subtype"] and "unknown" not in (a["subtype"], b["subtype"]) and "unspecified" not in (a["subtype"], b["subtype"]):
        return False
    gap = abs((dt.date.fromisoformat(a["event_date"]) - dt.date.fromisoformat(b["event_date"])).days)
    return gap <= gov["definitions"]["date_agreement_days"]


def _merge(a: dict, b: dict) -> dict:
    if a["outcome"] != "event":
        return {"outcome": a["outcome"], "subtype": None, "event_date": None, "method": "agreement"}
    vague = {"unknown", "unspecified"}
    st = a["subtype"] if a["subtype"] not in vague else b["subtype"]
    return {"outcome": "event", "subtype": st, "event_date": min(a["event_date"], b["event_date"]), "method": "agreement"}


def _update_status(db: Session, case: Case, gov: dict, username: str) -> None:
    reads = list(db.scalars(select(Read).where(Read.case_id == case.id)))
    ind = [r for r in reads if r.kind == "independent"]
    lead = [r for r in reads if r.kind == "lead"]
    if any(r.submitted_at is None for r in ind):
        return
    if case.case_type == "annotation" or case.reads_required == 1:
        dec = ind[0].decision
        case.final = {**({k: dec[k] for k in ("lvef", "cac_score")} if case.case_type == "annotation" else
                         {k: dec[k] for k in ("outcome", "subtype", "event_date")}), "method": "single_read"}
        case.status = "final"
    elif agree(ind[0].decision, ind[1].decision, gov):
        case.final = _merge(ind[0].decision, ind[1].decision)
        case.status = "final"
    elif lead and lead[0].submitted_at is not None:
        dec = lead[0].decision
        case.final = {"outcome": dec["outcome"], "subtype": dec["subtype"], "event_date": dec["event_date"], "method": "lead_adjudication"}
        case.status = "final"
    else:
        case.status = "disagreement"
        if not lead:
            db.add(Read(case_id=case.id, reader_id=None, kind="lead"))
            log_event(db, "system", "adjudication.disagreement", case.id, {"family": case.family})
    if case.status == "final":
        log_event(db, username, "adjudication.case_final", case.id, {"method": case.final["method"], "family": case.family})


def submit_read(db: Session, case: Case, user: User, decision: dict, gov: dict) -> Case:
    if case.status == "final":
        raise AdjudicationError("This case is already final.")
    reads = list(db.scalars(select(Read).where(Read.case_id == case.id)))
    mine = next((r for r in reads if r.kind == "independent" and r.reader_id == user.id), None)
    if mine is None and case.status == "disagreement":
        if user.role != "lead_cardiologist":
            raise AdjudicationError("Only a lead cardiologist can resolve a disagreement.")
        if any(r.kind == "independent" and r.reader_id == user.id for r in reads):
            raise AdjudicationError("You read this case independently; another lead cardiologist must resolve it.")
        mine = next(r for r in reads if r.kind == "lead")
        mine.reader_id = user.id
    if mine is None:
        raise AdjudicationError("This case is not assigned to you.")
    if mine.submitted_at is not None:
        raise AdjudicationError("You have already submitted this read.")
    mine.decision = validate_decision(case, decision)
    mine.submitted_at = utcnow()
    log_event(db, user.username, f"adjudication.read_{mine.kind}", case.id, {"family": case.family})
    db.flush()
    _update_status(db, case, gov, user.username)
    db.commit()
    return case


def queue_for(db: Session, run_id: str, user: User) -> dict:
    mine = db.execute(select(Read, Case).join(Case, Case.id == Read.case_id)
                      .where(Case.label_run_id == run_id, Read.reader_id == user.id, Read.kind == "independent")).all()
    open_ = [{"case_id": c.id, "type": c.case_type, "family": c.family} for r, c in mine if r.submitted_at is None]
    done = sum(1 for r, _ in mine if r.submitted_at is not None)
    disagreements = []
    if user.role == "lead_cardiologist":
        rows = db.execute(select(Read, Case).join(Case, Case.id == Read.case_id)
                          .where(Case.label_run_id == run_id, Read.kind == "lead", Read.submitted_at.is_(None))).all()
        mine_ids = {c.id for _, c in mine}
        disagreements = [{"case_id": c.id, "type": c.case_type, "family": c.family} for _, c in rows if c.id not in mine_ids]
    return {"open": open_, "completed": done, "disagreements": disagreements}


def reader_view(db: Session, case: Case, user: User) -> dict:
    reads = list(db.scalars(select(Read).where(Read.case_id == case.id).order_by(Read.id)))
    mine = next((r for r in reads if r.reader_id == user.id), None)
    is_lead_task = case.status == "disagreement" and user.role == "lead_cardiologist" and not (mine and mine.kind == "independent")
    if not mine and not is_lead_task:
        raise AdjudicationError("This case is not assigned to you.")
    view = {"case_id": case.id, "case_type": case.case_type, "family": case.family, "status": case.status,
            "my_read": mine.decision if mine and mine.submitted_at else None, "mode": "lead" if is_lead_task else "independent"}
    if is_lead_task:   # lead sees both independent reads, anonymised
        view["independent_reads"] = [{"reader": f"Reader {chr(65 + i)}", **r.decision}
                                     for i, r in enumerate([r for r in reads if r.kind == "independent"])]
    return view


# ------------------------------------------------------------------ metrics
def wilson(k: float, n: float, z: float = 1.96) -> list[float] | None:
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [max(0.0, c - h), min(1.0, c + h)]


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    if not a:
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(a) | set(b)) / (n * n)
    return 1.0 if pe >= 1 else (po - pe) / (1 - pe)


def _ann_correct(field: str, extracted, truth) -> bool:
    if extracted is None or truth is None:
        return extracted is None and truth is None
    tol = 3.0 if field == "lvef" else max(1.0, 0.05 * truth)
    return abs(extracted - truth) <= tol


def metrics(db: Session, run_id: str, labels: pd.DataFrame | None = None, annotation_extracted: dict | None = None) -> dict:
    cases = list(db.scalars(select(Case).where(Case.label_run_id == run_id)))
    reads = {}
    for r in db.scalars(select(Read).join(Case, Case.id == Read.case_id).where(Case.label_run_id == run_id)):
        reads.setdefault(r.case_id, []).append(r)
    sub_auto = labels.set_index("candidate_id")["subtype_auto"].to_dict() if labels is not None else {}
    out: dict = {"families": {}, "annotation": {}, "totals": {}}
    for fam in FAMILIES:
        fc = [c for c in cases if c.case_type == "event" and c.family == fam]
        a, b = [], []
        for c in fc:
            ind = [r for r in reads.get(c.id, []) if r.kind == "independent" and r.submitted_at]
            if len(ind) == 2:
                a.append(ind[0].decision["outcome"])
                b.append(ind[1].decision["outcome"])
        strata = {}
        for st in ("uncertain", "auto_confirmed", "auto_rejected"):
            sc = [c for c in fc if c.stratum == st]
            fin = [c.final["outcome"] for c in sc if c.status == "final"]
            cnt = Counter(fin)
            strata[st] = {"cases": len(sc), "final": len(fin), "event": cnt["event"], "not_event": cnt["not_event"],
                          "indeterminate": cnt["indeterminate"]}
        conf, rej = strata["auto_confirmed"], strata["auto_rejected"]
        nc, nr = conf["event"] + conf["not_event"], rej["event"] + rej["not_event"]
        finals = [c for c in fc if c.status == "final"]
        ev_final = [c for c in finals if c.final["outcome"] == "event"]
        sub_ok = [sub_auto.get(c.ref_id) == c.final["subtype"] for c in ev_final if c.ref_id in sub_auto]
        out["families"][fam] = {
            "cases": len(fc), "final": len(finals), "open": sum(c.status == "open" for c in fc),
            "disagreement": sum(c.status == "disagreement" for c in fc),
            "lead_resolved": sum(1 for c in finals if c.final.get("method") == "lead_adjudication"),
            "double_reads": len(a), "kappa": cohen_kappa(a, b), "raw_agreement": (sum(x == y for x, y in zip(a, b)) / len(a)) if a else None,
            "ppv_auto_confirmed": conf["event"] / nc if nc else None, "ppv_ci": wilson(conf["event"], nc),
            "npv_auto_rejected": rej["not_event"] / nr if nr else None, "npv_ci": wilson(rej["not_event"], nr),
            "indeterminate_fraction": (sum(c.final["outcome"] == "indeterminate" for c in finals) / len(finals)) if finals else None,
            "auto_subtype_accuracy": (sum(sub_ok) / len(sub_ok)) if sub_ok else None,
            "strata": strata,
        }
    ann = [c for c in cases if c.case_type == "annotation"]
    for fld in ("lvef", "cac_score"):
        n, w_ok, w_all, w_sq = 0, 0.0, 0.0, 0.0
        for c in ann:
            if c.status != "final":
                continue
            truth = c.final[fld]["value"]
            ext = (annotation_extracted or {}).get(c.ref_id, {}).get(fld)
            if truth is None and ext is None:
                continue
            w = 1.0 / max(c.sampling_prob, 1e-9)
            n += 1
            w_all += w
            w_sq += w * w
            w_ok += w * _ann_correct(fld, ext, truth)
        acc = w_ok / w_all if w_all else None
        n_eff = (w_all * w_all / w_sq) if w_sq else 0.0     # Kish effective sample size
        out["annotation"][fld] = {"evaluated": n, "accuracy": acc, "n_effective": round(n_eff, 1),
                                  "ci": wilson(acc * n_eff, n_eff) if acc is not None else None,
                                  "weighting": "inverse sampling probability"}
    out["annotation"]["cases"] = len(ann)
    out["annotation"]["final"] = sum(c.status == "final" for c in ann)
    out["totals"] = {"cases": len(cases), "final": sum(c.status == "final" for c in cases),
                     "open": sum(c.status == "open" for c in cases), "disagreement": sum(c.status == "disagreement" for c in cases)}
    return out
