"""Automatic labelling of candidate outcome events (weak supervision).

1. build_episodes(): post-index outcome codes -> candidate EPISODES per outcome
   family (codes within 7 days collapse into one episode). Procedures with no
   nearby code create "evidence-only" candidates, to catch events codes missed.
2. Labelling functions (LFs) look at the evidence around each candidate
   (troponin / natriuretic peptide kinetics, imaging and resuscitation
   procedures, note text with negation, visit type, diagnosis position) and
   each votes +1 (supports event), -1 (against) or 0 (abstain).
3. A transparent log-odds label model combines the votes into p_auto:
       p >= confirm_threshold  -> auto "confirmed"
       p <= reject_threshold   -> auto "rejected"
       otherwise               -> "uncertain"  (goes to cardiologist adjudication)
4. Subtypes (STEMI/NSTEMI/type 2, ischemic/hemorrhagic/TIA, HFrEF/HFmrEF/HFpEF,
   VF/VT, post-operative AF) are annotated from codes, reports and LVEF.

Weights in DEFAULT_LABEL_MODEL are expert-set starting values. After
adjudication, `python -m curation.pipeline refit-label-model` proposes
weights learned from the cardiologists' decisions (a new labelling run with
a new audit and adjudication sample is then required to use them).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import codesets as cs
from .imaging_nlp import extract_lvef
from .nlp import summarize

EPISODE_GAP_DAYS = 7
EVIDENCE_WINDOW = {"ami": (-1, 3), "stroke": (-1, 3), "hf": (-2, 3), "scd": (-1, 1), "af": (-30, 30)}
NOTE_WINDOW = {"ami": (-1, 7), "stroke": (-1, 7), "hf": (-2, 7), "scd": (-1, 7), "af": (-3, 30)}
LVEF_WINDOW = (-7, 30)
MENTION_KEYS = {
    "ami": ["ami", "nstemi", "stemi", "type2_mi"],
    "stroke": ["stroke", "stroke_ischemic_img", "stroke_hem_img", "no_acute_intracranial"],
    "hf": ["hf_acute", "hf_any"],
    "scd": ["arrest", "nsvt", "vf"],
    "af": ["af", "flutter"],
}


# ------------------------------------------------------------------ episodes
def _cid(pid: Any, fam: str, d: pd.Timestamp, src: str) -> str:
    return "C" + hashlib.sha1(f"{pid}|{fam}|{d:%Y-%m-%d}|{src}".encode()).hexdigest()[:12]


def build_episodes(codes: pd.DataFrame, procs: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    idx = features[["person_id", "index_date"]].copy()
    idx["index_date"] = pd.to_datetime(idx["index_date"])
    c = codes.merge(idx, on="person_id", how="inner")
    c["event_date"] = pd.to_datetime(c["event_date"])
    c = c[c["event_date"] > c["index_date"]]
    c["acute"] = c["visit_concept_id"].isin(cs.INPATIENT_ED_VISITS)
    c["primary"] = c["condition_status_concept_id"].isin(cs.PRIMARY_STATUS)
    c = c.sort_values(["person_id", "family", "event_date"])

    gap = np.timedelta64(EPISODE_GAP_DAYS, "D")
    # Episode = run of codes for one patient+family with gaps <= 7 days between consecutive codes
    c = c.reset_index(drop=True)
    same = (c["person_id"].eq(c["person_id"].shift())) & (c["family"].eq(c["family"].shift()))
    new_ep = ~same | ((c["event_date"] - c["event_date"].shift()) > pd.Timedelta(days=EPISODE_GAP_DAYS))
    c["ep"] = new_ep.cumsum()
    c["primary_acute"] = c["primary"] & c["acute"]
    g = c.groupby("ep")
    cand = g.agg(person_id=("person_id", "first"), family=("family", "first"), event_date=("event_date", "min"),
                 acute=("acute", "any"), primary_any=("primary", "any"), primary_acute=("primary_acute", "any"),
                 n_code_rows=("code", "size")).reset_index()
    cand["primary"] = np.where(cand["acute"], cand["primary_acute"], cand["primary_any"])
    codes_by_ep = (c.drop_duplicates(["ep", "code"]).sort_values(["ep", "code"]).groupby("ep")["code"]
                   .agg(lambda x: ";".join(x.astype(str))))
    cand["codes"] = cand["ep"].map(codes_by_ep)
    cand["source"] = "code"
    # AF: number of distinct AF code dates within 365 days of episode start
    af_dates = {pid: np.unique(v.to_numpy()) for pid, v in c.loc[c["family"] == "af"].groupby("person_id")["event_date"]}
    cnt = np.zeros(len(cand), dtype=int)
    for i in np.flatnonzero((cand["family"] == "af").to_numpy()):
        arr = af_dates[cand.at[i, "person_id"]]
        st = np.datetime64(cand.at[i, "event_date"])
        cnt[i] = np.searchsorted(arr, st + np.timedelta64(365, "D"), "right") - np.searchsorted(arr, st, "left")
    cand["af_code_dates_365"] = cnt
    cand["candidate_id"] = [_cid(p_, f_, d_, "code") for p_, f_, d_ in zip(cand["person_id"], cand["family"], cand["event_date"])]
    cand["primary"] = cand["primary"].astype(bool)
    cand = cand[["candidate_id", "person_id", "family", "event_date", "source", "codes", "acute", "primary",
                 "n_code_rows", "af_code_dates_365"]]

    # evidence-only candidates from trigger procedures without a nearby code episode
    p = procs[procs["category"].isin(cs.TRIGGER_PROCEDURES)].merge(idx, on="person_id")
    p["procedure_date"] = pd.to_datetime(p["procedure_date"])
    p = p[p["procedure_date"] >= p["index_date"]].sort_values("procedure_date")
    p["family"] = p["category"].map(cs.TRIGGER_PROCEDURES)
    ep_dates = {} if cand.empty else {k: v["event_date"].to_numpy() for k, v in cand.groupby(["person_id", "family"])}
    extra: list[dict] = []
    last: dict = {}
    for r in p.itertuples(index=False):
        key = (r.person_id, r.family)
        d = np.datetime64(r.procedure_date)
        near = ep_dates.get(key)
        if near is not None and (np.abs(near - d) <= gap).any():
            continue
        if key in last and d - last[key] <= gap:
            continue
        last[key] = d
        if r.procedure_date <= r.index_date:
            continue
        extra.append({"candidate_id": _cid(r.person_id, r.family, r.procedure_date, "evidence"), "person_id": r.person_id,
                      "family": r.family, "event_date": r.procedure_date, "source": "evidence_only",
                      "codes": "", "acute": False, "primary": False, "n_code_rows": 0, "af_code_dates_365": 0})
    if extra:
        cand = pd.concat([cand, pd.DataFrame(extra)], ignore_index=True)
    if cand.empty:
        return cand
    cand = cand.merge(idx, on="person_id")
    cand["days_from_index"] = (cand["event_date"] - cand["index_date"]).dt.days
    return cand.drop(columns=["index_date"]).sort_values(["person_id", "family", "event_date"]).reset_index(drop=True)


# ------------------------------------------------------------------ evidence
_TROP_FACTOR = {"ng/ml": 1000, "ug/l": 1000, "µg/l": 1000, "mcg/l": 1000, "ng/l": 1, "pg/ml": 1}
_NP_FACTOR = {"pg/ml": 1, "ng/l": 1, "pmol/l": None}


def normalize_labs(labs: pd.DataFrame, sex_by_person: dict | None = None) -> pd.DataFrame:
    """Troponins -> ng/L with (sex-specific) URL; BNP / NT-proBNP -> pg/mL."""
    if labs.empty:
        return labs.assign(value_n=[], url=[])
    L = labs.copy()
    L["measured_at"] = pd.to_datetime(L["measured_at"])
    unit = L["unit"].fillna("").str.lower().str.replace(" ", "", regex=False)
    trop = L["analyte"].str.contains("troponin")
    f_trop = unit.map(_TROP_FACTOR)
    # unitless conventional troponin < 5 is almost certainly ng/mL
    f_trop = f_trop.where(f_trop.notna(), np.where(L["analyte"].str.startswith("hs_"), 1, np.where(L["value"] < 5, 1000, 1)))
    f_np = np.where(unit == "pmol/l", np.where(L["analyte"] == "bnp", 3.47, 8.457), 1.0)
    factor = np.where(trop, f_trop, f_np).astype(float)
    L["value_n"] = L["value"] * factor
    rh = pd.to_numeric(L.get("range_high"), errors="coerce")
    sex = L["person_id"].map(sex_by_person or {}).fillna("male")   # unknown sex -> higher (conservative) limit
    default = [cs.DEFAULT_URL_NG_L.get(a, {}).get(sx if sx in ("male", "female") else "male", np.nan)
               for a, sx in zip(L["analyte"], sex)]
    L["url"] = np.where(trop, np.where(rh > 0, rh * factor, default), np.nan)
    return L


def _window(idx: tuple | None, d: pd.Timestamp, w: tuple[int, int]) -> list[dict]:
    """idx = (sorted datetime64 array, records) for one person -> records within [d+w0, d+w1 end of day]."""
    if idx is None:
        return []
    dates, recs = idx
    lo = np.datetime64(d + pd.Timedelta(days=w[0]))
    hi = np.datetime64(d + pd.Timedelta(days=w[1], hours=23, minutes=59, seconds=59))
    i, j = np.searchsorted(dates, lo, "left"), np.searchsorted(dates, hi, "right")
    return recs[i:j]


def troponin_summary(labs: list[dict]) -> dict:
    t = [x for x in labs if "troponin" in x["analyte"]]
    if not t:
        return {"measured": False, "elevated": False, "rise_fall": False, "peak_x_url": None, "n": 0}
    ratios = [x["value_n"] / x["url"] for x in t if x["url"] and x["url"] > 0]
    elevated = any(r > 1 for r in ratios)
    rise_fall = False
    for analyte in {x["analyte"] for x in t}:
        rows = [x for x in t if x["analyte"] == analyte]           # records are time-ordered
        v = np.array([x["value_n"] for x in rows])
        u = rows[0]["url"]
        if len(v) < 2 or not v.max() > u:
            continue
        if (v.max() - v.min()) / max(v.max(), 1e-9) >= 0.2:
            rise_fall = True
        if analyte == "hs_troponin_t":                              # Mayo TRPS absolute delta rule
            ts = [pd.Timestamp(x["measured_at"]) for x in rows]
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    hrs = (ts[j] - ts[i]).total_seconds() / 3600
                    if any(hrs <= h and abs(v[j] - v[i]) >= d for h, d in cs.HS_TNT_DELTA_NG_L):
                        rise_fall = True
    return {"measured": True, "elevated": elevated, "rise_fall": rise_fall,
            "peak_x_url": float(max(ratios)) if ratios else None, "n": len(t)}


def np_summary(labs: list[dict], age: float) -> dict:
    n = [x for x in labs if x["analyte"] in ("bnp", "nt_probnp")]
    if not n:
        return {"measured": False, "elevated": False, "low": False}
    nt_rule_in = 450 if age < 50 else (900 if age <= 75 else 1800)
    elev = any((x["analyte"] == "bnp" and x["value_n"] >= 100) or (x["analyte"] == "nt_probnp" and x["value_n"] >= nt_rule_in) for x in n)
    low = all((x["analyte"] == "bnp" and x["value_n"] < 100) or (x["analyte"] == "nt_probnp" and x["value_n"] < 300) for x in n)
    return {"measured": True, "elevated": elev, "low": low}


# ------------------------------------------------------------------ LFs
@dataclass
class LF:
    name: str
    families: tuple[str, ...]
    description: str
    fn: Callable[[dict], int]


def _m(ctx, key, status="affirmed") -> int:
    return ctx["mentions"].get(key, {}).get(status, 0)


def _has_proc(ctx, *cats) -> bool:
    return any(p["category"] in cats for p in ctx["procs"])


ACUTE = ("ami", "stroke", "hf", "scd")
LFS: list[LF] = [
    LF("acute_visit", ACUTE, "Code recorded in an inpatient/ED visit (+) vs outpatient only (−)",
       lambda c: 0 if c["source"] != "code" else (1 if c["acute"] else -1)),
    LF("primary_dx", ACUTE, "Coded as primary/principal diagnosis", lambda c: 1 if c["primary"] else 0),
    LF("no_dx_code", ("ami", "stroke", "hf", "scd", "af"), "Evidence-only candidate (no outcome diagnosis code)",
       lambda c: -1 if c["source"] == "evidence_only" else 0),
    # AMI
    LF("trop_rise_fall", ("ami",), "Troponin >99th-pct URL with rise/fall ≥20%", lambda c: 1 if c["trop"]["rise_fall"] else 0),
    LF("trop_elevated_flat", ("ami",), "Troponin elevated without documented rise/fall",
       lambda c: 1 if c["trop"]["elevated"] and not c["trop"]["rise_fall"] else 0),
    LF("trop_normal", ("ami",), "Troponin measured and all values ≤ URL", lambda c: -1 if c["trop"]["measured"] and not c["trop"]["elevated"] else 0),
    LF("coronary_procedure", ("ami",), "Coronary angiography or PCI in window",
       lambda c: 1 if _has_proc(c, "pci", "coronary_angiography") else 0),
    LF("note_mi", ("ami",), "MI affirmed (+) / negated (−) in notes",
       lambda c: 1 if _m(c, "ami") else (-1 if _m(c, "ami", "negated") else 0)),
    LF("note_mi_historical", ("ami",), "MI mentioned only as history",
       lambda c: -1 if _m(c, "ami", "historical") and not _m(c, "ami") else 0),
    # Stroke
    LF("neuroimaging", ("stroke",), "Head CT/MRI in window (+); acute visit without imaging (−)",
       lambda c: 1 if _has_proc(c, "head_ct", "head_mri") else (-1 if c["acute"] else 0)),
    LF("img_infarct", ("stroke",), "Acute infarct / restricted diffusion reported", lambda c: 1 if _m(c, "stroke_ischemic_img") else 0),
    LF("img_hemorrhage", ("stroke",), "Acute intracranial hemorrhage reported", lambda c: 1 if _m(c, "stroke_hem_img") else 0),
    LF("img_negative", ("stroke",), "'No acute intracranial abnormality' without infarct/hemorrhage",
       lambda c: -1 if _m(c, "no_acute_intracranial") and not (_m(c, "stroke_ischemic_img") or _m(c, "stroke_hem_img")) else 0),
    LF("note_stroke", ("stroke",), "Stroke/TIA affirmed (+) / negated or history-only (−)",
       lambda c: 1 if _m(c, "stroke") else (-1 if _m(c, "stroke", "negated") or _m(c, "stroke", "historical") else 0)),
    LF("reperfusion", ("stroke",), "Thrombolysis or thrombectomy", lambda c: 1 if _has_proc(c, "thrombolysis", "thrombectomy") else 0),
    # HF
    LF("natriuretic_peptide", ("hf",), "BNP/NT-proBNP above rule-in (+) / all below rule-out (−)",
       lambda c: 1 if c["np"]["elevated"] else (-1 if c["np"]["measured"] and c["np"]["low"] else 0)),
    LF("note_hf", ("hf",), "Acute/decompensated HF affirmed (+) / HF negated (−)",
       lambda c: 1 if _m(c, "hf_acute") else (-1 if _m(c, "hf_any", "negated") or _m(c, "hf_any", "hypothetical") else 0)),
    LF("echo", ("hf",), "Echocardiogram or LVEF reported in window",
       lambda c: 1 if _has_proc(c, "echo") or c["lvef"] is not None else 0),
    # SCD
    LF("resuscitation", ("scd",), "CPR or defibrillation in window", lambda c: 1 if _has_proc(c, "cpr", "rhythm_restoration") else 0),
    LF("note_arrest", ("scd",), "Arrest/VF/sustained VT/ROSC affirmed (+) / negated (−)",
       lambda c: 1 if _m(c, "arrest") else (-1 if _m(c, "arrest", "negated") else 0)),
    LF("note_nsvt", ("scd",), "Only non-sustained VT documented", lambda c: -1 if _m(c, "nsvt") and not _m(c, "arrest") else 0),
    LF("died_within_1d", ("scd",), "Death within 1 day", lambda c: 1 if c["death_gap"] is not None and 0 <= c["death_gap"] <= 1 else 0),
    # AF
    LF("repeat_af_codes", ("af",), "AF coded on ≥2 dates within 1 year (+) / single outpatient code (−)",
       lambda c: 1 if c["af_code_dates_365"] >= 2 or (c["acute"] and c["primary"]) else (-1 if c["source"] == "code" and not c["acute"] else 0)),
    LF("note_af", ("af",), "AF/flutter affirmed (+) / negated or only 'evaluate for' (−)",
       lambda c: 1 if _m(c, "af") else (-1 if _m(c, "af", "negated") or _m(c, "af", "hypothetical") else 0)),
    LF("cardioversion", ("af",), "Cardioversion performed", lambda c: 1 if _has_proc(c, "cardioversion") else 0),
]
LF_BY_NAME = {lf.name: lf for lf in LFS}

# log-odds weights: [weight when vote=+1, weight when vote=-1 (subtracted)]
DEFAULT_LABEL_MODEL: dict = {
    "version": "expert-v1",
    "thresholds": {"confirm": 0.8, "reject": 0.2},
    "families": {
        "ami": {"bias": -1.0, "lfs": {"acute_visit": [1.0, 1.5], "primary_dx": [0.8, 0], "no_dx_code": [0, 0.5],
                "trop_rise_fall": [2.5, 0], "trop_elevated_flat": [0.5, 0], "trop_normal": [0, 3.0],
                "coronary_procedure": [1.0, 0], "note_mi": [1.5, 2.0], "note_mi_historical": [0, 1.5]}},
        "stroke": {"bias": -1.0, "lfs": {"acute_visit": [1.0, 1.5], "primary_dx": [0.8, 0], "no_dx_code": [0, 0.5],
                   "neuroimaging": [0.8, 1.5], "img_infarct": [2.0, 0], "img_hemorrhage": [2.5, 0],
                   "img_negative": [0, 0.7], "note_stroke": [1.5, 2.0], "reperfusion": [1.5, 0]}},
        "hf": {"bias": -1.0, "lfs": {"acute_visit": [1.0, 1.0], "primary_dx": [0.8, 0], "no_dx_code": [0, 0.5],
               "natriuretic_peptide": [1.5, 2.0], "note_hf": [1.5, 1.5], "echo": [0.3, 0]}},
        "scd": {"bias": -1.0, "lfs": {"acute_visit": [1.0, 1.0], "primary_dx": [0.8, 0], "no_dx_code": [0, 0.5],
                "resuscitation": [2.5, 0], "note_arrest": [1.5, 1.5], "note_nsvt": [0, 2.5], "died_within_1d": [1.0, 0]}},
        "af": {"bias": 0.0, "lfs": {"no_dx_code": [0, 0.5], "repeat_af_codes": [1.5, 0.5], "note_af": [2.0, 1.5],
               "cardioversion": [1.0, 0]}},
    },
}


def model_sha(model: dict) -> str:
    return hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()


def score(model: dict, family: str, votes: dict[str, int]) -> float:
    fm = model["families"][family]
    z = fm["bias"]
    for name, (wp, wn) in fm["lfs"].items():
        v = votes.get(name, 0)
        z += wp if v > 0 else (-wn if v < 0 else 0.0)
    return 1 / (1 + math.exp(-z))


# ------------------------------------------------------------------ subtypes
_HINTS: dict | None = None


def code_hints(codes_l: list[str]) -> set[str]:
    """Subtypes suggested by the candidate's diagnosis codes (training/code_lists/candidate_codes.csv)."""
    global _HINTS
    if _HINTS is None:
        from .codelists import load
        _HINTS = {r["code"]: r["subtype_hint"] for r in load(included_only=False) if r["subtype_hint"]}
    return {_HINTS[c] for c in codes_l if c in _HINTS}


