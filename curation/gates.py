"""Release gates. A release (and therefore training on Mayo data) is only
possible when every gate is `pass` or explicitly `waived` by the role allowed
to waive it, with a written justification recorded in the audit trail."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cardioai_core.schema import FAMILIES

from . import governance
from .store import Approval, Case, LabelRun, Review, User, verify_log


def _reviews(db: Session, run_id: str) -> dict[str, Review]:
    """Latest review per item (a later 'revoke' cancels an earlier acknowledge/waive)."""
    latest: dict[str, Review] = {}
    for r in db.scalars(select(Review).where(Review.label_run_id == run_id).order_by(Review.id)):
        latest[r.item_id] = r
    return {k: v for k, v in latest.items() if v.action != "revoke"}


def audit_items(db: Session, run_id: str, report: dict | None) -> list[dict]:
    rev = _reviews(db, run_id)
    items = []
    for c in (report or {}).get("checks", []):
        r = rev.get(f"audit:{c['id']}")
        resolved = c["status"] == "pass" or (r is not None and (r.action == "waive" or (c["status"] == "warn" and r.action == "acknowledge")))
        items.append({**c, "review": None if r is None else {"action": r.action, "justification": r.justification,
                                                              "user_id": r.user_id, "at": r.created_at.isoformat()},
                      "resolved": resolved})
    return items


def approval(db: Session, run_id: str, stage: str) -> Approval | None:
    return db.scalar(select(Approval).where(Approval.label_run_id == run_id, Approval.stage == stage)
                     .order_by(Approval.created_at.desc()).limit(1))


def evaluate(db: Session, data_dir, run: LabelRun, report: dict | None, m: dict | None) -> list[dict]:
    gov = governance.load(data_dir)
    G = gov["gates"]
    rev = _reviews(db, run.id)
    gates: list[dict] = []

    def add(gid, title, status, detail, waivable_by=None):
        r = rev.get(f"gate:{gid}")
        if status == "fail" and waivable_by and r is not None and r.action == "waive":
            status, detail = "waived", f"{detail} — waived: {r.justification}"
        gates.append({"id": gid, "title": title, "status": status, "detail": detail, "waivable_by": waivable_by})

    # 1. audit
    if report is None:
        add("audit", "Automatic audit completed and reviewed", "pending",
            "Required step after automatic labelling: run the automatic audit before any review")
    else:
        items = audit_items(db, run.id, report)
        unresolved = [i["id"] for i in items if not i["resolved"]]
        add("audit", "Automatic audit completed and reviewed", "pass" if not unresolved else "pending",
            "All warnings acknowledged and failures waived" if not unresolved else f"Unresolved: {', '.join(unresolved)}")
    # 2. data-scientist dataset review
    a = approval(db, run.id, "ds_dataset_review")
    add("ds_dataset_review", "Data scientist approved dataset for adjudication",
        "pass" if a and a.decision == "approve" else ("fail" if a else "pending"),
        "Not yet reviewed" if not a else f"{a.decision} by user #{a.user_id}: {a.comment}")
    # 3. adjudication progress
    n_cases = len(list(db.scalars(select(Case.id).where(Case.label_run_id == run.id))))
    if m is None or n_cases == 0:
        add("adjudication_complete", "All adjudication cases final", "pending", "No adjudication sample yet")
    else:
        t = m["totals"]
        add("adjudication_complete", "All adjudication cases final", "pass" if t["open"] + t["disagreement"] == 0 else "pending",
            f"{t['final']}/{t['cases']} final, {t['disagreement']} awaiting lead adjudication")
        for fam in FAMILIES:
            f = m["families"][fam]
            if f["cases"] == 0:
                continue
            name = FAMILIES[fam]["name"]
            done = f["open"] + f["disagreement"] == 0
            k = f["kappa"]
            if f["double_reads"] < G["min_double_reads_for_kappa"]:
                add(f"kappa_{fam}", f"{name}: inter-reader agreement", "fail" if done else "pending",
                    f"Only {f['double_reads']} double-read cases (need ≥{G['min_double_reads_for_kappa']})", "lead_cardiologist")
            else:
                add(f"kappa_{fam}", f"{name}: inter-reader agreement",
                    ("pass" if k >= G["min_kappa"] else "fail") if done else "pending",
                    f"Cohen's κ = {k:.2f} on {f['double_reads']} cases (min {G['min_kappa']})", "lead_cardiologist")
            for key, lab, thr in (("ppv_auto_confirmed", "PPV of auto-confirmed labels", G["min_ppv_auto_confirmed"]),
                                  ("npv_auto_rejected", "NPV of auto-rejected labels", G["min_npv_auto_rejected"])):
                v, ci = f[key], f[key.split("_")[0] + "_ci"]
                if v is None:
                    add(f"{key}_{fam}", f"{name}: {lab}", "pass" if done else "pending", "No cases in this stratum")
                else:
                    add(f"{key}_{fam}", f"{name}: {lab}", ("pass" if v >= thr else "fail") if done else "pending",
                        f"{v:.1%} (95% CI {ci[0]:.1%}–{ci[1]:.1%}), min {thr:.0%}", "lead_cardiologist")
            ind = f["indeterminate_fraction"]
            if ind is not None:
                add(f"indeterminate_{fam}", f"{name}: cases that could not be determined",
                    ("pass" if ind <= G["max_indeterminate_fraction"] else "fail") if done else "pending",
                    f"{ind:.1%} indeterminate (max {G['max_indeterminate_fraction']:.0%}) — these patients are excluded for this outcome",
                    "lead_cardiologist")
        for fld, lab in (("lvef", "LVEF"), ("cac_score", "Coronary calcium")):
            an = m["annotation"].get(fld, {})
            if an.get("evaluated"):
                acc = an["accuracy"]
                done = m["annotation"]["final"] == m["annotation"]["cases"]
                add(f"annotation_{fld}", f"{lab} extraction accuracy", ("pass" if acc >= G["min_annotation_accuracy"] else "fail") if done else "pending",
                    f"{acc:.1%} on {an['evaluated']} reports (min {G['min_annotation_accuracy']:.0%})", "data_scientist")
    # 4. audit-trail integrity
    v = verify_log(db)
    add("audit_trail", "Audit trail intact", "pass" if v["ok"] else "fail",
        f"{v['entries']} entries verified" if v["ok"] else f"Hash chain broken at entry {v['first_bad_id']}")
    # 5. sign-offs (two different people)
    ds = approval(db, run.id, "ds_release")
    cl = approval(db, run.id, "cardiology_release")
    pre_ok = all(g["status"] in ("pass", "waived") for g in gates)
    for stage, title, appr in (("ds_release", "Data scientist release sign-off", ds), ("cardiology_release", "Lead cardiologist release sign-off", cl)):
        if appr and appr.decision == "approve":
            add(stage, title, "pass", f"Signed by user #{appr.user_id}")
        elif appr:
            add(stage, title, "fail", f"Rejected: {appr.comment}")
        else:
            add(stage, title, "pending", "Available once all gates above pass" if not pre_ok else "Awaiting sign-off")
    if ds and cl and ds.decision == cl.decision == "approve" and ds.user_id == cl.user_id:
        gates[-1]["status"] = "fail"
        gates[-1]["detail"] = "Release sign-offs must come from two different people"
    return gates


def ready_for_signoff(gates: list[dict]) -> bool:
    return all(g["status"] in ("pass", "waived") for g in gates if g["id"] not in ("ds_release", "cardiology_release"))


def releasable(gates: list[dict]) -> bool:
    return all(g["status"] in ("pass", "waived") for g in gates)


def waivable(gates: list[dict], gate_id: str, user: User) -> bool:
    g = next((g for g in gates if g["id"] == gate_id), None)
    # Only the accountable clinical/technical role may waive; admins cannot.
    return bool(g and g["status"] == "fail" and g["waivable_by"] and user.role == g["waivable_by"])
