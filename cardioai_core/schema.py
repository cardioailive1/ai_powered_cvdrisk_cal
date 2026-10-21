"""Canonical feature schema and calculator definitions.

Every model feature has ONE name used everywhere: the OMOP extraction SQL,
the synthetic generator, the training script, the API and the frontend.
"""
from __future__ import annotations

import math
from typing import Any

# --------------------------------------------------------------------------
# Calculators -> endpoints -> horizons
# --------------------------------------------------------------------------
HORIZONS: dict[str, int] = {"30d": 30, "1y": 365, "5y": 1826}

# --------------------------------------------------------------------------
# Eligibility: who the calculators are for (ADULTS ONLY — no pediatric use)
# One definition used by the cohort SQL, the automatic audit, the API's input validation and the docs.
# --------------------------------------------------------------------------
AGE_MIN = 18   # years, inclusive: patients under 18 are never extracted, trained on, validated on or scored
AGE_MAX = 89   # years, inclusive (age < 90): de-identified data top-codes ages 90 and over ("90+"), so exact
               # ages above 89 are not available to train on (HIPAA Safe Harbor, 45 CFR 164.514(b)(2)(i)(C))
INDEX_WINDOW = ("2012-01-01", "2019-12-31")   # index visits (default; leaves >= 5 years of follow-up)

ELIGIBILITY: dict[str, Any] = {
    "population": f"Adults aged {AGE_MIN}–{AGE_MAX} years. Pediatric patients (under {AGE_MIN}) are excluded from "
                  "training, validation and use of all five calculators.",
    "inclusion": [
        {"id": "I1", "criterion": f"Age {AGE_MIN}–{AGE_MAX} years at the index visit (age ≥ {AGE_MIN}.0 and < {AGE_MAX + 1}.0)",
         "applies_to": "all", "where": "cohort SQL; API validation"},
        {"id": "I2", "criterion": f"An outpatient visit between {INDEX_WINDOW[0]} and {INDEX_WINDOW[1]} (one index visit per patient, "
                                  "chosen at random among eligible visits)", "applies_to": "all", "where": "cohort SQL"},
        {"id": "I3", "criterion": "At least 365 days of observation before the index visit", "applies_to": "all", "where": "cohort SQL"},
        {"id": "I4", "criterion": "Total cholesterol or HDL cholesterol measured in the 365 days before the index visit",
         "applies_to": "all", "where": "cohort SQL"},
        {"id": "I5", "criterion": "Recorded sex male or female (the models and published equations are sex-specific)",
         "applies_to": "all", "where": "cohort SQL; API validation"},
    ],
    "exclusion": [
        {"id": "E1", "criterion": f"Age under {AGE_MIN} at the index visit (pediatric)", "applies_to": "all",
         "where": "cohort SQL; audit (fail); API refuses"},
        {"id": "E2", "criterion": f"Age {AGE_MAX + 1} or over at the index visit (top-coded in de-identified data)", "applies_to": "all",
         "where": "cohort SQL; audit (fail); API refuses"},
        {"id": "E3", "criterion": "Sex not recorded as male or female", "applies_to": "all", "where": "cohort SQL"},
        {"id": "E4", "criterion": "Death recorded on or before the index date", "applies_to": "all", "where": "cohort SQL"},
        {"id": "E5", "criterion": "No observation time after the index date (no follow-up)", "applies_to": "all", "where": "cohort SQL"},
        {"id": "E6", "criterion": "Known atrial fibrillation or flutter at the index visit", "applies_to": "af",
         "where": "AF training labels; API returns 'not applicable'"},
    ],
    "outcome_rules": [   # not patient exclusions: these events are not counted as the outcome
        "Type 2 (supply/demand) MI is not counted as an AMI event",
        "Post-operative AF (≤ 30 days after cardiac surgery) is not counted as new-onset AF",
        "Only the first event of each outcome after the index date is counted",
    ],
    "published_equation_ranges": {"Pooled Cohort Equations": "40–79 years", "CHARGE-AF": "46–94 years (used here up to 89)",
                                  "CHA₂DS₂-VASc": "adults with AF", "ICD criteria": "adults"},
}