def subtype(fam: str, ctx: dict, codes: str, cardiac_surgery_before: bool) -> str:
    codes_l = codes.split(";") if codes else []
    hints = code_hints(codes_l)
    if fam == "ami":
        if "type2" in hints or _m(ctx, "type2_mi"):
            return "type2"
        if "stemi" in hints or (_m(ctx, "stemi") and not _m(ctx, "nstemi")):
            return "stemi"
        if "nstemi" in hints or _m(ctx, "nstemi"):
            return "nstemi"
        return "unspecified"
    if fam == "stroke":
        if _m(ctx, "stroke_hem_img") or "hemorrhagic" in hints:
            return "hemorrhagic"
        if _m(ctx, "stroke_ischemic_img") or "ischemic" in hints:
            return "ischemic"
        if "tia" in hints:
            return "tia"
        return "unknown"
    if fam == "hf":
        ef = ctx["lvef"]
        if ef is not None:
            return "hfref" if ef <= 40 else ("hfmref" if ef < 50 else "hfpef")
        if "hfref" in hints:
            return "hfref"
        if "hfpef" in hints:
            return "hfpef"
        return "unknown"
    if fam == "scd":
        if _m(ctx, "vf") or "vf" in hints:
            return "vf"
        if "vt" in hints and _m(ctx, "arrest"):
            return "vt"
        if "arrest" in hints or _m(ctx, "arrest"):
            return "arrest"
        return "unknown"
    if fam == "af":
        if cardiac_surgery_before:
            return "postoperative"
        flutter_codes = bool(codes_l) and all(code_hints([c]) == {"flutter"} for c in codes_l)
        # "atrial flutter" also matches the AF pattern, so flutter wins only if every AF mention is a flutter mention
        if flutter_codes or (_m(ctx, "flutter") and _m(ctx, "flutter") >= _m(ctx, "af")):
            return "flutter"
        return "af"
    return "unknown"


