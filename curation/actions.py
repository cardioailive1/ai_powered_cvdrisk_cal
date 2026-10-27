"""Role-checked actions shared by the web workbench and the CLI."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import gates as gates_mod
from . import governance
from .adjudication import create_sample, metrics
from .runs import audit_problem, get_run_data, latest_audit, load_raw
from .store import Approval, Case, LabelRun, Review, User, log_event


class ActionError(PermissionError):
    pass


def _need(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise ActionError(f"This action requires role: {' or '.join(roles)}")


def run_metrics(db: Session, data_dir, run: LabelRun) -> dict:
    labels = pd.read_parquet(Path(run.path) / "labels.parquet", columns=["candidate_id", "subtype_auto"])
    rd = get_run_data(data_dir, run)
    ann_ids = [c.ref_id for c in db.scalars(select(Case).where(Case.label_run_id == run.id, Case.case_type == "annotation"))]
    ann = {nid: rd.annotation_packet(nid)["extracted"] for nid in ann_ids}
    return metrics(db, run.id, labels, ann)


def current_gates(db: Session, data_dir, run: LabelRun) -> list[dict]:
    ar = latest_audit(db, run.id)
    return gates_mod.evaluate(db, data_dir, run, ar.report if ar else None, run_metrics(db, data_dir, run))


def review_audit_item(db: Session, run: LabelRun, user: User, check_id: str, action: str, justification: str) -> None:
    _need(user, "data_scientist")
    if p := audit_problem(db, run):
        raise ActionError(p)
    ar = latest_audit(db, run.id)
    check = next((c for c in ar.report["checks"] if c["id"] == check_id), None)
    if not check:
        raise ActionError(f"Unknown audit check {check_id}")
    if action not in ("acknowledge", "waive", "revoke"):
        raise ActionError("action must be acknowledge, waive or revoke")
    if action == "acknowledge" and check["status"] == "fail":
        raise ActionError("Failures cannot be acknowledged — fix the data or waive with a justification")
    if action == "waive" and len(justification.strip()) < 20:
        raise ActionError("A waiver needs a justification of at least 20 characters")
    if gates_mod.approval(db, run.id, "ds_dataset_review"):
        raise ActionError("The dataset review is already decided for this label run; start a new label run to change it")
    db.add(Review(label_run_id=run.id, item_id=f"audit:{check_id}", user_id=user.id, action=action, justification=justification))
    log_event(db, user.username, f"audit.{action}", f"{run.id}:{check_id}", {"status": check["status"], "justification": justification})
    db.commit()


def waive_gate(db: Session, data_dir, run: LabelRun, user: User, gate_id: str, justification: str) -> None:
    gates = current_gates(db, data_dir, run)
    if not gates_mod.waivable(gates, gate_id, user):
        g = next((g for g in gates if g["id"] == gate_id), None)
        raise ActionError("This gate cannot be waived" + ("" if not g else f" by your role (status {g['status']}, waivable by {g['waivable_by']})"))
    if len(justification.strip()) < 20:
        raise ActionError("A waiver needs a justification of at least 20 characters")
    db.add(Review(label_run_id=run.id, item_id=f"gate:{gate_id}", user_id=user.id, action="waive", justification=justification))
    log_event(db, user.username, "gate.waive", f"{run.id}:{gate_id}", {"justification": justification})
    db.commit()


def approve(db: Session, data_dir, run: LabelRun, user: User, stage: str, decision: str, comment: str) -> dict:
    if decision not in ("approve", "reject"):
        raise ActionError("decision must be approve or reject")
    if gates_mod.approval(db, run.id, stage):
        raise ActionError("This stage already has a decision for the current label run")
    result: dict = {}
    if p := audit_problem(db, run, data_dir if stage == "ds_dataset_review" else None):
        raise ActionError(p)   # no data scientist or cardiologist decision before the automatic audit
    if stage == "ds_dataset_review":
        _need(user, "data_scientist")
        ar = latest_audit(db, run.id)
        items = gates_mod.audit_items(db, run.id, ar.report)
        if decision == "approve" and any(not i["resolved"] for i in items):
            raise ActionError("Acknowledge every warning and waive or fix every failure before approving")
        if decision == "reject" and len(comment.strip()) < 10:
            raise ActionError("Say what needs rework")
    elif stage in ("ds_release", "cardiology_release"):
        _need(user, "data_scientist" if stage == "ds_release" else "lead_cardiologist")
        gates = current_gates(db, data_dir, run)
        if decision == "approve" and not gates_mod.ready_for_signoff(gates):
            raise ActionError("All gates must pass or be waived before release sign-off")
        other = gates_mod.approval(db, run.id, "cardiology_release" if stage == "ds_release" else "ds_release")
        if other and other.user_id == user.id:
            raise ActionError("The two release sign-offs must be made by different people")
    else:
        raise ActionError(f"Unknown stage {stage}")
    try:   # approval + adjudication sample are one transaction: no approval without a sample
        db.add(Approval(label_run_id=run.id, stage=stage, user_id=user.id, decision=decision, comment=comment))
        log_event(db, user.username, f"approval.{stage}", run.id, {"decision": decision, "comment": comment})
        if stage == "ds_dataset_review" and decision == "approve":
            rd = get_run_data(data_dir, run)
            raw = load_raw(data_dir)
            result["sample"] = create_sample(db, run, rd.labels, raw["baseline_imaging_notes"], governance.load(data_dir),
                                             user.username, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result