def age_problem(age: float) -> str | None:
    """Plain-language reason a patient's age is outside the calculators' population, or None."""
    if age < AGE_MIN:
        return (f"These calculators are for adults aged {AGE_MIN}–{AGE_MAX}. They are not validated for patients "
                f"under {AGE_MIN} and must not be used in children or adolescents.")
    if age > AGE_MAX:
        return (f"These calculators are validated for ages {AGE_MIN}–{AGE_MAX}; ages {AGE_MAX + 1} and over are outside "
                "the training data.")
    return None

CALCULATORS: dict[str, dict[str, Any]] = {
    "ami": {
        "name": "Acute Myocardial Infarction (AMI)",
        "target_cstat": 0.82,
        "description": "Predicts 30-day, 1-year and 5-year risk of acute myocardial infarction.",
        "population": "Primary and secondary prevention populations",
        "primary_endpoint": "ami",
        "endpoints": {"ami": "Acute myocardial infarction"},
    },
    "stroke": {
        "name": "Stroke Risk",
        "target_cstat": 0.75,
        "description": "Predicts ischemic stroke, hemorrhagic stroke and TIA risk.",
        "population": "Includes AF-stroke and hemorrhagic stroke models",
        "primary_endpoint": "any_stroke",
        "endpoints": {
            "any_stroke": "Any stroke or TIA",
            "ischemic_stroke": "Ischemic stroke",
            "hemorrhagic_stroke": "Hemorrhagic stroke",
            "tia": "Transient ischemic attack",
        },
    },
    "hf": {
        "name": "Heart Failure",
        "target_cstat": 0.80,
        "description": "Predicts incident heart failure and decompensation events.",
        "population": "Separate models for HFrEF and HFpEF",
        "primary_endpoint": "hf",
        "endpoints": {"hf": "Heart failure hospitalization / diagnosis", "hfref": "HFrEF", "hfpef": "HFpEF"},
    },
    "scd": {
        "name": "Sudden Cardiac Death (SCD)",
        "target_cstat": 0.78,
        "description": "Predicts risk of sudden cardiac arrest and ventricular arrhythmias.",
        "population": "Guides ICD placement decisions",
        "primary_endpoint": "scd",
        "endpoints": {"scd": "Cardiac arrest / VF / sustained VT"},
    },
    "af": {
        "name": "Atrial Fibrillation",
        "target_cstat": 0.80,
        "description": "Predicts new-onset atrial fibrillation and recurrence.",
        "population": "Enables targeted screening and early intervention",
        "primary_endpoint": "af",
        "endpoints": {"af": "New-onset atrial fibrillation / flutter"},
        # Diagnosis codes keep recurring in patients who already have AF, so the
        # model is trained (and applied) only in patients without known AF.
        "exclude_if": "afib",
        "exclude_reason": ("Patient already has atrial fibrillation. The new-onset AF model does not apply; "
                           "recurrence prediction needs rhythm-monitoring data that this form does not collect."),
    },
}


