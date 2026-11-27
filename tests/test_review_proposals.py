"""Proposals for the data scientist audit review: evidence-based acknowledge / waive / fix, accepted or rejected
item by item; nothing is recorded until accepted."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from curation import actions, governance, runs  # noqa: E402
from curation.review_proposals import propose  # noqa: E402
from curation.store import User, open_store  # noqa: E402
from training.synthetic_raw import generate_raw, write_raw  # noqa: E402



@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    return governance.load(tmp_path_factory.mktemp("g"))


def test_hscrp_tail_is_waived_units_problem_is_not(cfg):
    """MCP run LR-20261125-180105: hsCRP 2.4% above range with median ~1.7 mg/L -> waive; weight in ounces
    (65%, median 2783) -> fix."""
    feats = pd.DataFrame({"hscrp": [0.5, 1.7, 3.0, 400.0], "weight_kg": [2783.0, 2900.0, 80.0, 2500.0]})
    chk = {"id": "plausibility", "status": "fail", "title": "Values outside clinical range", "value": 0.02358,
           "table": [{"feature": "hscrp", "n": 38835, "implausible_frac": 0.02358},
                     {"feature": "sbp", "n": 573628, "implausible_frac": 0.0084}]}
    p = propose(chk, cfg, feats)
    assert p["action"] == "waive" and "hscrp 2.4% of 38,835" in p["justification"] and "median 2.35" in p["justification"]
    chk["table"].append({"feature": "weight_kg", "n": 579748, "implausible_frac": 0.65525})
    p = propose(chk, cfg, feats)
    assert p["action"] == "fix" and "weight_kg" in p["justification"]


def test_icd_drift_smooth_vs_step(cfg):
    """MCP yearly counts (AF): 20881, 31263, 44833, 69065 for 2014-2017 -> smooth: acknowledge. A doubling only
    at 2016 -> fix."""
    def labels(counts):
        rows = [{"event_date": f"{y}-06-01", "family": "af", "source": "code"} for y, n in counts.items() for _ in range(n)]
        return pd.DataFrame(rows)
    chk = {"id": "icd_drift", "status": "warn", "title": "Candidate rate change across ICD-9 -> ICD-10", "value": 5}
    smooth = labels({2014: 209, 2015: 313, 2016: 448, 2017: 691})
    assert propose(chk, cfg, labels=smooth)["action"] == "acknowledge"
    step = labels({2014: 200, 2015: 220, 2016: 480, 2017: 520})
    assert propose(chk, cfg, labels=step)["action"] == "fix"


def test_other_failures_need_a_fix(cfg):
    p = propose({"id": "core_missing", "status": "fail", "title": "Core features missing", "value": 1, "detail": "hdl"}, cfg)
    assert p["action"] == "fix"
    w = propose({"id": "events_after_death", "status": "warn", "title": "after death", "value": 534, "detail": "0.1%"}, cfg)
    assert w["action"] == "acknowledge" and "534" in w["justification"]


def test_accept_and_reject_flow(tmp_path):
    from cardioai_core.security import hash_password
    d = tmp_path
    write_raw(d / "raw", generate_raw(n=600, seed=11))
    S = open_store(d)
    with S() as db:
        db.add(User(username="ds", role="data_scientist", password_hash=hash_password("x" * 12)))
        db.add(User(username="card", role="cardiologist", password_hash=hash_password("x" * 12)))
        db.commit()
        run = runs.create_label_run(db, d, "ds")
        ds = db.query(User).filter_by(username="ds").one()
        card = db.query(User).filter_by(username="card").one()
        props = actions.audit_proposals(db, d, run)
        ok = [p for p in props if p["action"] != "fix"]
        if not ok:
            pytest.skip("synthetic audit has no open warnings")
        with pytest.raises(actions.ActionError):
            actions.accept_proposal(db, d, run, card, ok[0]["check_id"])            # data scientists only
        first, rest = ok[0], ok[1:]
        actions.reject_proposal(db, d, run, ds, first["check_id"], "need to look at the yearly table")
        still = {p["check_id"] for p in actions.audit_proposals(db, d, run)}
        assert first["check_id"] in still                                          # rejected: stays open
        for p in rest:
            actions.accept_proposal(db, d, run, ds, p["check_id"])
        actions.accept_proposal(db, d, run, ds, first["check_id"], "Checked the yearly table myself: no step at 2015.")
        left = {p["check_id"] for p in actions.audit_proposals(db, d, run)}
        assert not (left & {p["check_id"] for p in ok})                            # all accepted ones resolved
        from curation.gates import audit_items
        from curation.runs import latest_audit
        items = {i["id"]: i for i in audit_items(db, run.id, latest_audit(db, run.id).report)}
        assert items[first["check_id"]]["review"]["justification"].startswith("Checked the yearly table myself")


def test_unexplained_warning_is_investigated(cfg):
    p = propose({"id": "lf_coverage", "status": "warn", "title": "Labelling functions that never fire", "value": 2,
                 "detail": "trop_elevated_flat"}, cfg)
    assert p["action"] == "fix" and p["justification"].startswith("Investigate")


def test_event_proposals():
    from curation.adjudication import propose_event
    row = {"auto_label": "confirmed", "p_auto": 0.97, "subtype_auto": "nstemi", "event_date": "2016-03-02",
           "lf_trop_rise_fall": 1, "lf_note_mi": 1, "lf_trop_normal": 0}
    p = propose_event(row, "ami")
    d = p["decision"]
    assert p["acceptable"] and d["outcome"] == "event" and d["subtype"] == "nstemi" and d["criteria"] == ["trop_rise_fall"]
    assert d["confidence"] == "high" and d["event_date"] == "2016-03-02"
    row2 = {"auto_label": "confirmed", "p_auto": 0.9, "subtype_auto": "x", "event_date": "2016-03-02", "lf_note_mi": 1}
    p2 = propose_event(row2, "ami")
    assert not p2["acceptable"] and p2["decision"]["subtype"] == "unspecified"          # no criterion documented
    p3 = propose_event({"auto_label": "uncertain", "p_auto": 0.3, "event_date": "2016-03-02", "lf_trop_normal": -1}, "ami")
    assert p3["decision"]["outcome"] == "not_event" and p3["decision"]["confidence"] == "low" and p3["acceptable"]