# ------------------------------------------------------------------ main entry
class Evidence:
    """Per-person, time-sorted evidence index (labs, procedures, notes) for fast window lookups."""

    def __init__(self, labs: pd.DataFrame, procs: pd.DataFrame, notes: pd.DataFrame, sex_by_person: dict | None = None):
        self.labs = normalize_labs(labs, sex_by_person)
        procs = procs.copy()
        procs["procedure_date"] = pd.to_datetime(procs["procedure_date"])
        notes = notes.copy()
        notes["note_date"] = pd.to_datetime(notes["note_date"])
        self.procs, self.notes = procs, notes
        self._labs = self._index(self.labs, "measured_at")
        self._procs = self._index(procs, "procedure_date")
        self._notes = self._index(notes, "note_date")

    @staticmethod
    def _index(df: pd.DataFrame, col: str) -> dict:
        if df is None or df.empty:
            return {}
        df = df.sort_values(["person_id", col], kind="stable")
        out = {}
        for pid, g in df.groupby("person_id", sort=False):
            out[pid] = (g[col].to_numpy(dtype="datetime64[ns]"), g.to_dict("records"))
        return out

    def labs_for(self, pid, d, w):
        return _window(self._labs.get(pid), d, w)

    def procs_for(self, pid, d, w):
        return _window(self._procs.get(pid), d, w)

    def notes_for(self, pid, d, w):
        return _window(self._notes.get(pid), d, w)