# --------------------------------------------------------------------------
# Outcome families: the unit of labeling and cardiologist adjudication.
# Each model endpoint belongs to one family; subtype endpoints only count
# confirmed events with a matching adjudicated/auto subtype.
# --------------------------------------------------------------------------
FAMILIES: dict[str, dict[str, Any]] = {
    "ami": {
        "name": "Acute myocardial infarction",
        "definition": "Fourth Universal Definition of Myocardial Infarction (Thygesen K et al., Circulation 2018)",
        "criteria": {
            "trop_rise_fall": "Cardiac troponin rise and/or fall with ≥1 value above the 99th-percentile URL",
            "ischemic_symptoms": "Symptoms of acute myocardial ischemia",
            "ecg_ischemia": "New ischemic ECG changes or pathological Q waves",
            "imaging": "Imaging evidence of new loss of viable myocardium / new regional wall-motion abnormality",
            "thrombus": "Coronary thrombus on angiography or autopsy",
        },
        "subtypes": {"stemi": "STEMI", "nstemi": "NSTEMI (type 1)", "type2": "Type 2 MI (supply/demand)",
                     "unspecified": "MI, type unspecified"},
    },
    "stroke": {
        "name": "Stroke / TIA",
        "definition": "AHA/ASA updated definition of stroke (Sacco RL et al., Stroke 2013)",
        "criteria": {
            "focal_deficit": "Acute focal neurological deficit of vascular origin",
            "imaging_infarct": "Acute infarct on CT/MRI",
            "imaging_hemorrhage": "Acute intracranial hemorrhage on CT/MRI",
            "transient": "Transient symptoms without acute infarct on imaging (TIA)",
        },
        "subtypes": {"ischemic": "Ischemic stroke", "hemorrhagic": "Hemorrhagic stroke (ICH/SAH)", "tia": "TIA",
                     "unknown": "Stroke, type undetermined"},
    },
    "hf": {
        "name": "Heart failure",
        "definition": "Universal Definition of Heart Failure (Bozkurt B et al., J Card Fail 2021)",
        "criteria": {
            "symptoms": "New or worsening HF symptoms (dyspnea, orthopnea, edema, fatigue)",
            "signs": "Objective signs: congestion on exam or imaging",
            "np_elevated": "Elevated natriuretic peptide",
            "treatment": "IV diuretic / vasoactive therapy or hospitalization for HF",
        },
        "subtypes": {"hfref": "HFrEF (LVEF ≤40%)", "hfmref": "HFmrEF (LVEF 41–49%)", "hfpef": "HFpEF (LVEF ≥50%)",
                     "unknown": "HF, LVEF unknown"},
    },
    "scd": {
        "name": "Sudden cardiac arrest / ventricular arrhythmia",
        "definition": "2017 AHA/ACC/HRS Ventricular Arrhythmia and SCD Guideline definitions",
        "criteria": {
            "pulseless": "Unexpected loss of pulse / cardiac arrest",
            "shockable": "VF or sustained VT (≥30 s or requiring termination)",
            "resuscitation": "CPR, defibrillation or appropriate ICD therapy",
            "sudden_death": "Unexpected death within 1 h of symptom onset",
        },
        "subtypes": {"vf": "Ventricular fibrillation", "vt": "Sustained ventricular tachycardia",
                     "arrest": "Cardiac arrest, rhythm unspecified", "unknown": "Undetermined"},
    },
    "af": {
        "name": "Atrial fibrillation / flutter (new onset)",
        "definition": "2023 ACC/AHA/ACCP/HRS AF Guideline: ECG-documented AF ≥30 s",
        "criteria": {
            "ecg_documented": "AF/flutter documented on ECG or rhythm strip (≥30 s)",
            "clinician_dx": "Clinician-confirmed diagnosis in the record",
            "treatment": "Rate/rhythm control or anticoagulation initiated",
        },
        "subtypes": {"af": "Atrial fibrillation", "flutter": "Atrial flutter",
                     "postoperative": "Post-operative AF (≤30 d after cardiac surgery)", "unknown": "Undetermined"},
    },
}

ENDPOINT_FAMILY: dict[str, str] = {
    "ami": "ami", "any_stroke": "stroke", "ischemic_stroke": "stroke", "hemorrhagic_stroke": "stroke", "tia": "stroke",
    "hf": "hf", "hfref": "hf", "hfpef": "hf", "scd": "scd", "af": "af",
}
# Subtype endpoints: which confirmed subtypes count as an event. Subtypes listed in
# "known_other" are definite non-matches; any other subtype is treated as unknown.
ENDPOINT_SUBTYPES: dict[str, dict[str, set[str]]] = {
    "ischemic_stroke": {"match": {"ischemic"}, "known_other": {"hemorrhagic", "tia"}},
    "hemorrhagic_stroke": {"match": {"hemorrhagic"}, "known_other": {"ischemic", "tia"}},
    "tia": {"match": {"tia"}, "known_other": {"ischemic", "hemorrhagic"}},
    "hfref": {"match": {"hfref"}, "known_other": {"hfmref", "hfpef"}},
    "hfpef": {"match": {"hfpef"}, "known_other": {"hfmref", "hfref"}},
}


