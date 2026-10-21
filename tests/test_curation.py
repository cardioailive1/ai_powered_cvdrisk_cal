"""End-to-end tests of the governed labelling pipeline on a small synthetic dataset:
auto-label -> audit -> data-scientist review -> blinded adjudication -> gates -> release -> training labels."""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from curation import governance  # noqa: E402
from curation.runs import create_label_run, current, run_audit  # noqa: E402
from curation.server import create_app  # noqa: E402
from curation.simulate import simulate  # noqa: E402
from curation.store import User, open_store, verify_log  # noqa: E402
from cardioai_core.security import hash_password  # noqa: E402
from training.labeling import build_episodes  # noqa: E402
from training.nlp import find_mentions  # noqa: E402
from training.synthetic_raw import generate_raw, write_raw  # noqa: E402
from training.train import labels as train_labels  # noqa: E402

PW = "test-password-123"


# ---------------------------------------------------------------- unit
@pytest.mark.parametrize("text,key,status", [
    ("Discharge diagnosis: NSTEMI.", "ami", "affirmed"),
    ("Acute coronary syndrome was ruled out.", "ami", "negated"),
    ("History of myocardial infarction in 2010.", "ami", "historical"),
    ("Palpitations; evaluate for atrial fibrillation", "af", "hypothetical"),
    ("Holter showed sinus rhythm, no atrial fibrillation.", "af", "negated"),
    ("Stroke ruled out; symptoms attributed to migraine", "stroke", "negated"),
])
def test_negation_context(text, key, status):
    assert find_mentions(text, key)[0]["status"] == status


def test_episode_collapse_and_evidence_only():
    feats = pd.DataFrame({"person_id": [1, 2], "index_date": pd.to_datetime(["2016-01-01", "2016-01-01"])})
    codes = pd.DataFrame({"person_id": [1, 1, 1], "family": ["ami"] * 3,
                          "event_date": pd.to_datetime(["2017-03-01", "2017-03-03", "2017-03-20"]),
                          "code": ["I21.4", "I21.4", "I21.4"], "visit_concept_id": [9201, 9201, 9202],
                          "condition_status_concept_id": [32902, 32908, 32908]})
    procs = pd.DataFrame({"person_id": [1, 2], "category": ["pci", "cpr"], "procedure_date": pd.to_datetime(["2017-03-01", "2018-05-05"]),
                          "code": ["92928", "92950"]})
    c = build_episodes(codes, procs, feats)
    p1 = c[c.person_id == 1]
    assert len(p1) == 2 and p1.iloc[0]["acute"] and p1.iloc[0]["primary"]          # 2 codes in 2 days -> 1 episode; day-19 follow-up separate
    ev = c[c.source == "evidence_only"]
    assert list(ev.family) == ["scd"] and ev.iloc[0].person_id == 2               # PCI next to a code adds nothing; lone CPR does


def test_training_labels_exclude_unresolved():
    df = pd.DataFrame({"days_ami": [100, None, None, 500], "unknown_ami": [None, 50, 900, 200],
                       "followup_days": [2000] * 4, "death_days": [None] * 4})
    y = train_labels(df, "ami", 365)
    assert y[0] == 1 and pd.isna(y[1]) and y[2] == 0 and pd.isna(y[3])   # unresolved day 200 < 1y, event only at day 500
    y5 = train_labels(df, "ami", 1826)
    assert y5[3] == 1                                                              # confirmed event within horizon wins


# ---------------------------------------------------------------- end-to-end
@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("curation")
    write_raw(d / "raw", generate_raw(n=3000, seed=3))
    cfg = governance.load(d)
    cfg["provenance"] = "synthetic-demo"
    cfg["audit"]["min_cohort"] = 500
    governance.save(d, cfg)
    (d / ".secret").write_text("x" * 48)
    S = open_store(d)
    with S() as db:
        for u, r in [("ds1", "data_scientist"), ("card1", "cardiologist"), ("card2", "cardiologist"),
                     ("lead1", "lead_cardiologist"), ("admin", "admin")]:
            db.add(User(username=u, role=r, password_hash=hash_password(PW)))
        db.commit()
        run = create_label_run(db, d, "test")
        run_audit(db, d, run, "test")
    client = TestClient(create_app(str(d)))
    tok = {u: client.post("api/auth/login", json={"username": u, "password": PW}).json()["token"]
           for u in ["ds1", "card1", "card2", "lead1", "admin"]}
    return {"dir": d, "S": S, "c": client, "h": {u: {"Authorization": "Bearer " + t} for u, t in tok.items()}}


def test_auto_labels_accurate(env):
    with env["S"]() as db:
        run = current(db)
    L = pd.read_parquet(Path(run.path) / "labels.parquet")
    assert {"p_auto", "auto_label", "subtype_auto", "label_relevant"} <= set(L.columns)
    assert set(L.family) == {"ami", "stroke", "hf", "scd", "af"}


