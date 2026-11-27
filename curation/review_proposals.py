"""Proposals for the data scientist review of the automatic audit.

For every audit check that is not passed and not yet resolved, this proposes an action with a justification
written from the evidence (the audit's own tables, the labels and the baseline features):

    acknowledge  a warning that is explained by the data and needs no change
    waive        a failure that is a tail of real values on the right scale, not an error to fix
    fix          a problem to fix or investigate first; it cannot be accepted as proposed (the data scientist can
                 still acknowledge or waive it manually, with their own reason, once understood)

Nothing is recorded until the data scientist accepts a proposal (CLI `review-proposals` or the workbench Audit
page). Accepting records the same review a manual acknowledge / waive would, under the data scientist's name;
rejecting records the rejection in the audit trail and leaves the item open for manual review.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from training.audit import ICD10_TRANSITION, NUMERIC_FIELDS

TAIL_MAX = 0.05          # a failing feature may be waived only if at most this share is out of range ...
STEP_TOLERANCE = 1.25    # ... and ICD drift is acknowledged only if the 2015->2016 change is within x1.25 of the trend


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%" if x >= 0.001 else f"{100 * x:.2f}%"


def _median(features: pd.DataFrame | None, feature: str) -> float | None:
    if features is None or feature not in features:
        return None
    s = pd.to_numeric(features[feature], errors="coerce").dropna()
    return float(s.median()) if len(s) else None


def _in_range(feature: str, v: float | None) -> bool:
    f = NUMERIC_FIELDS.get(feature)
    return v is not None and f is not None and f.get("min", -math.inf) <= v <= f.get("max", math.inf)


def _feature_tail(check: dict, cfg: dict, features: pd.DataFrame | None, frac_key: str, fail_key: str) -> dict:
    """plausibility / units: waive only if every failing feature has a small tail and a median on the right scale."""
    fail_at = cfg["audit"][fail_key]
    bad = [r for r in (check.get("table") or []) if (r.get(frac_key) or 0) > fail_at]
    if not bad:
        return {"action": "acknowledge", "justification": f"No feature above {_pct(fail_at)}; {check['detail']}".strip()}
    tails, problems = [], []
    for r in bad:
        f, frac, med = r["feature"], float(r[frac_key]), _median(features, r["feature"])
        unit = NUMERIC_FIELDS.get(f, {}).get("unit", "")
        desc = f"{f} {_pct(frac)}" + (f" of {r['n']:,}" if r.get("n") else "") + (f", median {med:g} {unit}".rstrip() if med is not None else "")
        (tails if frac <= TAIL_MAX and _in_range(f, med) else problems).append(desc)
    if problems:
        return {"action": "fix", "justification": "Likely units or code mapping, not a tail of real values: "
                + "; ".join(problems) + ". Fix the extraction (unit rule or code list) and rebuild features."}
    why = ("values out of range are set to missing in the release" if frac_key == "implausible_frac"
           else "the median is on the expected scale, so the flagged values are real extremes, not unconverted units")
    return {"action": "waive", "justification": f"Tail of real values on the right scale: {'; '.join(tails)}. "
            f"All other features are below {_pct(fail_at)}; {why}."}


def _icd_step(labels: pd.DataFrame | None) -> dict:
    if labels is None or not {"event_date", "family", "source"} <= set(labels.columns):
        return {"action": "fix", "justification": "Labels not available to check the yearly trend; check manually."}
    L = labels[labels["source"] == "code"]
    y = pd.to_datetime(L["event_date"]).dt.year
    t = pd.crosstab(y, L["family"])
    y0 = ICD10_TRANSITION.year                                   # 2015: the switch is in October
    need = [y0 - 1, y0, y0 + 1, y0 + 2]
    if not all(v in t.index for v in need):
        return {"action": "fix", "justification": "Too few years around October 2015 to separate a coding step from the trend."}
    steps, worst = {}, 1.0
    for fam in t.columns:
        c = t[fam].astype(float)
        if (c[need] <= 0).any():
            continue
        trend = math.sqrt((c[y0] / c[y0 - 1]) * (c[y0 + 2] / c[y0 + 1]))   # growth in the years around the switch
        step = (c[y0 + 1] / c[y0]) / trend                                  # 2016 vs 2015, relative to that growth
        steps[fam] = step
        worst = max(worst, step, 1 / step)
    detail = ", ".join(f"{f} x{s:.2f}" for f, s in steps.items())
    if worst <= STEP_TOLERANCE:
        return {"action": "acknowledge", "justification": f"Yearly candidate counts show no step at the Oct 2015 ICD-10 switch "
                f"(2016/2015 change relative to the neighbouring years' growth: {detail}). The rate change reflects cohort "
                "accrual 2012-2019, ageing and EHR capture, not code mapping."}
    return {"action": "fix", "justification": f"Step at the ICD-10 switch beyond x{STEP_TOLERANCE} of the trend ({detail}): "
            "check the ICD-9/ICD-10 code lists for these families."}


ACK = {
    "events_after_death": "Candidates dated after death are {v:,} ({detail}); under the fail limit. Adjudication reviews them "
                          "and follow-up ends at death.",
    "uncertain_fraction": "Families with many uncertain auto-labels: {detail}. They lack a single confirmatory test; "
                          "uncertain candidates go to cardiologist adjudication by design.",
    "lf_conflict": "Labelling functions disagree in: {detail}. Codes, labs and notes are expected to conflict; conflicting "
                   "candidates are prioritised for adjudication.",
    "missingness": "Features often missing: {detail}. Measured only when clinically indicated; handled as missing in "
                   "training (indicator or imputation) and reported in the model card.",
    "missing_disparity": "Missingness differs between groups ({detail}); reported in the model card and checked in "
                         "subgroup performance.",
    "annotation_yield": "{detail}. Extraction accuracy is measured in annotation review before release.",
}


def propose(check: dict, cfg: dict, features: pd.DataFrame | None = None, labels: pd.DataFrame | None = None) -> dict:
    cid, status = check["id"], check["status"]
    if cid == "plausibility":
        p = _feature_tail(check, cfg, features, "implausible_frac", "implausible_fail")
    elif cid == "units":
        p = _feature_tail(check, cfg, features, "suspect_frac", "unit_suspect_fail")
    elif cid == "icd_drift":
        p = _icd_step(labels)
    elif cid == "annotation_yield" and isinstance(check.get("value"), (int, float)) and "LVEF" in (check.get("detail") or ""):
        p = {"action": "acknowledge", "justification": ACK[cid].format(detail=check["detail"].split(". Accuracy")[0])}
    elif status == "fail":
        p = {"action": "fix", "justification": f"Failure: {check['title']} ({check.get('detail') or check.get('value')}). "
             "Fix the data or extraction; failures are not waived without specific evidence."}
    elif cid in ACK:
        p = {"action": "acknowledge", "justification": ACK[cid].format(v=check.get("value") or 0, detail=check.get("detail") or check.get("value"))}
    else:      # no standard explanation (e.g. labelling functions that never fire, incidence out of range): look first
        p = {"action": "fix", "justification": f"Investigate before accepting: {check['title']} = {check.get('value')} "
             f"(threshold {check.get('threshold')}). {check.get('detail') or ''} No standard explanation applies; "
             "review it manually (acknowledge or waive with your own reason) once understood.".strip()}
    if status == "fail" and p["action"] == "acknowledge":     # failures can only be waived
        p["action"] = "waive"
    if status == "warn" and p["action"] == "waive":
        p["action"] = "acknowledge"
    return {"check_id": cid, "status": status, "title": check["title"], "value": check.get("value"),
            "threshold": check.get("threshold"), **p}


def proposals(items: list[dict], cfg: dict, run_path: str | Path, raw_dir: str | Path) -> list[dict]:
    """Proposals for the unresolved, non-passing audit items (gates.audit_items)."""
    open_items = [i for i in items if i["status"] != "pass" and not i["resolved"]]
    if not open_items:
        return []
    ids = {i["id"] for i in open_items}
    features = labels = None
    if ids & {"plausibility", "units"}:
        cols = [r["feature"] for i in open_items if i["id"] in ("plausibility", "units") for r in (i.get("table") or [])]
        f = Path(raw_dir) / "features.parquet"
        if f.exists():
            import pyarrow.parquet as pq
            have = set(pq.read_schema(f).names)
            features = pd.read_parquet(f, columns=sorted({c for c in cols if c in have}))
    if "icd_drift" in ids:
        labels = pd.read_parquet(Path(run_path) / "labels.parquet", columns=["event_date", "family", "source"])
    return [propose(i, cfg, features, labels) for i in open_items]