def all_endpoints() -> list[str]:
    seen: list[str] = []
    for c in CALCULATORS.values():
        for e in c["endpoints"]:
            if e not in seen:
                seen.append(e)
    return seen


# --------------------------------------------------------------------------
# Input fields (what the clinician enters) — drives the dynamic frontend
# --------------------------------------------------------------------------
# type: number | select | bool.  "calcs": which calculators show the field (None = all)
FIELDS: list[dict[str, Any]] = [
    # Demographics
    {"key": "age", "label": "Age", "unit": "years", "type": "number", "min": AGE_MIN, "max": AGE_MAX, "default": 55, "section": "demographics", "required": True},
    {"key": "sex", "label": "Sex", "type": "select", "options": ["male", "female"], "default": "male", "section": "demographics", "required": True},
    {"key": "race", "label": "Race/Ethnicity", "type": "select", "options": ["white", "black", "hispanic", "asian", "other"], "default": "white", "section": "demographics", "required": True},
    {"key": "height_cm", "label": "Height", "unit": "cm", "type": "number", "min": 120, "max": 220, "default": 175, "section": "demographics"},
    {"key": "weight_kg", "label": "Weight", "unit": "kg", "type": "number", "min": 30, "max": 300, "default": 80, "section": "demographics"},
    # Clinical history
    {"key": "prior_mi", "label": "Prior MI", "type": "bool", "section": "history"},
    {"key": "prior_stroke", "label": "Prior Stroke/TIA", "type": "bool", "section": "history"},
    {"key": "angina", "label": "Angina", "type": "bool", "section": "history"},
    {"key": "heart_failure", "label": "Heart Failure", "type": "bool", "section": "history"},
    {"key": "afib", "label": "Atrial Fibrillation", "type": "bool", "section": "history"},
    {"key": "family_hx_cad", "label": "Family History CAD", "type": "bool", "section": "history"},
    {"key": "hypertension", "label": "Hypertension diagnosis", "type": "bool", "section": "history"},
    {"key": "pad", "label": "Peripheral artery disease", "type": "bool", "section": "history"},
    # Risk factors
    {"key": "sbp", "label": "Systolic BP", "unit": "mmHg", "type": "number", "min": 70, "max": 250, "default": 130, "section": "risk", "required": True},
    {"key": "dbp", "label": "Diastolic BP", "unit": "mmHg", "type": "number", "min": 40, "max": 150, "default": 80, "section": "risk"},
    {"key": "smoking", "label": "Smoking Status", "type": "select", "options": ["never", "former", "current"], "default": "never", "section": "risk"},
    {"key": "diabetes", "label": "Diabetes Status", "type": "select", "options": ["none", "prediabetes", "type1", "type2"], "default": "none", "section": "risk"},
    # Labs
    {"key": "total_chol", "label": "Total Cholesterol", "unit": "mg/dL", "type": "number", "min": 80, "max": 400, "default": 200, "section": "labs"},
    {"key": "hdl", "label": "HDL Cholesterol", "unit": "mg/dL", "type": "number", "min": 15, "max": 120, "default": 50, "section": "labs"},
    {"key": "ldl", "label": "LDL Cholesterol", "unit": "mg/dL", "type": "number", "min": 20, "max": 300, "default": 120, "section": "labs"},
    {"key": "trig", "label": "Triglycerides", "unit": "mg/dL", "type": "number", "min": 20, "max": 1500, "default": 150, "section": "labs"},
    {"key": "glucose", "label": "Fasting Glucose", "unit": "mg/dL", "type": "number", "min": 40, "max": 500, "default": 95, "section": "labs"},
    {"key": "hba1c", "label": "HbA1c", "unit": "%", "type": "number", "min": 3, "max": 18, "default": 5.6, "section": "labs"},
    {"key": "hscrp", "label": "hs-CRP", "unit": "mg/L", "type": "number", "min": 0, "max": 50, "default": 1.5, "section": "labs"},
    {"key": "egfr", "label": "eGFR", "unit": "mL/min/1.73m²", "type": "number", "min": 5, "max": 150, "default": 90, "section": "labs"},
    # Imaging (from Radiology reports on Mayo Clinic Platform) — optional
    {"key": "cac_score", "label": "Coronary artery calcium (Agatston)", "unit": "AU", "type": "number", "min": 0, "max": 5000, "section": "imaging", "calcs": ["ami", "stroke", "hf"]},
    {"key": "lvef", "label": "LV ejection fraction", "unit": "%", "type": "number", "min": 5, "max": 85, "section": "imaging", "calcs": ["hf", "scd", "af"]},
    # Medications
    {"key": "statin", "label": "Statin", "type": "bool", "section": "meds"},
    {"key": "aspirin", "label": "Aspirin", "type": "bool", "section": "meds"},
    {"key": "acei_arb", "label": "ACE-I/ARB", "type": "bool", "section": "meds"},
    {"key": "beta_blocker", "label": "Beta Blocker", "type": "bool", "section": "meds"},
    {"key": "anticoagulant", "label": "Anticoagulant", "type": "bool", "section": "meds"},
    {"key": "antihypertensive", "label": "Any antihypertensive", "type": "bool", "section": "meds"},
]

