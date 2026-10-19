"""Automatic audit of the raw curation dataset and its auto-labels.

Every check returns status pass / warn / fail with the measured value and the
threshold, so the data scientist can acknowledge warnings or waive failures
(with a written justification) in the curation workbench. Any unwaived FAIL
blocks adjudication and release.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from cardioai_core.schema import FIELDS, HORIZONS

REQUIRED_FEATURE_COLS = ["person_id", "index_date", "age", "sex", "race", "followup_days", "death_days"]
CORE_FEATURES = ["age", "sex", "sbp", "total_chol", "hdl"]
NUMERIC_FIELDS = {f["key"]: f for f in FIELDS if f["type"] == "number"}
# Values that usually mean a unit was not converted
UNIT_SUSPECT = {
    "height_cm": lambda s: (s < 3) | ((s > 40) & (s < 90)),        # metres or inches
    "weight_kg": lambda s: s > 230,                                   # pounds
    "total_chol": lambda s: s < 15, "hdl": lambda s: s < 5, "ldl": lambda s: s < 10,   # mmol/L
    "glucose": lambda s: s < 25,                                      # mmol/L
    "hba1c": lambda s: s > 20,                                        # mmol/mol
    "hscrp": lambda s: s > 100,
}
ICD10_TRANSITION = pd.Timestamp("2015-10-01")


def _check(cid, category, title, status, value=None, threshold=None, detail="", table=None) -> dict:
    return {"id": cid, "category": category, "title": title, "status": status, "value": value,
            "threshold": threshold, "detail": detail, "table": table}


def _status(x, warn, fail=None, higher_is_worse=True) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "warn"
    worse = (lambda a, b: a > b) if higher_is_worse else (lambda a, b: a < b)
    if fail is not None and worse(x, fail):
        return "fail"
    if warn is not None and worse(x, warn):
        return "warn"
    return "pass"


def run_audit(features: pd.DataFrame, labels: pd.DataFrame, evidence_counts: dict[str, int],
              baseline_notes: pd.DataFrame | None, cfg: dict) -> dict[str, Any]:
    A = cfg["audit"]
    th = cfg.get("label_thresholds", {"confirm": 0.8, "reject": 0.2})
    checks: list[dict] = []
    F = features.copy()
    F["index_date"] = pd.to_datetime(F["index_date"])
    n = len(F)

    # ---------------- schema & cohort
    missing_cols = [c for c in REQUIRED_FEATURE_COLS if c not in F.columns]
    checks.append(_check("schema", "Schema", "Required columns present", "fail" if missing_cols else "pass",
                         len(missing_cols), 0, ", ".join(missing_cols) or "All required columns present"))
    checks.append(_check("cohort_size", "Cohort", "Cohort size", "fail" if n < A["min_cohort"] else "pass",
                         n, f"≥ {A['min_cohort']}"))
    dup = int(F["person_id"].duplicated().sum())
    checks.append(_check("duplicates", "Cohort", "Duplicate patients", "fail" if dup else "pass", dup, 0))
    bad_age = int(((F["age"] < 18) | (F["age"] > 90)).sum())
    checks.append(_check("age_range", "Cohort", "Age outside 18–90 at index", "fail" if bad_age else "pass", bad_age, 0))
    neg_fu = int((F["followup_days"] < 0).sum())
    checks.append(_check("followup_negative", "Temporal", "Negative follow-up time", "fail" if neg_fu else "pass", neg_fu, 0))

    # ---------------- plausibility & units
    rows, worst = [], 0.0
    for k, f in NUMERIC_FIELDS.items():
        if k not in F:
            continue
        s = F[k].dropna()
        if s.empty:
            continue
        frac = float(((s < f["min"]) | (s > f["max"])).mean())
        worst = max(worst, frac)
        rows.append({"feature": k, "n": int(len(s)), "implausible_frac": round(frac, 5), "min": float(s.min()), "max": float(s.max())})
    checks.append(_check("plausibility", "Data quality", "Values outside clinical range (worst feature)",
                         _status(worst, A["implausible_warn"], A["implausible_fail"]), round(worst, 5),
                         f"warn > {A['implausible_warn']}, fail > {A['implausible_fail']}",
                         "Out-of-range values are set to missing in the release.", rows))
    rows, worst = [], 0.0
    for k, fn in UNIT_SUSPECT.items():
        if k in F:
            s = F[k].dropna()
            if len(s):
                frac = float(fn(s).mean())
                worst = max(worst, frac)
                rows.append({"feature": k, "suspect_frac": round(frac, 5)})
    checks.append(_check("units", "Data quality", "Values suggesting unconverted units (worst feature)",
                         _status(worst, A["unit_suspect_warn"], A["unit_suspect_fail"]), round(worst, 5),
                         f"warn > {A['unit_suspect_warn']}, fail > {A['unit_suspect_fail']}", "", rows))

    # ---------------- missingness & disparity
    rows, core_bad, high_miss = [], [], []
    for k in [f["key"] for f in FIELDS if f["key"] in F.columns and f["type"] != "bool"]:
        m = float(F[k].isna().mean()) if F[k].dtype != object else float((F[k].isna() | (F[k] == "")).mean())
        rows.append({"feature": k, "missing": round(m, 4)})
        if k in CORE_FEATURES and m > A["core_missing_fail"]:
            core_bad.append(k)
        if m > A["missing_warn"] and k not in ("cac_score", "lvef", "hscrp"):
            high_miss.append(k)
    checks.append(_check("core_missing", "Missingness", "Core features missing (age, sex, SBP, TC, HDL)",
                         "fail" if core_bad else "pass", len(core_bad), f"each ≤ {A['core_missing_fail']:.0%}", ", ".join(core_bad)))
    checks.append(_check("missingness", "Missingness", "Features with high missingness",
                         "warn" if high_miss else "pass", len(high_miss), f"≤ {A['missing_warn']:.0%}", ", ".join(high_miss), rows))
    disp = []
    for k in ["sbp", "total_chol", "hdl", "ldl", "hba1c", "egfr", "hscrp"]:
        if k not in F:
            continue
        for grp in ["sex", "race"]:
            rates = F.groupby(grp)[k].apply(lambda s: s.isna().mean())
            sizes = F[grp].value_counts()
            rates = rates[sizes.reindex(rates.index) >= 100]
            if len(rates) >= 2:
                gap = float(rates.max() - rates.min())
                if gap > A["missing_disparity_warn"]:
                    disp.append({"feature": k, "by": grp, "gap": round(gap, 3), "highest": str(rates.idxmax()), "lowest": str(rates.idxmin())})
    checks.append(_check("missing_disparity", "Fairness", "Missingness differs between sex/race groups",
                         "warn" if disp else "pass", len(disp), f"gap ≤ {A['missing_disparity_warn']:.0%}",
                         "Differential missingness can bias models against under-measured groups.", disp))

    # ---------------- representativeness
    rows, small = [], []
    for grp in ["sex", "race"]:
        for g, cnt in F[grp].value_counts().items():
            frac = cnt / n
            rows.append({"group": f"{grp}={g}", "n": int(cnt), "fraction": round(frac, 4)})
            if frac < A["min_subgroup_fraction"]:
                small.append(f"{grp}={g}")
    checks.append(_check("representation", "Fairness", "Small demographic subgroups", "warn" if small else "pass",
                         len(small), f"each ≥ {A['min_subgroup_fraction']:.0%}", ", ".join(small), rows))

    # ---------------- follow-up adequacy
    five = HORIZONS["5y"]
    adequate = float(((F["followup_days"] >= five) | F["death_days"].notna()).mean())
    checks.append(_check("followup_5y", "Temporal", "Patients with ≥5 y follow-up or death",
                         _status(adequate, A["min_5y_followup"], None, higher_is_worse=False), round(adequate, 3),
                         f"≥ {A['min_5y_followup']:.0%}", "Low values shrink the 5-year training set."))

    # ---------------- candidate / label checks
    L = labels.copy()
    L["event_date"] = pd.to_datetime(L["event_date"])
    before = int((L["days_from_index"] <= 0).sum())
    checks.append(_check("events_after_index", "Leakage", "Candidate events on/before index date",
                         "fail" if before else "pass", before, 0, "Outcome information before index would leak into features."))
    d = L.merge(F[["person_id", "death_days"]], on="person_id", how="left")
    after_death = int((d["death_days"].notna() & (d["days_from_index"] > d["death_days"] + 1)).sum())
    frac_ad = after_death / max(len(L), 1)
    checks.append(_check("events_after_death", "Temporal", "Candidate events after recorded death",
                         _status(frac_ad, 0.0, A["after_death_fail"]), after_death, f"fail > {A['after_death_fail']:.1%} of candidates",
                         "Usually a date-shifting or death-linkage problem."))
    orphan = int((~L["person_id"].isin(F["person_id"])).sum())
    checks.append(_check("orphans", "Schema", "Candidates for patients not in cohort", "fail" if orphan else "pass", orphan, 0))

    rows, high_unc, conflict_rows = [], [], []
    for fam, g in L.groupby("family"):
        rel = g[g["label_relevant"]] if "label_relevant" in g else g
        vc = rel["auto_label"].value_counts(normalize=True)
        unc = float(vc.get("uncertain", 0.0))
        lf_cols = [c for c in g.columns if c.startswith("lf_") and (g[c] != 0).any()]
        pos = (g[lf_cols] > 0).any(axis=1)
        neg = (g[lf_cols] < 0).any(axis=1)
        conf = float((pos & neg).mean())
        rows.append({"family": fam, "candidates": int(len(g)), "relevant": int(len(rel)),
                     "confirmed": round(float(vc.get("confirmed", 0)), 3), "uncertain": round(unc, 3),
                     "rejected": round(float(vc.get("rejected", 0)), 3), "evidence_only": int((g["source"] == "evidence_only").sum()),
                     "lf_conflict_rate": round(conf, 3)})
        if unc > A["max_uncertain_fraction"]:
            high_unc.append(fam)
        if conf > A["lf_conflict_warn"]:
            conflict_rows.append(fam)
    checks.append(_check("uncertain_fraction", "Auto-labels", "Families with many uncertain auto-labels",
                         "warn" if high_unc else "pass", len(high_unc), f"≤ {A['max_uncertain_fraction']:.0%} of relevant candidates",
                         ", ".join(high_unc) + (" — large adjudication load or weak evidence capture" if high_unc else ""), rows))
    checks.append(_check("lf_conflict", "Auto-labels", "Labelling functions disagreeing on the same candidate",
                         "warn" if conflict_rows else "pass", len(conflict_rows), f"≤ {A['lf_conflict_warn']:.0%}", ", ".join(conflict_rows)))

    # LF coverage: an LF that never fires usually means missing evidence tables / wrong codes
    dead = [c[3:] for c in L.columns if c.startswith("lf_") and (L[c] == 0).all()]
    checks.append(_check("lf_coverage", "Auto-labels", "Labelling functions that never fire", "warn" if dead else "pass",
                         len(dead), 0, ", ".join(dead) + (" — check evidence extraction / code lists" if dead else "")))
    empty_ev = [k for k, v in evidence_counts.items() if v == 0]
    checks.append(_check("evidence_tables", "Schema", "Empty evidence tables", "fail" if empty_ev else "pass",
                         len(empty_ev), 0, ", ".join(empty_ev), [{"table": k, "rows": v} for k, v in evidence_counts.items()]))

    # Prevalence of auto-confirmed first events vs plausible epidemiological range (5-year cumulative)
    first = (L[(L["auto_label"] == "confirmed")].sort_values("event_date").drop_duplicates(["person_id", "family"]))
    rows, out_range = [], []
    for fam, (lo, hi) in A["prevalence_5y"].items():
        ev = first[(first["family"] == fam) & (first["days_from_index"] <= five)]["person_id"]
        elig = F if fam != "af" else F[F.get("afib", 0) == 0]
        has_event = elig["person_id"].isin(ev)
        denom = int(((elig["followup_days"] >= five) | elig["death_days"].notna() | has_event).sum())
        rate = float(has_event.sum() / max(denom, 1))
        rows.append({"family": fam, "events": int(has_event.sum()), "denominator": denom, "rate_5y": round(rate, 4),
                     "expected": f"{lo:.1%}–{hi:.1%}"})
        if not lo <= rate <= hi:
            out_range.append(f"{fam} {rate:.2%}")
    checks.append(_check("prevalence", "Auto-labels", "5-year auto-confirmed incidence outside expected range",
                         "warn" if out_range else "pass", len(out_range), "within configured range", ", ".join(out_range), rows))

    # Coding drift across the ICD-9 -> ICD-10 transition (candidate rate per 1,000 patient-years)
    rows, drift = [], []
    F["end_date"] = F["index_date"] + pd.to_timedelta(F["followup_days"], unit="D")
    py9 = ((np.minimum(F["end_date"], ICD10_TRANSITION) - F["index_date"]).dt.days.clip(lower=0).sum()) / 365.25
    py10 = ((F["end_date"] - np.maximum(F["index_date"], ICD10_TRANSITION)).dt.days.clip(lower=0).sum()) / 365.25
    code_c = L[L["source"] == "code"]
    for fam, g in code_c.groupby("family"):
        n9 = int((g["event_date"] < ICD10_TRANSITION).sum())
        n10 = int((g["event_date"] >= ICD10_TRANSITION).sum())
        if py9 < 1000 or py10 < 1000 or n9 + n10 < 50:
            rows.append({"family": fam, "icd9_rate": None, "icd10_rate": None, "ratio": None, "note": "insufficient person-time"})
            continue
        r9, r10 = 1000 * n9 / py9, 1000 * n10 / py10
        ratio = r10 / r9 if r9 > 0 else float("inf")
        rows.append({"family": fam, "icd9_rate": round(r9, 2), "icd10_rate": round(r10, 2), "ratio": round(ratio, 2)})
        lo, hi = A["drift_ratio"]
        if not lo <= ratio <= hi:
            drift.append(f"{fam} ×{ratio:.2f}")
    checks.append(_check("icd_drift", "Coding", "Candidate rate change across ICD-9 → ICD-10 (Oct 2015)",
                         "warn" if drift else "pass", len(drift), f"ratio {A['drift_ratio'][0]}–{A['drift_ratio'][1]}",
                         ", ".join(drift) + (" — check code mappings; cohort ages over follow-up also raises rates" if drift else ""), rows))

    # Annotation (CAC / LVEF from reports)
    if baseline_notes is not None and len(baseline_notes):
        from .imaging_nlp import extract_cac, extract_lvef
        bn = baseline_notes
        has_kw_ef = bn["note_text"].str.contains(r"ejection fraction|lvef|\bef\b", case=False, regex=True)
        has_kw_cac = bn["note_text"].str.contains(r"agatston|calcium", case=False, regex=True)
        ef_hit = bn.loc[has_kw_ef, "note_text"].map(extract_lvef).notna().mean() if has_kw_ef.any() else 1.0
        cac_hit = bn.loc[has_kw_cac, "note_text"].map(extract_cac).notna().mean() if has_kw_cac.any() else 1.0
        worst = float(min(ef_hit, cac_hit))
        checks.append(_check("annotation_yield", "Annotation", "Reports mentioning LVEF/CAC where a value was extracted",
                             _status(worst, A["annotation_yield_warn"], None, higher_is_worse=False), round(worst, 3),
                             f"≥ {A['annotation_yield_warn']:.0%}",
                             f"LVEF {ef_hit:.1%}, CAC {cac_hit:.1%}. Accuracy is measured in adjudication (annotation review).",
                             [{"field": "lvef", "reports": int(has_kw_ef.sum()), "extracted": round(float(ef_hit), 3)},
                              {"field": "cac_score", "reports": int(has_kw_cac.sum()), "extracted": round(float(cac_hit), 3)}]))

    counts = {s: sum(c["status"] == s for c in checks) for s in ("pass", "warn", "fail")}
    overall = "fail" if counts["fail"] else ("warn" if counts["warn"] else "pass")
    return {"overall": overall, "counts": counts, "n_patients": n, "n_candidates": int(len(L)), "checks": checks}


def to_markdown(rep: dict, title: str) -> str:
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    L = [f"# {title}", "", f"Overall: **{rep['overall'].upper()}** — {rep['counts']}", "",
         "| | Check | Value | Threshold | Detail |", "|---|---|---|---|---|"]
    for c in rep["checks"]:
        L.append(f"| {icon.get(c['status'], '')} | {c['category']}: {c['title']} | {c['value']} | {c['threshold'] or ''} | {c['detail']} |")
    return "\n".join(L) + "\n"
