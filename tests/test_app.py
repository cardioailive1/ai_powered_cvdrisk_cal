import pytest

from cardioai_core.equations import cha2ds2_vasc, charge_af, pooled_cohort_equations  # noqa: E402
from training.imaging_nlp import extract_cac, extract_lvef  # noqa: E402

BASE = dict(age=55, total_chol=213, hdl=50, sbp=120, smoking="never", diabetes="none")


# --- Published worked example (Goff 2014, Table A): 2.1%, 5.3%, 3.0%, 6.1% -------
@pytest.mark.parametrize("race,sex,expected", [("white", "female", 2.1), ("white", "male", 5.3),
                                                ("black", "female", 3.0), ("black", "male", 6.1)])
def test_pce_reference_values(race, sex, expected):
    r = pooled_cohort_equations({**BASE, "race": race, "sex": sex})
    assert abs(r["risk"] * 100 - expected) < 0.15


def test_pce_not_for_secondary_prevention():
    assert not pooled_cohort_equations({**BASE, "race": "white", "sex": "male", "prior_mi": True})["applicable"]


def test_cha2ds2_vasc_score():
    r = cha2ds2_vasc({"afib": True, "age": 76, "sex": "female", "hypertension": True, "diabetes": "type2", "sbp": 130})
    assert r["score"] == 5  # age>=75 (2) + female (1) + HTN (1) + DM (1)


def test_charge_af_monotonic_in_age():
    p = dict(height_cm=175, weight_kg=80, sbp=130, dbp=80, race="white")
    assert charge_af({**p, "age": 75})["risk"] > charge_af({**p, "age": 55})["risk"]


def test_imaging_nlp():
    assert extract_cac("Total coronary calcium score: 412 (Agatston).") == 412
    assert extract_cac("No coronary artery calcification is identified.") == 0
    assert extract_lvef("The LVEF is estimated at 30-35%.") == 32.5
    assert extract_lvef("Left ventricular ejection fraction 55 %.") == 55


# --- API (sign-in flow and RBAC are covered in test_auth.py) ---------------------
@pytest.fixture(scope="module")
def client(app_started):
    return app_started[1]


@pytest.fixture(scope="module")
def auth(admin):
    return admin


def test_predict_requires_auth(client):
    assert client.post("/api/predict", json={"calculator": "ami", "patient": {}}).status_code == 401


PATIENT = dict(age=62, sex="male", race="white", height_cm=178, weight_kg=92, sbp=142, dbp=86, smoking="current",
               diabetes="type2", total_chol=230, hdl=38, ldl=150, trig=210, egfr=70, hypertension=True)


@pytest.mark.parametrize("calc", ["ami", "stroke", "hf", "scd", "af"])
def test_predict_all_calculators(auth, calc):
    r = auth.post("/api/predict", json={"calculator": calc, "patient": PATIENT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["primary"] and 0 < body["primary"]["risk"] < 1
    risks = [h["risk"] for h in body["endpoints"][0]["horizons"].values()]
    assert risks == sorted(risks), "cumulative risk must not fall with longer horizon"
    assert body["recommendations"]


def test_higher_risk_profile_scores_higher(auth):
    low = dict(PATIENT, age=40, smoking="never", diabetes="none", sbp=115, total_chol=170, hdl=60, hypertension=False)
    r_low = auth.post("/api/predict", json={"calculator": "ami", "patient": low}).json()
    r_high = auth.post("/api/predict", json={"calculator": "ami", "patient": PATIENT}).json()
    assert r_high["primary"]["risk"] > r_low["primary"]["risk"]


def test_af_model_not_applied_to_known_af(auth):
    r = auth.post("/api/predict", json={"calculator": "af", "patient": dict(PATIENT, afib=True)}).json()
    assert r["applicable"] is False


def test_validation_errors(auth):
    r = auth.post("/api/predict", json={"calculator": "ami", "patient": dict(PATIENT, age=7, hdl=300)})
    assert r.status_code == 422 and len(r.json()["errors"]) >= 2


def test_meta_and_models(auth):
    m = auth.get("/api/meta").json()
    assert set(m["calculators"]) == {"ami", "stroke", "hf", "scd", "af"} and m["model"]["is_demo"]
    assert auth.get("/api/models").status_code == 200