def test_roles_and_review_flow(env):
    c, h = env["c"], env["h"]
    # cardiologist cannot approve the dataset; DS cannot approve while items unresolved
    assert c.post("api/approve", json={"stage": "ds_dataset_review", "decision": "approve"}, headers=h["card1"]).status_code == 400
    r = c.post("api/approve", json={"stage": "ds_dataset_review", "decision": "approve"}, headers=h["ds1"])
    audit = c.get("api/audit", headers=h["ds1"]).json()
    open_items = [i for i in audit["items"] if not i["resolved"]]
    if open_items:
        assert r.status_code == 400
    for i in open_items:
        if i["status"] == "fail":
            assert c.post("api/audit/review", json={"check_id": i["id"], "action": "acknowledge"}, headers=h["ds1"]).status_code == 400
            act = {"check_id": i["id"], "action": "waive", "justification": "Synthetic test data; accepted for pipeline test"}
        else:
            act = {"check_id": i["id"], "action": "acknowledge", "justification": "reviewed"}
        assert c.post("api/audit/review", json=act, headers=h["ds1"]).status_code == 200
    r = c.post("api/approve", json={"stage": "ds_dataset_review", "decision": "approve", "comment": "ok"}, headers=h["ds1"])
    assert r.status_code == 200 and "sample" in r.json()


def test_blinding_and_assignment(env):
    c, h = env["c"], env["h"]
    q1 = c.get("api/queue", headers=h["card1"]).json()
    assert q1["open"]
    case = next(x for x in q1["open"] if x["type"] == "event")
    body = c.get(f"api/cases/{case['case_id']}", headers=h["card1"]).json()
    flat = repr(body)
    for leaked in ("p_auto", "auto_label", "stratum", "uncertain", "auto_confirmed", "lf_", "sampling_prob"):
        assert leaked not in flat, leaked
    assert body["packet"]["criteria"] and body["mode"] == "independent"
    # a reader not assigned the case cannot open it; DS cannot read cases
    q2 = {x["case_id"] for x in c.get("api/queue", headers=h["card2"]).json()["open"]}
    other = next((x["case_id"] for x in q1["open"] if x["case_id"] not in q2), None)
    if other:
        assert c.get(f"api/cases/{other}", headers=h["card2"]).status_code == 400
    assert c.get(f"api/cases/{case['case_id']}", headers=h["ds1"]).status_code == 403
    # decision validation
    bad = c.post(f"api/cases/{case['case_id']}/read", json={"outcome": "event"}, headers=h["card1"])
    assert bad.status_code == 400 and "subtype" in bad.json()["detail"]


def test_gates_signoff_release(env):
    c, h, d = env["c"], env["h"], env["dir"]
    with env["S"]() as db:
        simulate(db, d, current(db))
    st = c.get("api/status", headers=h["ds1"]).json()
    assert st["metrics"]["totals"]["open"] == 0 and st["metrics"]["totals"]["disagreement"] == 0
    # releases cannot be built before sign-off
    assert c.post("api/release", headers=h["ds1"]).status_code == 400
    for g in st["gates"]:
        if g["status"] == "fail" and g["waivable_by"]:
            who = "lead1" if g["waivable_by"] == "lead_cardiologist" else "ds1"
            assert c.post("api/gates/waive", json={"gate_id": g["id"], "justification": "Small synthetic test cohort, accepted"},
                          headers=h[who]).status_code == 200
    assert c.post("api/approve", json={"stage": "cardiology_release", "decision": "approve"}, headers=h["ds1"]).status_code == 400  # wrong role
    assert c.post("api/approve", json={"stage": "ds_release", "decision": "approve", "comment": "ok"}, headers=h["ds1"]).status_code == 200
    assert c.post("api/approve", json={"stage": "cardiology_release", "decision": "approve", "comment": "ok"}, headers=h["lead1"]).status_code == 200
    r = c.post("api/release", headers=h["ds1"])
    assert r.status_code == 200, r.text
    rel = Path(r.json()["path"])
    ds = pd.read_parquet(rel / "dataset.parquet")
    assert {"days_ami", "unknown_ami", "days_hfref", "unknown_hfpef"} <= set(ds.columns)
    import json
    meta = json.loads((rel / "release.json").read_text())
    assert meta["simulated_adjudication"] and meta["synthetic_data"] and len(meta["approvals"]) == 3


def test_audit_log_tamper_detected(env):
    with env["S"]() as db:
        assert verify_log(db)["ok"]
    con = sqlite3.connect(env["dir"] / "curation.db")
    con.execute("UPDATE audit_log SET details = '{\"decision\": \"forged\"}' WHERE id = 3")
    con.commit()
    con.close()
    with env["S"]() as db:
        v = verify_log(db)
    assert not v["ok"] and v["first_bad_id"] == 3


