"""Clinical mention detection with NegEx-style context (negated / historical /
hypothetical / affirmed). Deliberately transparent rules: every auto-label
can be traced back to the sentence that drove it, and cardiologists see the
same sentences highlighted in the adjudication packet.
"""
from __future__ import annotations

import re
from typing import Iterable

PATTERNS: dict[str, re.Pattern] = {k: re.compile(v, re.I) for k, v in {
    "ami": r"\b(n-?stemi|stemi|myocardial infarction|acute mi|heart attack|acute coronary syndrome)\b",
    "nstemi": r"\bn-?stemi\b|non-st[- ]elevation myocardial infarction",
    "stemi": r"(?<![n-])\bstemi\b|st[- ]elevation myocardial infarction",
    "type2_mi": r"\btype (2|ii) (mi|myocardial infarction)\b|demand ischemia",
    "stroke": r"\b(stroke|cva|cerebrovascular accident|cerebral infarct\w*|transient ischemic attack|tia)\b",
    "stroke_ischemic_img": r"\b(acute (ischemic )?infarct\w*|restricted diffusion|acute ischemi\w+|large vessel occlusion)\b",
    "stroke_hem_img": r"\b(intracranial hemorrhage|intraparenchymal hemorrhage|intracerebral hemorrhage|subarachnoid hemorrhage|ich)\b",
    "no_acute_intracranial": r"\bno acute intracranial (abnormality|process|findings?|hemorrhage or infarct)\b",
    "hf_acute": (r"\b(acute (on chronic )?((systolic|diastolic|combined( systolic and diastolic)?) )?(congestive )?heart failure|"
                 r"decompensated (congestive )?heart failure|chf exacerbation|heart failure exacerbation|"
                 r"cardiogenic pulmonary edema|iv diuresis)\b"),
    "hf_any": r"\b(heart failure|chf)\b",
    "arrest": (r"\b(cardiac arrest|ventricular fibrillation|vf arrest|rosc|return of spontaneous circulation|"
               r"sustained ventricular tachycardia|pulseless)\b"),
    "nsvt": r"\b(non-?sustained ventricular tachycardia|nsvt|run of (non-?sustained )?vt)\b",
    "vf": r"\b(ventricular fibrillation|vf arrest)\b",
    "af": r"\b(atrial fibrillation|atrial flutter|afib|a-fib)\b",
    "flutter": r"\batrial flutter\b",
}.items()}

_SENT = re.compile(r"(?<=[.!?])\s+|\n+|;\s*")
_NEG_PRE = re.compile(r"\b(no|not|denies|denied|negative for|without|free of|ruled out|absence of|never had|resolved)\b", re.I)
_NEG_POST = re.compile(r"^\W*(?:\w+\W+){0,3}?(?:was|were|is|has been|been)?\W*(ruled out|excluded|unlikely|"
                       r"not (?:seen|identified|present|confirmed))\b", re.I)
_HYPO_PRE = re.compile(r"\b(rule out|r/o|evaluate for|evaluation for|eval for|concern for|possible|possibly|suspected|"
                       r"suspicion of|question of|risk of|screen(?:ing)? for|assess for)\b|\?", re.I)
_HIST_PRE = re.compile(r"\b(history of|h/o|hx of|prior|previous|old|remote|family history|status post|s/p)\b", re.I)

STATUSES = ("affirmed", "negated", "historical", "hypothetical")


def sentences(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for part in _SENT.split(text or ""):
        i = text.find(part, pos)
        if part.strip():
            out.append((i, part))
        pos = i + len(part)
    return out


def _context(sentence: str, start: int, end: int) -> str:
    pre = sentence[max(0, start - 45):start]
    pre = re.split(r",|:|\bbut\b|\bhowever\b", pre)[-1]
    post = sentence[end:end + 45]
    if _NEG_PRE.search(pre) or _NEG_POST.search(post):
        return "negated"
    if _HYPO_PRE.search(pre):
        return "hypothetical"
    if _HIST_PRE.search(pre):
        return "historical"
    return "affirmed"


def find_mentions(text: str, key: str) -> list[dict]:
    """Return [{start, end, status, sentence}] for pattern `key` in `text`."""
    pat = PATTERNS[key]
    out = []
    for off, sent in sentences(text):
        for m in pat.finditer(sent):
            out.append({"start": off + m.start(), "end": off + m.end(), "status": _context(sent, m.start(), m.end()),
                        "sentence": sent.strip()})
    return out


def summarize(texts: Iterable[str], keys: Iterable[str]) -> dict[str, dict[str, int]]:
    texts = [t for t in texts if t]
    res = {}
    for k in keys:
        c = dict.fromkeys(STATUSES, 0)
        for t in texts:
            for m in find_mentions(t, k):
                c[m["status"]] += 1
        res[k] = c
    return res


def highlight_segments(text: str, keys: Iterable[str], max_excerpts: int = 6, radius: int = 220) -> list[list[dict]]:
    """Excerpts around mentions as safe segments [{t, h}] (h = highlighted) for the adjudication UI.
    Context status is NOT included (adjudicators judge it themselves)."""
    spans = []
    for k in keys:
        for m in PATTERNS[k].finditer(text or ""):
            spans.append((m.start(), m.end()))
    spans.sort()
    excerpts, last_end = [], -1
    for s, e in spans:
        if s < last_end:
            continue
        a, b = max(0, s - radius), min(len(text), e + radius)
        inner = [(x, y) for x, y in spans if x >= a and y <= b]
        segs, cur = [], a
        for x, y in inner:
            if x < cur:
                continue
            if x > cur:
                segs.append({"t": text[cur:x], "h": False})
            segs.append({"t": text[x:y], "h": True})
            cur = y
        if cur < b:
            segs.append({"t": text[cur:b], "h": False})
        if a > 0:
            segs.insert(0, {"t": "…", "h": False})
        if b < len(text):
            segs.append({"t": "…", "h": False})
        excerpts.append(segs)
        last_end = b
        if len(excerpts) >= max_excerpts:
            break
    return excerpts
