"""Rule-based extraction of coronary artery calcium (Agatston) score and LVEF
from radiology / echo / cardiac MRI report text.

Validate on a hand-labelled sample of ~200 reports in the Discover workspace
before relying on it (precision/recall per field), and extend the patterns to
your institution's report templates.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

_CAC = [
    re.compile(r"(?:total\s+)?(?:coronary\s+(?:artery\s+)?)?calcium\s+score\s*(?:\(agatston\))?\s*(?:is|of|=|:)?\s*(\d{1,4}(?:\.\d+)?)", re.I),
    re.compile(r"agatston\s+(?:score\s*)?(?:is|of|=|:)?\s*(\d{1,4}(?:\.\d+)?)", re.I),
    re.compile(r"(\d{1,4}(?:\.\d+)?)\s*agatston", re.I),
    re.compile(r"calci(?:um|fication)\b[^.]{0,60}?\bscore\s*(?:is|of|=|:)?\s*(\d{1,4}(?:\.\d+)?)", re.I),
]
_CAC_ZERO = re.compile(r"no\s+(?:detectable\s+)?coronary\s+(?:artery\s+)?calcification|calcium\s+score\s*(?:is|of|=|:)?\s*zero", re.I)

_EF_RANGE = re.compile(r"(?:lvef|ejection\s+fraction|\bef\b)[^0-9%]{0,40}?(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\s*(?:%|percent\b)", re.I)
_EF_SINGLE = re.compile(r"(?:lvef|ejection\s+fraction|\bef\b)[^0-9%]{0,40}?(\d{1,2}(?:\.\d)?)\s*(?:%|percent\b)", re.I)


def extract_cac(text: str) -> float | None:
    if not text:
        return None
    if _CAC_ZERO.search(text):
        return 0.0
    for pat in _CAC:
        m = pat.search(text)
        if m:
            v = float(m.group(1))
            if 0 <= v <= 10000:
                return v
    return None


def extract_lvef(text: str) -> float | None:
    if not text:
        return None
    m = _EF_RANGE.search(text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if 5 <= lo <= hi <= 85:
            return (lo + hi) / 2
    m = _EF_SINGLE.search(text)
    if m:
        v = float(m.group(1))
        if 5 <= v <= 85:
            return v
    return None


def parse_imaging_reports(notes: pd.DataFrame) -> pd.DataFrame:
    """notes: person_id, note_date, note_text -> per-person latest cac_score / lvef."""
    n = notes.copy()
    n["cac_score"] = n["note_text"].map(extract_cac)
    n["lvef"] = n["note_text"].map(extract_lvef)
    n = n.sort_values("note_date")
    out = n.groupby("person_id").agg(
        cac_score=("cac_score", lambda s: s.dropna().iloc[-1] if s.notna().any() else np.nan),
        lvef=("lvef", lambda s: s.dropna().iloc[-1] if s.notna().any() else np.nan),
    )
    return out
