"""Published, peer-reviewed risk equations used as a reference baseline
next to the AI models. These are NOT trained by us; they are reproduced from
the original publications and unit-tested against published worked examples.

  * Pooled Cohort Equations (Goff DC et al., Circulation 2014) — 10-yr ASCVD
  * CHARGE-AF simple model (Alonso A et al., JAHA 2013) — 5-yr incident AF
  * CHA2DS2-VASc (Lip GYH et al., Chest 2010) — stroke risk in AF
  * ACC/AHA/HRS primary-prevention ICD criteria (LVEF-based flag, not a probability)
"""
from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------- PCE -----
_PCE = {
    ("white", "female"): dict(ln_age=-29.799, ln_age_sq=4.884, ln_tc=13.540, ln_age_ln_tc=-3.114,
                              ln_hdl=-13.578, ln_age_ln_hdl=3.149, ln_tsbp=2.019, ln_age_ln_tsbp=0.0,
                              ln_usbp=1.957, ln_age_ln_usbp=0.0, smoker=7.574, ln_age_smoker=-1.665,
                              diabetes=0.661, s0=0.9665, mean=-29.18),
    ("black", "female"): dict(ln_age=17.114, ln_age_sq=0.0, ln_tc=0.940, ln_age_ln_tc=0.0,
                              ln_hdl=-18.920, ln_age_ln_hdl=4.475, ln_tsbp=29.291, ln_age_ln_tsbp=-6.432,
                              ln_usbp=27.820, ln_age_ln_usbp=-6.087, smoker=0.691, ln_age_smoker=0.0,
                              diabetes=0.874, s0=0.9533, mean=86.61),
    ("white", "male"): dict(ln_age=12.344, ln_age_sq=0.0, ln_tc=11.853, ln_age_ln_tc=-2.664,
                            ln_hdl=-7.990, ln_age_ln_hdl=1.769, ln_tsbp=1.797, ln_age_ln_tsbp=0.0,
                            ln_usbp=1.764, ln_age_ln_usbp=0.0, smoker=7.837, ln_age_smoker=-1.795,
                            diabetes=0.658, s0=0.9144, mean=61.18),
    ("black", "male"): dict(ln_age=2.469, ln_age_sq=0.0, ln_tc=0.302, ln_age_ln_tc=0.0,
                            ln_hdl=-0.307, ln_age_ln_hdl=0.0, ln_tsbp=1.916, ln_age_ln_tsbp=0.0,
                            ln_usbp=1.809, ln_age_ln_usbp=0.0, smoker=0.549, ln_age_smoker=0.0,
                            diabetes=0.645, s0=0.8954, mean=19.54),
}


def _f(p: dict, k: str) -> float | None:
    v = p.get(k)
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _b(p: dict, k: str) -> bool:
    v = p.get(k)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def pooled_cohort_equations(p: dict[str, Any]) -> dict[str, Any]:
    age, tc, hdl, sbp = _f(p, "age"), _f(p, "total_chol"), _f(p, "hdl"), _f(p, "sbp")
    base = {"id": "pce", "name": "Pooled Cohort Equations (2013 ACC/AHA)",
            "outcome": "10-year hard ASCVD (MI, CHD death, stroke)", "horizon": "10y",
            "reference": "Goff DC Jr et al. Circulation 2014;129:S49-S73"}
    if None in (age, tc, hdl, sbp):
        return {**base, "applicable": False, "reason": "Requires age, total cholesterol, HDL and systolic BP."}
    if _b(p, "prior_mi") or _b(p, "prior_stroke"):
        return {**base, "applicable": False, "reason": "Not for patients with clinical ASCVD (secondary prevention)."}
    if not 40 <= age <= 79:
        return {**base, "applicable": False, "reason": "Validated for ages 40-79 only."}
    sex = "male" if str(p.get("sex")).lower() == "male" else "female"
    race = "black" if str(p.get("race")).lower() == "black" else "white"  # others use white equations
    c = _PCE[(race, sex)]
    treated = _b(p, "antihypertensive") or _b(p, "acei_arb") or _b(p, "beta_blocker")
    smoker = 1.0 if str(p.get("smoking")).lower() == "current" else 0.0
    dm = 1.0 if str(p.get("diabetes")).lower() in {"type1", "type2"} else 0.0
    la, ltc, lhdl, lsbp = math.log(age), math.log(tc), math.log(hdl), math.log(sbp)
    s = (c["ln_age"] * la + c["ln_age_sq"] * la * la + c["ln_tc"] * ltc + c["ln_age_ln_tc"] * la * ltc
         + c["ln_hdl"] * lhdl + c["ln_age_ln_hdl"] * la * lhdl
         + (c["ln_tsbp"] * lsbp + c["ln_age_ln_tsbp"] * la * lsbp if treated
            else c["ln_usbp"] * lsbp + c["ln_age_ln_usbp"] * la * lsbp)
         + c["smoker"] * smoker + c["ln_age_smoker"] * la * smoker + c["diabetes"] * dm)
    risk = 1 - c["s0"] ** math.exp(s - c["mean"])
    risk = min(max(risk, 0.0), 1.0)
    if risk < 0.05:
        cat = "Low (<5%)"
    elif risk < 0.075:
        cat = "Borderline (5-7.4%)"
    elif risk < 0.20:
        cat = "Intermediate (7.5-19.9%)"
    else:
        cat = "High (≥20%)"
    note = None
    if str(p.get("race")).lower() not in {"white", "black"}:
        note = "PCE was derived in White and Black cohorts; the White equation is applied here."
    return {**base, "applicable": True, "risk": risk, "category": cat, "note": note}


