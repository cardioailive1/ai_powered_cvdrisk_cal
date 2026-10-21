"""The training program's kit, end to end on a small synthetic environment: setup, trainees, the Module 2 explainer,
the planted-fault audit exercise and its grading, practice / calibration sets read through the real workbench API,
seeded disagreements for the lead, key confirmation and certification scoring."""
import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from curation import actions, training_kit as kit  # noqa: E402
from curation.runs import current, latest_audit  # noqa: E402
from curation.server import create_app  # noqa: E402
from curation.store import User, open_store, verify_log  # noqa: E402

ROSTER = [("ds.trainee", "data_scientist"), ("lead.trainee", "lead_cardiologist"), ("reader1", "cardiologist"), ("reader2", "cardiologist")]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp("kit")
    d, t = base / "env", base / "trainer"
    kit.setup(d, t, n=6000, seed=3)
    roster = base / "roster.csv"
    with roster.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["username", "full_name", "email", "role"])
        for u, r in ROSTER:
            w.writerow([u, u.title(), f"{u}@cardioailive.com", r])
    pw = {r["username"]: r["one_time_password"] for r in kit.add_trainees(d, str(roster))}
    return {"base": base, "d": d, "t": t, "pw": pw}


def test_refuses_non_training_folder(tmp_path):
    (tmp_path / "raw").mkdir()
    with pytest.raises(SystemExit):
        kit.make_set(tmp_path, "practice", tmp_path.parent / "keys")
    with pytest.raises(SystemExit):
        kit.repair(tmp_path, "units", tmp_path.parent / "keys")


def test_trainer_dir_must_be_outside(env):
    with pytest.raises(SystemExit):
        kit.make_set(env["d"], "practice", env["d"] / "keys")


def test_explain_shows_votes(env):
    rows = kit.explain(env["d"], near_threshold=1)
    assert rows and all("votes" in r and 0 <= r["p_auto"] <= 1 for r in rows)
    assert "truth" not in str(rows)


def test_planted_fault_exercise(env):
    f, t = env["base"] / "faults", env["t"]
    kit.plant_faults(env["d"], f, t)
    S = open_store(f)
    db = S()
    from curation.runs import create_label_run, run_audit
    run = create_label_run(db, f, "ds.trainee")
    checks = {c["id"]: c["status"] for c in run_audit(db, f, run, "ds.trainee").report["checks"]}
    assert checks["units"] == "fail" and checks["evidence_tables"] == "fail" and checks["events_after_death"] == "fail"
    with pytest.raises(SystemExit):
        kit.repair(f, "missingness", t)               # not an extraction fault: acknowledge or waive instead
    g = kit.grade_faults(f, t)
    assert not g["pass"] and set(g["planted_faults_seen_in_first_audit"]) >= {"units", "evidence_tables", "events_after_death"}

    for c in ("units", "evidence_tables", "events_after_death"):
        kit.repair(f, c, t)
    run = create_label_run(db, f, "ds.trainee")
    rep = run_audit(db, f, run, "ds.trainee").report
    assert not [c for c in rep["checks"] if c["status"] == "fail"]
    ds = db.scalar(select(User).where(User.username == "ds.trainee"))
    for c in rep["checks"]:
        if c["status"] == "warn":
            actions.review_audit_item(db, run, ds, c["id"], "acknowledge", "Expected in this synthetic cohort; reviewed the detail table.")
    actions.approve(db, f, run, ds, "ds_dataset_review", "approve", "Faults fixed and re-extracted; warnings reviewed.")
    g = kit.grade_faults(f, t)
    assert g["pass"] and all(g["fixed"].values()) and g["dataset_approved"]


def _login(c, user, pw):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _answer(c, h, case_id, key, right=True):
    v = c.get(f"/api/cases/{case_id}", headers=h).json()
    if v["case_type"] == "annotation":
        body = {"lvef": {"value": key["key_lvef"] or None}, "cac_score": {"value": key["key_cac_score"] or None}}
    elif key["key_outcome"] == "event" and right:
        body = {"outcome": "event", "subtype": key["key_subtype"], "event_date": key["key_event_date"],
                "criteria": [next(iter(v["packet"]["criteria"]))] if "packet" in v else ["trop_rise_fall"],
                "confidence": "high", "comment": ""}
    else:
        out = key["key_outcome"] if right else ("not_event" if key["key_outcome"] == "event" else "indeterminate")
        body = {"outcome": out, "confidence": "medium", "comment": "Not enough information in the record to decide."}
    r = c.post(f"/api/cases/{case_id}/read", json=body, headers=h)
    assert r.status_code == 200, r.text


