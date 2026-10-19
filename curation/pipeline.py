"""Curation pipeline CLI (run inside the Mayo Clinic Platform workspace).

    python -m curation.pipeline init --data-dir data --provenance mayo-clinic-platform
    python -m curation.pipeline user-add jsmith --role cardiologist --full-name "J Smith"   (prompts for password)
    python -m curation.pipeline label   --data-dir data          # 1. automatic labelling + annotation
    python -m curation.pipeline audit   --data-dir data          # 2. automatic audit
    python -m curation.server           --data-dir data          # 3-5. review, adjudication, sign-off in the workbench
    python -m curation.pipeline status  --data-dir data
    python -m curation.pipeline release --data-dir data --as dsmith   # 6. build the locked release (gates enforced)
    python -m training.train --release data/releases/<REL-id> --out bundles/<name>

Actions that need an identity (approve, review, waive, release) take --as USER
and read the password from $CURATION_PASSWORD or prompt for it.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select

from cardioai_core.security import hash_password, verify_password

from . import actions, governance
from .runs import create_label_run, current, latest_audit, run_audit
from .store import ROLES, Case, User, open_store, verify_log


def _db(data_dir):
    return open_store(data_dir)()


def _login(db, username: str) -> User:
    u = db.scalar(select(User).where(User.username == username))
    pw = os.environ.get("CURATION_PASSWORD") or getpass.getpass(f"Password for {username}: ")
    if not u or not u.active or not verify_password(pw, u.password_hash):
        sys.exit("Authentication failed")
    return u


def _run(db):
    r = current(db)
    if not r:
        sys.exit("No active label run — run `label` first")
    return r


def _sysuser() -> str:
    try:
        return "cli:" + getpass.getuser()
    except Exception:
        return "cli"


def cmd_init(a):
    d = Path(a.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    cfg = governance.load(d)
    cfg["provenance"] = a.provenance
    governance.save(d, cfg)
    sec = d / ".secret"
    if not sec.exists():
        sec.write_text(secrets.token_urlsafe(48))
        sec.chmod(0o600)
    open_store(d)
    print(f"Initialised {d} (provenance={a.provenance}). Edit {governance.path(d)} to change thresholds before labelling.")


def cmd_user_add(a):
    if a.role not in ROLES:
        sys.exit(f"role must be one of {ROLES}")
    db = _db(a.data_dir)
    if db.scalar(select(User).where(User.username == a.username)):
        sys.exit("User exists")
    from .store import check_reader_policy
    check_reader_policy(db, governance.load(a.data_dir), a.role, a.email or "")
    pw = a.password or os.environ.get("NEW_USER_PASSWORD") or getpass.getpass("New password (12+ chars): ")
    if len(pw) < 12:
        sys.exit("Password must be at least 12 characters")
    db.add(User(username=a.username, full_name=a.full_name or "", email=a.email or "", role=a.role, password_hash=hash_password(pw)))
    from .store import log_event
    log_event(db, _sysuser(), "user.created", a.username, {"role": a.role})
    db.commit()
    print(f"Created {a.role} '{a.username}'")


def cmd_label(a):
    db = _db(a.data_dir)
    run = create_label_run(db, a.data_dir, _sysuser(), a.label_model)
    print(json.dumps({"label_run": run.id, **run.summary}, indent=2, default=str))


def cmd_audit(a):
    db = _db(a.data_dir)
    ar = run_audit(db, a.data_dir, _run(db), _sysuser())
    print(f"Audit {ar.id}: {ar.overall.upper()} {ar.report['counts']}")
    for c in ar.report["checks"]:
        if c["status"] != "pass":
            print(f"  [{c['status'].upper():4}] {c['id']}: {c['title']} — {c['value']} ({c['detail']})")


def cmd_status(a):
    db = _db(a.data_dir)
    run = _run(db)
    print(f"Label run {run.id} ({run.label_model_version})")
    for g in actions.current_gates(db, a.data_dir, run):
        print(f"  [{g['status'].upper():7}] {g['title']} — {g['detail']}")


def cmd_review(a):
    db = _db(a.data_dir)
    actions.review_audit_item(db, _run(db), _login(db, a.as_user), a.check, a.action, a.justification or "")
    print("Recorded")


def cmd_approve(a):
    db = _db(a.data_dir)
    res = actions.approve(db, a.data_dir, _run(db), _login(db, a.as_user), a.stage, a.decision, a.comment or "")
    print(json.dumps(res or {"recorded": True}, indent=2))


def cmd_waive(a):
    db = _db(a.data_dir)
    actions.waive_gate(db, a.data_dir, _run(db), _login(db, a.as_user), a.gate, a.justification)
    print("Waiver recorded")


def cmd_simulate(a):
    from .simulate import simulate
    db = _db(a.data_dir)
    print(simulate(db, a.data_dir, _run(db), a.reader_accuracy))


def cmd_release(a):
    from .release import build_release
    db = _db(a.data_dir)
    rel = build_release(db, a.data_dir, _run(db), _login(db, a.as_user))
    print(f"Release {rel.id} written to {rel.path}\n  dataset sha256 {rel.dataset_sha}\n"
          f"Next: python -m training.train --release {rel.path} --out bundles/<name>")


def plan_workload(summary: dict, gov: dict, readers: int) -> dict:
    """Expected adjudication workload for a label run (before or after approval)."""
    S, P = gov["sampling"], gov["reader_policy"]
    rel = summary.get("relevant_auto", {})
    fam_rows, cases = {}, 0
    for fam, a in rel.items():
        n = (min(S["max_uncertain_per_family"], a.get("uncertain", 0)) + min(S["audit_sample_per_stratum"], a.get("confirmed", 0))
             + min(S["audit_sample_per_stratum"], a.get("rejected", 0)))
        fam_rows[fam] = n
        cases += n
    double = S["double_read_fraction"] if readers >= 2 else 0.0
    ind_reads = cases * (1 + double)
    lead_reads = cases * double * P["expected_disagreement_rate"]
    ann = S["annotation_sample"]
    per_reader = (ind_reads + ann) / max(readers, 1)
    hours = (per_reader - ann / max(readers, 1)) * P["minutes_per_event_case"] / 60 + (ann / max(readers, 1)) * P["minutes_per_annotation_case"] / 60
    return {"event_cases_by_family": fam_rows, "event_cases": cases, "independent_reads": round(ind_reads),
            "annotation_reads": ann, "expected_lead_reads": round(lead_reads), "readers": readers,
            "reads_per_reader": round(per_reader), "hours_per_reader": round(hours, 1),
            "lead_hours": round(lead_reads * P["minutes_per_event_case"] * 1.5 / 60, 1),
            "seats_needed": readers + 1, "seats_available": P["max_reader_seats"],
            "assumptions": f"{P['minutes_per_event_case']} min/event case, {P['minutes_per_annotation_case']} min/report, "
                           f"{P['expected_disagreement_rate']:.0%} of double reads disagree, lead case 1.5x longer"}


def cmd_workload(a):
    db = _db(a.data_dir)
    gov = governance.load(a.data_dir)
    run = _run(db)
    max_r = gov["reader_policy"]["max_reader_seats"] - 1          # one seat for the lead cardiologist
    for r in sorted({2, 3, max_r} | ({a.readers} if a.readers else set())):
        if r < 1:
            continue
        w = plan_workload(run.summary, gov, r)
        print(f"{r} independent readers + 1 lead ({w['seats_needed']}/{w['seats_available']} seats): "
              f"{w['event_cases']} event cases, {w['reads_per_reader']} reads ≈ {w['hours_per_reader']} h each; "
              f"lead ≈ {w['expected_lead_reads']} cases ≈ {w['lead_hours']} h")
    print("Assumptions:", w["assumptions"], "| by family:", w["event_cases_by_family"])


def cmd_verify(a):
    v = verify_log(_db(a.data_dir))
    print(json.dumps(v, indent=2))
    sys.exit(0 if v["ok"] else 1)


def cmd_refit(a):
    """Propose label-model weights learned from cardiologist decisions (does not change the active run)."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold

    from training.labeling import LFS, score
    db = _db(a.data_dir)
    run = _run(db)
    labels = pd.read_parquet(Path(run.path) / "labels.parquet").set_index("candidate_id")
    model = json.loads((Path(run.path) / "label_model.json").read_text())
    new = json.loads(json.dumps(model))
    new["version"] = f"refit-from-{run.id}"
    report = {}
    cases = [c for c in db.scalars(select(Case).where(Case.label_run_id == run.id, Case.case_type == "event", Case.status == "final"))
             if c.final["outcome"] in ("event", "not_event")]
    for fam, fm in model["families"].items():
        cs = [c for c in cases if c.family == fam and c.ref_id in labels.index]
        y = np.array([c.final["outcome"] == "event" for c in cs], dtype=int)
        if len(cs) < 40 or y.min() == y.max() or min(y.sum(), len(y) - y.sum()) < 10:
            report[fam] = {"status": "skipped", "reason": f"{len(cs)} adjudicated cases, {int(y.sum())} events"}
            continue
        names = [lf.name for lf in LFS if fam in lf.families and lf.name in fm["lfs"]]
        V = labels.loc[[c.ref_id for c in cs], [f"lf_{n}" for n in names]].to_numpy()
        X = np.hstack([(V > 0).astype(float), (V < 0).astype(float)])
        w = np.array([1 / c.sampling_prob for c in cs])
        old_p = np.array([score(model, fam, dict(zip(names, row))) for row in V])
        cv_p = np.zeros(len(y))
        for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(X, y):
            cv_p[te] = LogisticRegression(C=1.0, max_iter=2000).fit(X[tr], y[tr], sample_weight=w[tr]).predict_proba(X[te])[:, 1]
        lr = LogisticRegression(C=1.0, max_iter=2000).fit(X, y, sample_weight=w)
        k = len(names)
        new["families"][fam] = {"bias": float(lr.intercept_[0]),
                                "lfs": {n: [round(max(0.0, float(lr.coef_[0, i])), 3), round(max(0.0, -float(lr.coef_[0, k + i])), 3)]
                                        for i, n in enumerate(names)}}
        report[fam] = {"status": "refit", "cases": len(cs), "events": int(y.sum()),
                       "auc_current_model": float(roc_auc_score(y, old_p, sample_weight=w)),
                       "auc_refit_cv": float(roc_auc_score(y, cv_p, sample_weight=w))}
    Path(a.out).write_text(json.dumps(new, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nProposed label model written to {a.out}. To use it: `label --label-model {a.out}` "
          "(creates a NEW label run that needs its own audit, review and adjudication sample).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.environ.get("CURATION_DATA_DIR", "data"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--provenance", default="mayo-clinic-platform", choices=["mayo-clinic-platform", "synthetic-demo", "other"])
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("user-add")
    p.add_argument("username")
    p.add_argument("--role", required=True)
    p.add_argument("--full-name")
    p.add_argument("--email", help="required for cardiologists: must be an @cardioailive.com workspace account")
    p.add_argument("--password", help="avoid on shared machines; prefer the prompt or $NEW_USER_PASSWORD")
    p.set_defaults(fn=cmd_user_add)
    p = sub.add_parser("label")
    p.add_argument("--label-model", help="JSON label model (default: expert-v1)")
    p.set_defaults(fn=cmd_label)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("plan-workload", help="estimate reads and hours per cardiologist for the active label run")
    p.add_argument("--readers", type=int)
    p.set_defaults(fn=cmd_workload)
    p = sub.add_parser("review")
    p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--check", required=True)
    p.add_argument("--action", required=True, choices=["acknowledge", "waive", "revoke"])
    p.add_argument("--justification")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("approve")
    p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--stage", required=True, choices=["ds_dataset_review", "ds_release", "cardiology_release"])
    p.add_argument("--decision", default="approve", choices=["approve", "reject"])
    p.add_argument("--comment")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("waive")
    p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--gate", required=True)
    p.add_argument("--justification", required=True)
    p.set_defaults(fn=cmd_waive)
    p = sub.add_parser("simulate-adjudication", help="SYNTHETIC DATA ONLY")
    p.add_argument("--reader-accuracy", type=float, default=0.93)
    p.set_defaults(fn=cmd_simulate)
    p = sub.add_parser("release")
    p.add_argument("--as", dest="as_user", required=True)
    p.set_defaults(fn=cmd_release)
    sub.add_parser("verify-log").set_defaults(fn=cmd_verify)
    p = sub.add_parser("refit-label-model")
    p.add_argument("--out", default="label_model_refit.json")
    p.set_defaults(fn=cmd_refit)
    a = ap.parse_args()
    try:
        a.fn(a)
    except (actions.ActionError, PermissionError, ValueError, RuntimeError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