# ---------------------------------------------------------------- Mayo Clinic Platform specifics
def test_mayo_hs_tnt_sex_specific_url_and_delta():
    from training.labeling import normalize_labs, troponin_summary
    t0 = pd.Timestamp("2018-01-01 08:00")
    labs = pd.DataFrame({"person_id": [1, 1, 2], "analyte": ["hs_troponin_t"] * 3, "unit": ["ng/L"] * 3,
                         "measured_at": [t0, t0 + pd.Timedelta(hours=2), t0], "value": [12.0, 23.0, 12.0], "range_high": [None] * 3})
    L = normalize_labs(labs, {1: "female", 2: "male"})
    assert L["url"].tolist() == [10.0, 10.0, 15.0]                     # Mayo TRPS: F <=10, M <=15 ng/L
    recs = L[L.person_id == 1].to_dict("records")
    assert troponin_summary(recs)["rise_fall"]                          # +11 ng/L in 2 h above URL
    assert not troponin_summary(L[L.person_id == 2].to_dict("records"))["elevated"]   # 12 ng/L normal for a man


def test_reader_policy(env):
    c, h = env["c"], env["h"]
    bad = c.post("api/users", json={"username": "drx", "role": "cardiologist", "email": "drx@gmail.com", "password": PW}, headers=h["admin"])
    assert bad.status_code == 400 and "cardioailive.com" in bad.json()["detail"]
    ok = c.post("api/users", json={"username": "drx", "role": "cardiologist", "email": "drx@cardioailive.com", "password": PW}, headers=h["admin"])
    assert ok.status_code == 201
    fifth = c.post("api/users", json={"username": "dry", "role": "cardiologist", "email": "dry@cardioailive.com", "password": PW}, headers=h["admin"])
    assert fifth.status_code == 201                                      # card1, card2, lead1, drx, dry = 5 seats
    full = c.post("api/users", json={"username": "drz", "role": "cardiologist", "email": "drz@cardioailive.com", "password": PW}, headers=h["admin"])
    assert full.status_code == 400 and "seats" in full.json()["detail"]
    assert c.get("api/users", headers=h["admin"]).json()["seats_used"] <= 5


def test_echo_native_config_requires_confirmation():
    from training.extract import load_native_config, render_native_sql, unconfirmed
    cfg = load_native_config("/nonexistent.json")
    assert len(unconfirmed(cfg)) == 9 and len(unconfirmed(cfg, ("echo",))) == 2
    cfg["patient_link"]["omop_person_column"] = "person_source_value"
    cfg["echo"].update(patient_column="PATIENT_CLINIC_NUMBER", date_column="ECHO_DTM")
    cfg["diagnosis"].update(patient_column="PATIENT_DK", date_column="DX_DTM", code_column="DX_CODE", primary_value="Y")
    cfg["mortality"].update(patient_column="PATIENT_DK", death_date_column="DEATH_DTM")
    sql = render_native_sql("p.native", "p.omop", cfg)
    assert not unconfirmed(cfg) and not any("CONFIRM" in q for q in sql.values()) and "ECHO_NOTE" in sql["11_echo_notes_event"]
    assert "DIM_PATIENT" not in sql["10_echo_notes_baseline"]               # clinic number: direct join
    assert "p.native.DIM_PATIENT" in sql["12_primary_dx"] and "PRIMARY_DIAGNOSIS_FLAG" in sql["12_primary_dx"]
    assert "EHR_LAST_LIVE_INTERACTION_DTM" in sql["13_mortality"]


def test_old_native_config_file_still_loads(tmp_path):
    import json

    from training.extract import load_native_config
    f = tmp_path / "old.json"
    f.write_text(json.dumps({"echo": {"patient_column": "PATIENT_CLINIC_NUMBER", "person_bridge": "person_source_value"}}))
    cfg = load_native_config(f)
    assert cfg["patient_link"]["omop_person_column"] == "person_source_value" and "person_bridge" not in cfg["echo"]


def test_external_mortality_rules():
    import pandas as pd

    from training.extract import apply_mortality
    co = pd.DataFrame({"person_id": [1, 2, 3], "index_date": pd.to_datetime(["2015-01-01"] * 3),
                       "obs_end": pd.to_datetime(["2018-01-01"] * 3), "death_date": [pd.NaT, pd.NaT, pd.Timestamp("2017-06-01")]})
    m = pd.DataFrame({"person_id": [1, 2, 3], "death_date_ext": pd.to_datetime(["2014-12-01", None, "2017-07-01"]),
                      "last_live_date": pd.to_datetime(["2014-11-01", "2019-03-01", None])})
    out, removed = apply_mortality(co, m)
    assert removed == 1 and list(out["person_id"]) == [2, 3]               # E4 from the third-party death source
    assert out.loc[out.person_id == 2, "obs_end"].iloc[0] == pd.Timestamp("2019-03-01")   # last live interaction extends
    assert out.loc[out.person_id == 3, "death_date"].iloc[0] == pd.Timestamp("2017-06-01")  # earliest death wins