SECTIONS = {
    "demographics": "Demographics",
    "history": "Clinical History",
    "risk": "Risk Factors",
    "labs": "Laboratory Values",
    "imaging": "Imaging (from radiology reports)",
    "meds": "Current Medications",
}

# --------------------------------------------------------------------------
# Model feature vector (numeric; NaN = missing, handled natively by the models)
# --------------------------------------------------------------------------
MODEL_FEATURES: list[str] = [
    "age", "sex_male", "race_black", "race_hispanic", "race_asian", "race_other",
    "height_cm", "weight_kg", "bmi",
    "prior_mi", "prior_stroke", "angina", "heart_failure", "afib", "family_hx_cad",
    "hypertension", "pad",
    "sbp", "dbp", "smoking_former", "smoking_current",
    "diabetes_pre", "diabetes_t1", "diabetes_t2",
    "total_chol", "hdl", "ldl", "trig", "glucose", "hba1c", "hscrp", "egfr", "non_hdl",
    "cac_score_log", "lvef",
    "statin", "aspirin", "acei_arb", "beta_blocker", "anticoagulant", "antihypertensive",
]

# Human-readable names for explanations
FEATURE_LABELS: dict[str, str] = {f["key"]: f["label"] for f in FIELDS}
FEATURE_LABELS.update({
    "sex_male": "Male sex", "race_black": "Black/African American", "race_hispanic": "Hispanic/Latino",
    "race_asian": "Asian", "race_other": "Other race", "bmi": "BMI",
    "smoking_former": "Former smoker", "smoking_current": "Current smoker",
    "diabetes_pre": "Prediabetes", "diabetes_t1": "Type 1 diabetes", "diabetes_t2": "Type 2 diabetes",
    "non_hdl": "Non-HDL cholesterol", "cac_score_log": "Coronary calcium score",
})


def _num(v: Any) -> float:
    if v is None or v == "":
        return math.nan
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def _flag(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, str):
        return 1.0 if v.strip().lower() in {"1", "true", "yes", "y", "on"} else 0.0
    return 1.0 if v else 0.0


