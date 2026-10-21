"""Expanded candidate outcome code lists: every ICD-10-CM and ICD-9-CM diagnosis code that creates a candidate
outcome event, with its official description, the subtype it suggests, and the codes deliberately left out.

The RULES below are the clinical definition (reviewed by the lead cardiologist). `python -m training.codelists`
expands them against the official CMS code sets and writes training/code_lists/candidate_codes.csv, which is
committed. The extraction SQL matches diagnosis codes EXACTLY against that file, so the list clinicians review is
the list that runs. The CSV is all the Mayo Clinic Platform workspace needs (no code-set libraries there).

    python -m training.codelists            # regenerate the CSV (needs: pip install simple-icd-10-cm icd9cms)
    python -m training.codelists --check    # fail if the committed CSV is out of date (CI)

Sources: ICD-10-CM tabular list, CMS/CDC release of 1 April 2026 (via simple-icd-10-cm); ICD-9-CM final release,
CMS version 32 (FY2015; frozen when ICD-10-CM replaced it on 1 October 2015). ICD-10-CM header codes are listed
too, because some were billable before a later split (I47.2 until FY2023, I48.1 and I48.2 until FY2020) and
appear in older records.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

CSV_PATH = Path(__file__).parent / "code_lists" / "candidate_codes.csv"
COLUMNS = ["family", "vocabulary", "code", "description", "status", "subtype_hint", "included", "reason"]

# ------------------------------------------------------------------ clinical definition (prefixes on dotted codes)
# include: code prefixes that create a candidate. exclude: prefixes inside an include that do NOT (kept in the CSV
# with included=no and the reason). Every change here needs the lead cardiologist's sign-off.
RULES: dict[str, dict] = {
    "ami": {
        "icd10": {"include": ["I21", "I22"], "exclude": {}},
        "icd9": {"include": ["410"],
                 "exclude": {"410.?2": "Subsequent episode of care (follow-up of an earlier MI, not a new event)"}},
    },
    "stroke": {
        "icd10": {"include": ["I60", "I61", "I62", "I63", "G45"],
                  "exclude": {"I62.0": "Nontraumatic subdural hemorrhage: not a stroke under the AHA/ASA 2013 definition",
                              "I62.1": "Nontraumatic extradural hemorrhage: not a stroke under the AHA/ASA 2013 definition",
                              "G45.4": "Transient global amnesia: not cerebral ischemia"}},
        "icd9": {"include": ["430", "431", "432", "433.?1", "434.?1", "435", "436"],
                 "exclude": {"432.0": "Nontraumatic extradural hemorrhage: not a stroke under the AHA/ASA 2013 definition",
                             "432.1": "Subdural hemorrhage: not a stroke under the AHA/ASA 2013 definition",
                             "435.2": "Subclavian steal syndrome: not a transient ischemic attack"}},
    },
    "hf": {
        "icd10": {"include": ["I50", "I11.0", "I13.0", "I13.2", "I09.81", "I97.13"], "exclude": {}},
        "icd9": {"include": ["428", "402.01", "402.11", "402.91", "404.01", "404.03", "404.11", "404.13", "404.91", "404.93",
                             "398.91"], "exclude": {}},
    },
    "scd": {
        "icd10": {"include": ["I46", "I49.01", "I49.02", "I47.2", "I97.12"], "exclude": {}},
        "icd9": {"include": ["427.5", "427.41", "427.42", "427.1"], "exclude": {}},
    },
    "af": {
        "icd10": {"include": ["I48"], "exclude": {}},
        "icd9": {"include": ["427.31", "427.32"], "exclude": {}},
    },
}

# Subtype each code suggests (the auto-labeller combines it with notes, imaging and LVEF; readers decide).
SUBTYPE_HINTS: dict[str, list[tuple[str, str]]] = {   # first matching prefix wins
    "ami": [("I21.A1", "type2"), ("I21.A9", "unspecified"), ("I21.0", "stemi"), ("I21.1", "stemi"), ("I21.2", "stemi"),
            ("I21.3", "stemi"), ("I21.4", "nstemi"), ("I21.9", "unspecified"), ("I21.B", "unspecified"), ("I22.2", "nstemi"), ("I22.0", "stemi"), ("I22.1", "stemi"),
            ("I22.8", "stemi"), ("I22.9", "stemi"),
            ("410.7", "nstemi"), ("410.9", "unspecified"), ("410", "stemi")],
    "stroke": [("I60", "hemorrhagic"), ("I61", "hemorrhagic"), ("I62", "hemorrhagic"), ("I63", "ischemic"), ("G45", "tia"),
               ("430", "hemorrhagic"), ("431", "hemorrhagic"), ("432", "hemorrhagic"), ("433", "ischemic"), ("434", "ischemic"),
               ("435", "tia"), ("436", "unknown")],
    "hf": [("I50.2", "hfref"), ("I50.4", "hfref"), ("I50.3", "hfpef"), ("428.2", "hfref"), ("428.4", "hfref"), ("428.3", "hfpef")],
    "scd": [("I49.01", "vf"), ("I49.02", "vf"), ("427.41", "vf"), ("427.42", "vf"), ("I47.2", "vt"), ("427.1", "vt"),
            ("I46", "arrest"), ("I97.12", "arrest"), ("427.5", "arrest")],
    "af": [("I48.3", "flutter"), ("I48.4", "flutter"), ("I48.92", "flutter"), ("427.32", "flutter")],
}


def _match(code: str, prefix: str) -> bool:
    """Prefix match where '?' stands for any one character (e.g. '410.?2' = fifth digit 2)."""
    if len(code) < len(prefix):
        return False
    return all(p == "?" or p == c for p, c in zip(prefix, code))


def subtype_hint(family: str, code: str) -> str:
    for prefix, sub in SUBTYPE_HINTS.get(family, []):
        if _match(code, prefix):
            return sub
    return ""


def _icd10_codes() -> list[tuple[str, str, str]]:
    import simple_icd_10_cm as cm
    stems = tuple({p.replace(".", "")[:3] for spec in RULES.values() for p in spec["icd10"]["include"]})
    out = []
    for raw in cm.get_all_codes(False):
        if not raw.startswith(stems):
            continue
        if not cm.is_category_or_subcategory(raw) and not cm.is_extended_subcategory(raw):
            continue
        code = raw if len(raw) <= 3 else raw[:3] + "." + raw[3:]
        status = "billable" if cm.is_leaf(raw) else "header"
        out.append((code, cm.get_description(raw), status))
    return out


def _icd9_codes() -> list[tuple[str, str, str]]:
    import os

    import icd9cms
    import pandas as pd
    x = pd.read_excel(os.path.join(os.path.dirname(icd9cms.__file__), "data", "CMS32_DESC_LONG_SHORT_DX.xlsx"), dtype=str)
    out = []
    for raw, desc in zip(x["DIAGNOSIS CODE"], x["LONG DESCRIPTION"]):
        raw = str(raw).strip()
        if raw[:1] in ("E", "V") or len(raw) <= 3:
            code = raw
        else:
            code = raw[:3] + "." + raw[3:]
        out.append((code, str(desc).strip(), "billable"))
    return out


def expand() -> list[dict]:
    vocab = {"icd10": ("ICD10CM", _icd10_codes()), "icd9": ("ICD9CM", _icd9_codes())}
    rows = []
    for fam, spec in RULES.items():
        for key, (vname, codes) in vocab.items():
            rule = spec[key]
            for code, desc, status in codes:
                if not any(_match(code, p) for p in rule["include"]):
                    continue
                reason = next((r for p, r in rule["exclude"].items() if _match(code, p)), "")
                if status == "header":
                    status = "header (not billable in FY2026; may appear in older records)"
                rows.append({"family": fam, "vocabulary": vname, "code": code, "description": desc, "status": status,
                             "subtype_hint": subtype_hint(fam, code), "included": "no" if reason else "yes", "reason": reason})
    rows.sort(key=lambda r: (list(RULES).index(r["family"]), r["vocabulary"] != "ICD10CM", r["code"]))
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def load(path: Path = CSV_PATH, included_only: bool = True) -> list[dict]:
    """The committed expanded list (what the extraction SQL uses)."""
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r["included"] == "yes"] if included_only else rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed CSV differs from the rules")
    a = ap.parse_args(argv)
    text = to_csv(expand())
    if a.check:
        if not CSV_PATH.exists() or CSV_PATH.read_text() != text:
            print(f"{CSV_PATH} is out of date: run python -m training.codelists", file=sys.stderr)
            return 1
        print("candidate code list is up to date")
        return 0
    CSV_PATH.parent.mkdir(exist_ok=True)
    CSV_PATH.write_text(text)
    rows = list(csv.DictReader(io.StringIO(text)))
    for fam in RULES:
        inc = [r for r in rows if r["family"] == fam and r["included"] == "yes"]
        exc = [r for r in rows if r["family"] == fam and r["included"] == "no"]
        print(f"{fam:7} included {len(inc):4} (ICD-10-CM {sum(r['vocabulary'] == 'ICD10CM' for r in inc)}, "
              f"ICD-9-CM {sum(r['vocabulary'] == 'ICD9CM' for r in inc)}), excluded {len(exc)}")
    print(f"wrote {CSV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