def candidate_context(row: dict, ev: Evidence, feats_row: dict) -> dict:
    fam, pid, d = row["family"], row["person_id"], pd.Timestamp(row["event_date"])
    labs = ev.labs_for(pid, d, EVIDENCE_WINDOW[fam])
    procs = ev.procs_for(pid, d, EVIDENCE_WINDOW[fam])
    notes = ev.notes_for(pid, d, NOTE_WINDOW[fam])
    texts = [n["note_text"] for n in notes]
    age = float(feats_row.get("age", 60)) + row.get("days_from_index", 0) / 365.25
    lvef = None
    if fam == "hf":
        lv_notes = ev.notes_for(pid, d, LVEF_WINDOW)
        vals = [v for v in (extract_lvef(n["note_text"]) for n in lv_notes) if v is not None]
        lvef = vals[0] if vals else None
    dd = feats_row.get("death_days")
    death_gap = None if dd is None or (isinstance(dd, float) and math.isnan(dd)) else dd - row.get("days_from_index", 0)
    return {**row, "labs": labs, "procs": procs, "mentions": summarize(texts, MENTION_KEYS[fam]),
            "trop": troponin_summary(labs) if fam == "ami" else None,
            "np": np_summary(labs, age) if fam == "hf" else None, "lvef": lvef, "death_gap": death_gap}