def test_practice_calibration_and_certification(env):
    d, t, pw = env["d"], env["t"], env["pw"]
    res = kit.make_set(d, "calibration", t)
    assert res["cases_per_trainee"] == 5 * 8 + 5 and set(res["trainees"]) == {"lead.trainee", "reader1", "reader2"}
    with pytest.raises(SystemExit):
        kit.make_set(d, "calibration", t)              # one calibration set per environment
    keys = {k["ref_id"]: k for k in csv.DictReader((t / "calibration_key.csv").open())}
    assert sum(k["key_outcome"] == "event" for k in keys.values()) >= 15
    cases = list(csv.DictReader((t / "calibration_cases.csv").open()))
    review = kit.key_packets(d, "calibration", t)
    assert "Key:" in review.read_text()

    app = create_app(str(d))
    c = TestClient(app)
    for user, right in (("reader1", True), ("reader2", False)):
        h = _login(c, user, pw[user])
        q = c.get("/api/queue", headers=h).json()
        assert len(q["open"]) == len(keys)                  # every trainee reads every case, in the real workbench
        bad = 0
        for row in [r for r in cases if r["username"] == user]:
            k = keys[row["ref_id"]]
            wrong = not right and k["case_type"] == "event" and bad < 12
            bad += wrong
            _answer(c, h, row["case_id"], k, right=not wrong)

    s = kit.score(d, "calibration", t)
    assert s["key_unconfirmed"] == len(keys)                # draft key until the lead confirms it
    r1, r2 = s["trainees"]["reader1"], s["trainees"]["reader2"]
    assert r1["complete"] and r1["overall_agreement"] == 1.0 and r1["kappa"] == 1.0 and r1["pass"]
    assert r2["complete"] and r2["overall_agreement"] < 0.85 and not r2["pass"] and r2["discordant"]
    assert (t / "discordant_calibration_reader2.md").exists() and "RETAKE" in (t / "CERTIFICATION_calibration.md").read_text()
    assert s["trainees"].get("lead.trainee", {"complete": False})["complete"] is False

    rows = list(csv.DictReader((t / "calibration_key.csv").open()))
    for r in rows:
        r["confirmed_by"] = "lead.trainee"
    with (t / "calibration_key.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert kit.score(d, "calibration", t)["key_unconfirmed"] == 0


def test_seeded_disagreements_for_lead(env):
    d, t, pw = env["d"], env["t"], env["pw"]
    kit.make_set(d, "disagreements", t)
    keys = {k["ref_id"]: k for k in csv.DictReader((t / "disagreements_key.csv").open())}
    c = TestClient(create_app(str(d)))
    h = _login(c, "lead.trainee", pw["lead.trainee"])
    q = c.get("/api/queue", headers=h).json()
    assert len(q["disagreements"]) == len(keys) == 10
    cases = {r["case_id"]: r for r in csv.DictReader((t / "disagreements_cases.csv").open())}
    for item in q["disagreements"]:
        v = c.get(f"/api/cases/{item['case_id']}", headers=h).json()
        assert v["mode"] == "lead" and len(v["independent_reads"]) == 2
        k = keys[cases[item["case_id"]]["ref_id"]]
        body = ({"outcome": "event", "subtype": k["key_subtype"], "event_date": k["key_event_date"], "criteria": [],
                 "confidence": "high"} if k["key_outcome"] == "event" else {"outcome": "not_event", "confidence": "high"})
        if body["outcome"] == "event":
            from cardioai_core.schema import FAMILIES
            body["criteria"] = [next(iter(FAMILIES[k["family"]]["criteria"]))]
        body["comment"] = "Reader B dated the event from the follow-up visit; definition requires the acute episode."
        r = c.post(f"/api/cases/{item['case_id']}/read", json=body, headers=h)
        assert r.status_code == 200, r.text
    res = kit.score(d, "disagreements", t)["trainees"]["lead.trainee"]
    assert res["complete"] and res["overall_agreement"] == 1.0 and res["rationale_given"] == 10 and res["pass"]


def test_audit_trail_records_training(env):
    db = open_store(env["d"])()
    assert verify_log(db)["ok"]
    assert current(db) is not None and latest_audit(db, current(db).id) is not None