def build_feature_row(p: dict[str, Any]) -> dict[str, float]:
    """Patient input dict (API/form or OMOP extract row) -> model feature dict."""
    sex = str(p.get("sex", "")).lower()
    race = str(p.get("race", "")).lower()
    smoking = str(p.get("smoking", "never")).lower()
    diabetes = str(p.get("diabetes", "none")).lower()

    h, w = _num(p.get("height_cm")), _num(p.get("weight_kg"))
    bmi = _num(p.get("bmi"))
    if math.isnan(bmi) and h > 0 and w > 0:
        bmi = w / (h / 100.0) ** 2

    tc, hdl = _num(p.get("total_chol")), _num(p.get("hdl"))
    cac = _num(p.get("cac_score"))

    acei = _flag(p.get("acei_arb"))
    bb = _flag(p.get("beta_blocker"))
    anti_htn = max(_flag(p.get("antihypertensive")), acei, bb)

    row = {
        "age": _num(p.get("age")),
        "sex_male": 1.0 if sex in {"male", "m"} else 0.0,
        "race_black": 1.0 if race == "black" else 0.0,
        "race_hispanic": 1.0 if race == "hispanic" else 0.0,
        "race_asian": 1.0 if race == "asian" else 0.0,
        "race_other": 1.0 if race == "other" else 0.0,
        "height_cm": h, "weight_kg": w, "bmi": bmi,
        "prior_mi": _flag(p.get("prior_mi")), "prior_stroke": _flag(p.get("prior_stroke")),
        "angina": _flag(p.get("angina")), "heart_failure": _flag(p.get("heart_failure")),
        "afib": _flag(p.get("afib")), "family_hx_cad": _flag(p.get("family_hx_cad")),
        "hypertension": _flag(p.get("hypertension")), "pad": _flag(p.get("pad")),
        "sbp": _num(p.get("sbp")), "dbp": _num(p.get("dbp")),
        "smoking_former": 1.0 if smoking == "former" else 0.0,
        "smoking_current": 1.0 if smoking == "current" else 0.0,
        "diabetes_pre": 1.0 if diabetes == "prediabetes" else 0.0,
        "diabetes_t1": 1.0 if diabetes == "type1" else 0.0,
        "diabetes_t2": 1.0 if diabetes == "type2" else 0.0,
        "total_chol": tc, "hdl": hdl, "ldl": _num(p.get("ldl")), "trig": _num(p.get("trig")),
        "glucose": _num(p.get("glucose")), "hba1c": _num(p.get("hba1c")),
        "hscrp": _num(p.get("hscrp")), "egfr": _num(p.get("egfr")),
        "non_hdl": tc - hdl if not (math.isnan(tc) or math.isnan(hdl)) else math.nan,
        "cac_score_log": math.log1p(cac) if not math.isnan(cac) and cac >= 0 else math.nan,
        "lvef": _num(p.get("lvef")),
        "statin": _flag(p.get("statin")), "aspirin": _flag(p.get("aspirin")),
        "acei_arb": acei, "beta_blocker": bb,
        "anticoagulant": _flag(p.get("anticoagulant")), "antihypertensive": anti_htn,
    }
    return {k: row[k] for k in MODEL_FEATURES}


def validate_input(p: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty = valid)."""
    errors: list[str] = []
    for f in FIELDS:
        v = p.get(f["key"])
        if f.get("required") and (v is None or v == ""):
            errors.append(f"{f['label']} is required.")
            continue
        if v in (None, ""):
            continue
        if f["type"] == "number":
            x = _num(v)
            if math.isnan(x):
                errors.append(f"{f['label']} must be a number.")
            elif f["key"] == "age" and age_problem(x):
                errors.append(age_problem(x))
            elif x < f["min"] or x > f["max"]:
                errors.append(f"{f['label']} must be between {f['min']} and {f['max']} {f.get('unit', '')}".strip() + ".")
        elif f["type"] == "select" and str(v).lower() not in f["options"]:
            errors.append(f"{f['label']} must be one of: {', '.join(f['options'])}.")
    tc, hdl = _num(p.get("total_chol")), _num(p.get("hdl"))
    if not (math.isnan(tc) or math.isnan(hdl)) and hdl >= tc:
        errors.append("HDL cholesterol must be lower than total cholesterol.")
    return errors