def label_candidates(cands: pd.DataFrame, ev: Evidence, features: pd.DataFrame, model: dict | None = None) -> pd.DataFrame:
    model = model or copy.deepcopy(DEFAULT_LABEL_MODEL)
    th = model["thresholds"]
    feats = features.set_index("person_id")[["age", "death_days"]].to_dict("index")
    out = []
    for row in cands.to_dict("records"):
        fam = row["family"]
        ctx = candidate_context(row, ev, feats.get(row["person_id"], {}))
        votes = {lf.name: int(lf.fn(ctx)) for lf in LFS if fam in lf.families}
        p = score(model, fam, votes)
        surg = ev.procs_for(row["person_id"], pd.Timestamp(row["event_date"]), (-30, -1))
        st = subtype(fam, ctx, row["codes"], any(x["category"] == "cardiac_surgery" for x in surg))
        out.append({**row, "p_auto": p,
                    "auto_label": "confirmed" if p >= th["confirm"] else ("rejected" if p <= th["reject"] else "uncertain"),
                    "subtype_auto": st, "lvef_window": ctx["lvef"],
                    **{f"lf_{k}": v for k, v in votes.items()}})
    df = pd.DataFrame(out)
    lf_cols = [c for c in df.columns if c.startswith("lf_")]
    df[lf_cols] = df[lf_cols].fillna(0).astype("int8")
    return df


