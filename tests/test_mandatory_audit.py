"""The automatic audit is a required step: it runs right after automatic labelling, and no data scientist review,
cardiologist adjudication or release sign-off can happen for a label run that has no current audit."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from cardioai_core.security import hash_password  # noqa: E402
from curation import actions, gates as gates_mod, governance, runs  # noqa: E402
from curation.adjudication import AdjudicationError, reader_view, submit_read  # noqa: E402
from curation.store import Case, LogEntry, User, open_store  # noqa: E402
from training.synthetic_raw import generate_raw, write_raw  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    d = tmp_path / "data"
    write_raw(d / "raw", generate_raw(n=2500, seed=11))
    cfg = governance.load(d)
    cfg["provenance"] = "synthetic-demo"
    cfg["audit"]["min_cohort"] = 500
    governance.save(d, cfg)
    (d / ".secret").write_text("x" * 48)
    db = open_store(d)()
    for u, r in [("ds1", "data_scientist"), ("card1", "cardiologist"), ("card2", "cardiologist"), ("lead1", "lead_cardiologist")]:
        db.add(User(username=u, role=r, password_hash=hash_password("pw-123456789")))
    db.commit()
    return d, db


def _user(db, name):
    return db.scalar(select(User).where(User.username == name))


def _resolve_audit(db, run):
    ds = _user(db, "ds1")
    for c in runs.latest_audit(db, run.id).report["checks"]:
        if c["status"] == "warn":
            actions.review_audit_item(db, run, ds, c["id"], "acknowledge", "Reviewed the detail table for this check.")
        elif c["status"] == "fail":
            actions.review_audit_item(db, run, ds, c["id"], "waive", "Synthetic test cohort; accepted for this test only.")


def test_labelling_runs_the_audit_immediately(env):
    d, db = env
    run = runs.create_label_run(db, d, "test")
    ar = runs.latest_audit(db, run.id)
    assert ar is not None and ar.report["inputs_sha"] == run.inputs_sha and runs.audit_problem(db, run, d) is None
    events = [e.action for e in db.scalars(select(LogEntry).order_by(LogEntry.id))]
    assert events.index("label_run.created") < events.index("audit.completed")


def test_no_review_without_audit(env, monkeypatch):
    d, db = env

    def crashed(*a, **k):
        raise RuntimeError("audit crashed")
    monkeypatch.setattr(runs, "run_audit", crashed)
    with pytest.raises(RuntimeError):
        runs.create_label_run(db, d, "test")       # labels written, audit missing
    monkeypatch.undo()
    run = runs.current(db)
    assert runs.latest_audit(db, run.id) is None

    ds = _user(db, "ds1")
    with pytest.raises(actions.ActionError, match="required step"):
        actions.approve(db, d, run, ds, "ds_dataset_review", "approve", "looks fine")
    with pytest.raises(actions.ActionError, match="required step"):
        actions.review_audit_item(db, run, ds, "units", "acknowledge", "")
    with pytest.raises(actions.ActionError, match="required step"):
        actions.approve(db, d, run, _user(db, "lead1"), "cardiology_release", "approve", "")
    gate = next(g for g in actions.current_gates(db, d, run) if g["id"] == "audit")
    assert gate["status"] == "pending" and gate["waivable_by"] is None
    assert not db.scalars(select(Case).where(Case.label_run_id == run.id)).first()

    runs.run_audit(db, d, run, "test")              # the required step, then the reviews can start
    _resolve_audit(db, run)
    res = actions.approve(db, d, run, ds, "ds_dataset_review", "approve", "Audit resolved; approved for adjudication.")
    assert res["sample"]


def test_cardiologists_blocked_without_audit(env, monkeypatch):
    d, db = env
    run = runs.create_label_run(db, d, "test")
    _resolve_audit(db, run)
    actions.approve(db, d, run, _user(db, "ds1"), "ds_dataset_review", "approve", "Audit resolved; approved for adjudication.")
    case = db.scalars(select(Case).where(Case.label_run_id == run.id, Case.case_type == "event")).first()
    reader = next(u for u in (_user(db, "card1"), _user(db, "card2"))
                  if not isinstance(_try_view(db, case, u), AdjudicationError))

    monkeypatch.setattr(runs, "latest_audit", lambda *a, **k: None)   # audit record unavailable
    with pytest.raises(AdjudicationError, match="required step"):
        reader_view(db, case, reader)
    with pytest.raises(AdjudicationError, match="required step"):
        submit_read(db, case, reader, {"outcome": "not_event", "confidence": "high"}, governance.load(d))


def _try_view(db, case, u):
    try:
        return reader_view(db, case, u)
    except AdjudicationError as e:
        return e


def test_audit_refused_when_data_changed_after_labelling(env):
    d, db = env
    run = runs.create_label_run(db, d, "test")
    f = d / "raw" / "features.parquet"
    df = pd.read_parquet(f)
    df.iloc[1:].to_parquet(f, index=False)          # re-extracted data, same label run
    with pytest.raises(runs.SequenceError, match="label"):
        runs.run_audit(db, d, run, "test")
    assert "changed" in runs.audit_problem(db, run, d)
    with pytest.raises(actions.ActionError, match="changed"):
        actions.approve(db, d, run, _user(db, "ds1"), "ds_dataset_review", "approve", "looks fine")
    assert gates_mod.approval(db, run.id, "ds_dataset_review") is None