# ------------------------------------------------------------ CHARGE-AF ----
def charge_af(p: dict[str, Any]) -> dict[str, Any]:
    base = {"id": "charge_af", "name": "CHARGE-AF (simple model)", "outcome": "5-year incident atrial fibrillation",
            "horizon": "5y", "reference": "Alonso A et al. J Am Heart Assoc 2013;2:e000102"}
    age, h, w, sbp, dbp = (_f(p, k) for k in ("age", "height_cm", "weight_kg", "sbp", "dbp"))
    if None in (age, h, w, sbp, dbp):
        return {**base, "applicable": False, "reason": "Requires age, height, weight, systolic and diastolic BP."}
    if _b(p, "afib"):
        return {**base, "applicable": False, "reason": "Patient already has atrial fibrillation."}
    if not 46 <= age <= 94:
        return {**base, "applicable": False, "reason": "Validated for ages 46-94."}
    treated = _b(p, "antihypertensive") or _b(p, "acei_arb") or _b(p, "beta_blocker")
    s = (0.508 * age / 5 + 0.465 * (str(p.get("race")).lower() == "white") + 0.248 * h / 10
         + 0.115 * w / 15 + 0.197 * sbp / 20 - 0.101 * dbp / 10
         + 0.359 * (str(p.get("smoking")).lower() == "current") + 0.349 * treated
         + 0.237 * (str(p.get("diabetes")).lower() in {"type1", "type2"})
         + 0.701 * _b(p, "heart_failure") + 0.496 * _b(p, "prior_mi"))
    risk = 1 - 0.9718412736 ** math.exp(s - 12.58156)
    cat = "Low (<2.5%)" if risk < 0.025 else ("Intermediate (2.5-5%)" if risk < 0.05 else "High (≥5%)")
    return {**base, "applicable": True, "risk": risk, "category": cat, "note": None}


# ---------------------------------------------------------- CHA2DS2-VASc ---
# Adjusted annual stroke/thromboembolism rate by score (Lip 2010; ESC 2010 AF guideline table)
_CHADS_VASC_RATE = {0: 0.0, 1: 0.013, 2: 0.022, 3: 0.032, 4: 0.040, 5: 0.067, 6: 0.098, 7: 0.096, 8: 0.067, 9: 0.152}


def cha2ds2_vasc(p: dict[str, Any]) -> dict[str, Any]:
    base = {"id": "cha2ds2_vasc", "name": "CHA₂DS₂-VASc", "outcome": "Annual stroke/thromboembolism rate in AF",
            "horizon": "1y", "reference": "Lip GYH et al. Chest 2010;137:263-272"}
    if not _b(p, "afib"):
        return {**base, "applicable": False, "reason": "Applies only to patients with atrial fibrillation."}
    age = _f(p, "age") or 0
    sbp = _f(p, "sbp") or 0
    female = str(p.get("sex")).lower() == "female"
    htn = _b(p, "hypertension") or _b(p, "antihypertensive") or sbp >= 140
    score = (int(_b(p, "heart_failure")) + int(htn) + (2 if age >= 75 else (1 if age >= 65 else 0))
             + int(str(p.get("diabetes")).lower() in {"type1", "type2"}) + 2 * int(_b(p, "prior_stroke"))
             + int(_b(p, "prior_mi") or _b(p, "pad")) + int(female))
    rate = _CHADS_VASC_RATE[min(score, 9)]
    if (score == 0 and not female) or (score == 1 and female):
        rec = "Low risk — anticoagulation generally not recommended."
    elif (score == 1 and not female) or (score == 2 and female):
        rec = "Anticoagulation may be considered."
    else:
        rec = "Anticoagulation recommended unless contraindicated."
    return {**base, "applicable": True, "risk": rate, "score": score,
            "category": f"Score {score}", "note": rec}


# ------------------------------------------------------- ICD criteria flag --
def icd_primary_prevention_flag(p: dict[str, Any]) -> dict[str, Any]:
    base = {"id": "icd_flag", "name": "ACC/AHA/HRS primary-prevention ICD criteria (screen)",
            "outcome": "Guideline eligibility flag, not a probability", "horizon": None,
            "reference": "Al-Khatib SM et al. 2017 AHA/ACC/HRS Ventricular Arrhythmia/SCD Guideline"}
    lvef = _f(p, "lvef")
    if lvef is None:
        return {**base, "applicable": False, "reason": "Enter LVEF to screen against ICD criteria."}
    met = lvef <= 35 and (_b(p, "heart_failure") or _b(p, "prior_mi"))
    if met:
        note = ("LVEF ≤35% with HF or prior MI: evaluate for primary-prevention ICD "
                "(requires NYHA class, ≥40 days post-MI / ≥90 days GDMT, and expected survival >1 year).")
    elif lvef <= 35:
        note = "LVEF ≤35% without documented HF/MI — clinical review recommended."
    else:
        note = "LVEF >35% — does not meet LVEF threshold for primary-prevention ICD."
    return {**base, "applicable": True, "risk": None, "category": "Criteria met" if met else "Not met", "note": note}


EQUATIONS_BY_CALC = {
    "ami": [pooled_cohort_equations],
    "stroke": [cha2ds2_vasc, pooled_cohort_equations],
    "hf": [],
    "scd": [icd_primary_prevention_flag],
    "af": [charge_af],
}


def run_equations(calculator: str, p: dict[str, Any]) -> list[dict[str, Any]]:
    return [fn(p) for fn in EQUATIONS_BY_CALC.get(calculator, [])]
