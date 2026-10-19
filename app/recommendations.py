"""Guideline-anchored considerations shown with results.

Statin and BP thresholds use the PUBLISHED equations / measured values the
guidelines were written for — never the AI probability, whose thresholds have
not been validated for treatment decisions.
"""
from __future__ import annotations

from typing import Any


def _b(p: dict, k: str) -> bool:
    v = p.get(k)
    return v in (True, 1, "1", "true", "on", "yes")


def _f(p: dict, k: str) -> float | None:
    try:
        return None if p.get(k) in (None, "") else float(p[k])
    except (TypeError, ValueError):
        return None


def category(risk: float) -> tuple[str, str]:
    """Display bands for 5-year AI risk (presentation only, not treatment thresholds)."""
    if risk < 0.05:
        return "Low", "risk-low"
    if risk < 0.10:
        return "Moderate", "risk-moderate"
    if risk < 0.20:
        return "High", "risk-high"
    return "Very High", "risk-very-high"


def build(calculator: str, p: dict[str, Any], primary_risk: float | None, equations: list[dict]) -> list[str]:
    recs: list[str] = []
    eq = {e["id"]: e for e in equations if e.get("applicable")}
    sbp, lvef, age = _f(p, "sbp"), _f(p, "lvef"), _f(p, "age")
    secondary = _b(p, "prior_mi") or _b(p, "prior_stroke")
    dm = str(p.get("diabetes")) in {"type1", "type2"}

    if calculator in {"ami", "stroke"}:
        if secondary:
            recs.append("Clinical ASCVD present: high-intensity statin is indicated (2018 AHA/ACC Cholesterol Guideline); "
                        "consider adding ezetimibe/PCSK9 inhibitor if LDL-C remains ≥70 mg/dL.")
        elif "pce" in eq:
            r = eq["pce"]["risk"]
            if r >= 0.20:
                recs.append(f"PCE 10-yr ASCVD {r:.1%} (high): high-intensity statin to lower LDL-C ≥50%.")
            elif r >= 0.075:
                recs.append(f"PCE 10-yr ASCVD {r:.1%} (intermediate): moderate-intensity statin; if the decision is uncertain, "
                            "a coronary artery calcium score can reclassify risk.")
            elif r >= 0.05:
                recs.append(f"PCE 10-yr ASCVD {r:.1%} (borderline): clinician–patient risk discussion; risk enhancers "
                            "(hs-CRP ≥2, family history, CKD) may favor a statin.")
        if dm and age and 40 <= age <= 75 and not secondary:
            recs.append("Diabetes, age 40–75: at least moderate-intensity statin is recommended regardless of calculated risk.")
    if calculator == "stroke" and "cha2ds2_vasc" in eq:
        e = eq["cha2ds2_vasc"]
        recs.append(f"CHA₂DS₂-VASc {e['score']}: {e['note']}" + ("" if _b(p, "anticoagulant") else " (no anticoagulant recorded)."))
    if calculator in {"hf", "scd"}:
        if lvef is not None and lvef <= 40 and not (_b(p, "beta_blocker") and _b(p, "acei_arb")):
            recs.append("LVEF ≤40% without full GDMT recorded: review for beta-blocker, ARNI/ACE-I/ARB, MRA and SGLT2 "
                        "inhibitor per 2022 AHA/ACC/HFSA HF Guideline.")
        if lvef is None and (_b(p, "heart_failure") or _b(p, "prior_mi")):
            recs.append("No LVEF entered: an echocardiogram would refine HF and SCD risk and ICD eligibility.")
        if dm and calculator == "hf":
            recs.append("Type 2 diabetes: SGLT2 inhibitors reduce HF hospitalization in patients with diabetes and CV risk.")
    if calculator == "scd" and "icd_flag" in eq:
        recs.append(eq["icd_flag"]["note"])
    if calculator == "af" and primary_risk is not None and primary_risk >= 0.10:
        recs.append("Elevated predicted AF risk: consider opportunistic pulse/ECG checks; evidence for systematic "
                    "screening of asymptomatic adults is insufficient (USPSTF 2022).")
    if sbp is not None and sbp >= 130:
        recs.append(f"SBP {sbp:.0f} mmHg: target <130/80 mmHg (2017 ACC/AHA BP Guideline) with lifestyle ± medication.")
    if str(p.get("smoking")) == "current":
        recs.append("Current smoker: tobacco cessation counseling plus pharmacotherapy (varenicline or combination NRT).")
    if primary_risk is not None and primary_risk >= 0.20:
        recs.append("Very high predicted 5-year risk: consider cardiology review to confirm risk and optimize therapy.")
    if not recs:
        recs.append("Continue heart-healthy lifestyle; reassess risk factors at routine visits.")
    return recs