def lf_stats(labels: pd.DataFrame) -> dict:
    stats = {}
    for fam, g in labels.groupby("family"):
        rows = []
        for lf in LFS:
            col = f"lf_{lf.name}"
            if fam not in lf.families or col not in g:
                continue
            v = g[col]
            rows.append({"lf": lf.name, "description": lf.description, "coverage": float((v != 0).mean()),
                         "pos": float((v > 0).mean()), "neg": float((v < 0).mean())})
        conflict = 0.0
        pos_any = sum((g[f"lf_{r['lf']}"] > 0) for r in rows) > 0
        neg_any = sum((g[f"lf_{r['lf']}"] < 0) for r in rows) > 0
        conflict = float((pos_any & neg_any).mean())
        stats[fam] = {"n": int(len(g)), "lfs": rows, "conflict_rate": conflict,
                      "auto": {k: int(v) for k, v in g["auto_label"].value_counts().items()},
                      "subtypes": {k: int(v) for k, v in g.loc[g["auto_label"] != "rejected", "subtype_auto"].value_counts().items()},
                      "source": {k: int(v) for k, v in g["source"].value_counts().items()}}
    return stats


def mark_relevance(labels: pd.DataFrame, definitions: dict | None = None) -> pd.DataFrame:
    """A candidate is label-relevant if no EARLIER candidate of the same family for
    the same patient is auto-confirmed. Only the first event date enters training,
    so irrelevant candidates are not sent to adjudication (if an earlier confirmed
    event is overturned by a cardiologist, the release treats later unresolved
    candidates as unknown rather than guessing)."""
    L = labels.sort_values(["person_id", "family", "event_date"]).copy()
    d = definitions or {}
    counts_as_event = L["auto_label"] == "confirmed"
    # confirmed candidates that the outcome definitions exclude do not end the search for a first event
    if not d.get("ami_include_type2", False):
        counts_as_event &= ~((L["family"] == "ami") & (L["subtype_auto"] == "type2"))
    if d.get("af_exclude_postoperative", True):
        counts_as_event &= ~((L["family"] == "af") & (L["subtype_auto"] == "postoperative"))
    conf = counts_as_event.astype(int)
    prior = conf.groupby([L["person_id"], L["family"]]).cumsum() - conf
    L["label_relevant"] = prior == 0
    return L
