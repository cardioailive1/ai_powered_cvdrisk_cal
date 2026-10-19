"""Synthetic RAW curation dataset in exactly the format extract.py writes, plus a
hidden ground-truth table so the whole curation pipeline (auto-label -> audit
-> review -> adjudication -> release -> train) can be tested without patient
data. It deliberately includes realistic coding errors: chest-pain rule-outs
coded as MI, stroke mimics, NSVT coded as VT, history-of codes, post-MI
follow-up visits re-using I21, "evaluate for AF" codes, missed codes, ICD-9
before Oct 2015 and report phrasings the NLP misses.

    python -m training.synthetic_raw --n 60000 --out data/raw

truth*.parquet + SYNTHETIC marker are used ONLY by the reader simulator and tests.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from .imaging_nlp import parse_imaging_reports
from .synthetic import generate

ICD10_START = pd.Timestamp("2015-10-01")
URL_HS_TNT = 14.0


class _B:
    def __init__(self, seed: int):
        self.r = np.random.default_rng(seed)
        self.codes, self.procs, self.labs, self.notes, self.truth = [], [], [], [], []
        self.note_id = 1_000_000

    def p(self, x: float) -> bool:
        return bool(self.r.random() < x)

    def code(self, pid, fam, d, icd10, icd9, visit, primary):
        self.codes.append({"person_id": pid, "family": fam, "event_date": d.normalize(),
                           "code": icd10 if d >= ICD10_START else icd9,
                           "visit_concept_id": visit, "condition_status_concept_id": 32902 if primary else 32908})

    def proc(self, pid, cat, d, code):
        self.procs.append({"person_id": pid, "category": cat, "procedure_date": d.normalize(), "code": code})

    def lab(self, pid, analyte, t, value, unit, range_high=np.nan):
        self.labs.append({"person_id": pid, "analyte": analyte, "measured_at": t, "value": round(float(value), 2),
                          "unit": unit, "range_high": range_high})

    def note(self, pid, d, title, text):
        self.note_id += 1
        self.notes.append({"person_id": pid, "note_id": self.note_id, "note_date": d.normalize(), "note_title": title, "note_text": text})

    def troponin(self, pid, d, pattern):
        rh = URL_HS_TNT if self.p(0.7) else np.nan
        if pattern == "rise_fall":
            peak = self.r.uniform(4, 120)
            mult = [peak * self.r.uniform(0.15, 0.4), peak, peak * self.r.uniform(0.4, 0.8)]
        elif pattern == "flat_elevated":
            base = self.r.uniform(1.3, 3.0)
            mult = [base, base * self.r.uniform(0.95, 1.08), base * self.r.uniform(0.95, 1.08)]
        else:
            mult = [self.r.uniform(0.2, 0.8) for _ in range(3)]
        for k, hrs in enumerate([0, 6, 24][: 2 + int(self.p(0.7))]):
            self.lab(pid, "hs_troponin_t", d + pd.Timedelta(hours=8 + hrs), mult[k] * URL_HS_TNT, "ng/L", rh)

    def t(self, pid, fam, d, event, sub):
        self.truth.append({"person_id": pid, "family": fam, "date": d.normalize(), "event": event, "subtype": sub})


def _visit(r, p_inpt, p_ed):
    u = r.random()
    return 9201 if u < p_inpt else (9203 if u < p_inpt + p_ed else 9202)


def _ami(b, pid, d, true_event):
    r = b.r
    if true_event:
        sub = r.choice(["nstemi", "stemi", "type2"], p=[0.62, 0.30, 0.08])
        icd10 = {"stemi": r.choice(["I21.09", "I21.19", "I21.29"]), "nstemi": "I21.4", "type2": "I21.A1"}[sub]
        icd9 = {"stemi": "410.11", "nstemi": "410.71", "type2": "410.71"}[sub]
        coded = b.p(0.9)
        pci = sub != "type2" and b.p(0.55)
        if coded:
            b.code(pid, "ami", d, icd10, icd9, _visit(r, 0.85, 0.12), b.p(0.8))
        if b.p(0.95):
            b.troponin(pid, d, "rise_fall")
        if b.p(0.75) or pci:
            b.proc(pid, "coronary_angiography", d + pd.Timedelta(days=int(r.integers(0, 2))), "93458")
        if pci:
            b.proc(pid, "pci", d + pd.Timedelta(days=int(r.integers(0, 2))), "92928")
        if b.p(0.3):
            b.note(pid, d, "ED note", "Presents with chest pressure. Admitted to rule out acute coronary syndrome.")
        if b.p(0.85):
            dx = {"stemi": "STEMI (inferior)", "nstemi": "NSTEMI",
                  "type2": "Type 2 myocardial infarction due to demand ischemia in the setting of sepsis"}[sub]
            b.note(pid, d + pd.Timedelta(days=3), "Discharge summary",
                   f"Discharge diagnosis: {dx}. High-sensitivity troponin T rose and peaked during admission. "
                   + ("Coronary angiography with PCI to the culprit lesion." if pci else "Managed medically."))
        b.t(pid, "ami", d, True, sub)
        if coded and b.p(0.4):  # post-MI clinic visit re-using the I21 code (not a new event)
            fd = d + pd.Timedelta(days=int(r.integers(10, 28)))
            b.code(pid, "ami", fd, icd10, icd9, 9202, False)
            b.t(pid, "ami", fd, False, None)
    else:
        kind = r.choice(["rule_out", "history", "elective_pci"], p=[0.55, 0.35, 0.10])
        if kind == "rule_out":
            b.code(pid, "ami", d, r.choice(["I21.4", "I21.9"]), "410.91", _visit(r, 0.5, 0.5), b.p(0.3))
            if b.p(0.95):
                b.troponin(pid, d, "normal")
            if b.p(0.8):
                b.note(pid, d + pd.Timedelta(days=1), "Discharge summary",
                       "Chest pain, likely musculoskeletal. Serial troponins negative. Acute coronary syndrome was ruled out.")
        elif kind == "history":
            b.code(pid, "ami", d, "I21.9", "410.90", 9202, False)
            if b.p(0.5):
                b.note(pid, d, "Clinic note", f"Routine follow-up. History of myocardial infarction in {d.year - int(r.integers(2, 12))}, stable on therapy.")
        else:
            b.proc(pid, "pci", d, "92928")
            if b.p(0.8):
                b.troponin(pid, d, "normal")
            b.note(pid, d, "Procedure note", "Elective PCI to the LAD for stable angina. No evidence of myocardial infarction.")
        b.t(pid, "ami", d, False, None)


def _stroke(b, pid, d, true_event, sub=None):
    r = b.r
    if true_event:
        icd10 = {"ischemic": "I63.9", "hemorrhagic": "I61.9", "tia": "G45.9"}[sub]
        icd9 = {"ischemic": "434.91", "hemorrhagic": "431", "tia": "435.9"}[sub]
        reperf = sub == "ischemic" and b.p(0.18)
        if b.p(0.9):
            b.code(pid, "stroke", d, icd10, icd9, _visit(r, 0.8, 0.18), b.p(0.8))
        if b.p(0.95):
            b.proc(pid, "head_ct", d, "70450")
        if b.p(0.6):
            b.proc(pid, "head_mri", d + pd.Timedelta(days=1), "70553")
        if reperf:
            b.proc(pid, "thrombolysis" if b.p(0.6) else "thrombectomy", d, "37195")
        if b.p(0.9):
            txt = {"ischemic": "MRI brain: Acute infarct in the left MCA territory with restricted diffusion.",
                   "hemorrhagic": "CT head: Acute intraparenchymal hemorrhage in the right basal ganglia.",
                   "tia": "MRI brain: No acute intracranial abnormality."}[sub]
            b.note(pid, d, "Radiology report", txt)
        if b.p(0.8):
            dx = {"ischemic": "acute ischemic stroke", "hemorrhagic": "intracerebral hemorrhage", "tia": "transient ischemic attack"}[sub]
            b.note(pid, d + pd.Timedelta(days=3), "Discharge summary", f"Discharge diagnosis: {dx}. Neurology consulted.")
        b.t(pid, "stroke", d, True, sub)
    else:
        if b.p(0.7):
            b.code(pid, "stroke", d, r.choice(["I63.9", "G45.9"]), "435.9", _visit(r, 0.3, 0.7), b.p(0.3))
            if b.p(0.9):
                b.proc(pid, "head_ct", d, "70450")
                b.note(pid, d, "Radiology report", "CT head: No acute intracranial abnormality.")
            if b.p(0.85):
                b.note(pid, d + pd.Timedelta(days=1), "ED note",
                       "Transient left arm numbness. Symptoms attributed to complicated migraine; stroke ruled out.")
        else:
            b.code(pid, "stroke", d, "I63.9", "434.91", 9202, False)
            if b.p(0.6):
                b.note(pid, d, "Clinic note", f"History of stroke in {d.year - int(r.integers(2, 10))} with residual weakness.")
        b.t(pid, "stroke", d, False, None)


def _hf(b, pid, d, true_event, sub=None):
    r = b.r
    if true_event:
        icd10 = {"hfref": "I50.23", "hfpef": "I50.33", "hfmref": "I50.9"}[sub]
        icd9 = {"hfref": "428.23", "hfpef": "428.33", "hfmref": "428.0"}[sub]
        ef = {"hfref": r.integers(18, 39), "hfmref": r.integers(42, 49), "hfpef": r.integers(52, 66)}[sub]
        b.code(pid, "hf", d, icd10, icd9, _visit(r, 0.75, 0.1), b.p(0.7))
        if b.p(0.85):
            if b.p(0.5):
                b.lab(pid, "bnp", d + pd.Timedelta(hours=6), r.uniform(450, 2500), "pg/mL")
            else:
                b.lab(pid, "nt_probnp", d + pd.Timedelta(hours=6), r.uniform(2500, 15000), "pg/mL")
        if b.p(0.85):
            kind = {"hfref": "systolic", "hfpef": "diastolic", "hfmref": "combined systolic and diastolic"}[sub]
            b.note(pid, d + pd.Timedelta(days=3), "Discharge summary",
                   f"Admitted with acute on chronic {kind} heart failure. Treated with IV diuresis with good response.")
        if b.p(0.7):
            ed = d + pd.Timedelta(days=int(r.integers(0, 4)))
            b.proc(pid, "echo", ed, "93306")
            b.note(pid, ed, "Echocardiogram report", f"Transthoracic echo: LVEF estimated at {ef}%.")
        b.t(pid, "hf", d, True, sub)
    else:
        b.code(pid, "hf", d, "I50.9", "428.0", 9202, False)
        if b.p(0.7):
            b.lab(pid, "bnp", d, r.uniform(15, 85), "pg/mL")
        if b.p(0.8):
            b.note(pid, d, "Clinic note",
                   "Dyspnea on exertion; evaluate for heart failure. BNP normal. Echo with LVEF 60%. No evidence of heart failure.")
        b.t(pid, "hf", d, False, None)


def _scd(b, pid, d, true_event):
    r = b.r
    if true_event:
        sub = r.choice(["vf", "vt", "arrest"], p=[0.55, 0.2, 0.25])
        icd10 = {"vf": "I49.01", "vt": "I47.2", "arrest": "I46.9"}[sub]
        icd9 = {"vf": "427.41", "vt": "427.1", "arrest": "427.5"}[sub]
        cpr = b.p(0.8)
        if b.p(0.9):
            b.code(pid, "scd", d, icd10, icd9, _visit(r, 0.5, 0.5), b.p(0.7))
        if cpr:
            b.proc(pid, "cpr", d, "92950")
        if b.p(0.85):
            txt = {"vf": "Out-of-hospital cardiac arrest with ventricular fibrillation on arrival; ROSC after 2 shocks.",
                   "vt": "Sustained ventricular tachycardia with hemodynamic compromise requiring emergent termination.",
                   "arrest": "Cardiac arrest, PEA; return of spontaneous circulation after 8 minutes of CPR."}[sub]
            b.note(pid, d, "ED note", txt)
        b.t(pid, "scd", d, True, sub)
    else:
        b.code(pid, "scd", d, "I47.2", "427.1", _visit(r, 0.6, 0.0), b.p(0.2))
        if b.p(0.85):
            b.note(pid, d, "Progress note", "Telemetry: 6-beat run of nonsustained VT, asymptomatic. Continue beta-blocker.")
        b.t(pid, "scd", d, False, None)


def _af(b, pid, d, true_event):
    r = b.r
    if true_event:
        sub = r.choice(["af", "flutter", "postoperative"], p=[0.85, 0.08, 0.07])
        if sub == "postoperative":
            b.proc(pid, "cardiac_surgery", d - pd.Timedelta(days=int(r.integers(2, 6))), "33533")
        icd10, icd9 = ("I48.92", "427.32") if sub == "flutter" else ("I48.91", "427.31")
        visit = 9201 if sub == "postoperative" else _visit(r, 0.35, 0.1)
        b.code(pid, "af", d, icd10, icd9, visit, b.p(0.5))
        if b.p(0.7):
            b.code(pid, "af", d + pd.Timedelta(days=int(r.integers(30, 200))), icd10, icd9, 9202, False)
        if b.p(0.85):
            b.note(pid, d, "ECG report", "ECG: atrial flutter with 2:1 block." if sub == "flutter"
                   else f"ECG: atrial fibrillation with rapid ventricular response, rate {int(r.integers(105, 160))}.")
        if b.p(0.12):
            b.proc(pid, "cardioversion", d + pd.Timedelta(days=int(r.integers(1, 20))), "92960")
        b.t(pid, "af", d, True, sub)
    else:
        b.code(pid, "af", d, "I48.91", "427.31", 9202, False)
        if b.p(0.8):
            b.note(pid, d, "Clinic note",
                   "Palpitations; evaluate for atrial fibrillation. Holter monitor showed sinus rhythm, no atrial fibrillation.")
        b.t(pid, "af", d, False, None)


def _baseline_note(b, pid, d, cac, lvef):
    r = b.r
    parts, truth = [], {"cac_true": None, "lvef_true": None}
    if cac is not None:
        v = int(cac)
        truth["cac_true"] = float(v)
        if v == 0:
            parts.append(r.choice(["Coronary artery calcium score: 0.", "No coronary artery calcification is identified."]))
        elif b.p(0.06):
            parts.append(f"Coronary calcification present in the LAD; calcium burden quantified, score {v}.")  # NLP-hard
        else:
            parts.append(r.choice([f"Total coronary calcium score: {v} (Agatston).", f"Agatston score {v}.",
                                   f"CT calcium scoring. The total Agatston score is {v}."]))
    if lvef is not None:
        v = int(lvef)
        truth["lvef_true"] = float(v)
        if b.p(0.06):
            parts.append(f"Ejection fraction visually estimated at approximately {v} percent.")  # NLP-hard
        else:
            parts.append(r.choice([f"LVEF estimated at {v}%.", f"Left ventricular ejection fraction {v} %.",
                                   f"The ejection fraction is {v - 2}-{v + 3}%."]))
            if "-" in parts[-1]:
                truth["lvef_true"] = float(v) + 0.5
    b.note_id += 1
    return {"person_id": pid, "note_id": b.note_id, "note_date": d.normalize(), "note_text": " ".join(parts)}, {"note_id": b.note_id, **truth}


def generate_raw(n: int = 60000, seed: int = 11) -> dict[str, pd.DataFrame]:
    base = generate(n, seed)
    b = _B(seed + 1)
    r = b.r
    base["index_date"] = pd.Timestamp("2012-01-01") + pd.to_timedelta(r.integers(0, 8 * 365, n), unit="D")
    D = lambda row, days: row["index_date"] + pd.Timedelta(days=int(days))  # noqa: E731

    counts = dict.fromkeys(["ami", "stroke", "hf", "scd", "af"], 0)
    for row in base.to_dict("records"):
        pid = row["person_id"]
        if not np.isnan(row["days_ami"]):
            _ami(b, pid, D(row, row["days_ami"]), True)
            counts["ami"] += 1
        if not np.isnan(row["days_any_stroke"]):
            sub = "ischemic" if row["days_ischemic_stroke"] == row["days_any_stroke"] else (
                "hemorrhagic" if row["days_hemorrhagic_stroke"] == row["days_any_stroke"] else "tia")
            _stroke(b, pid, D(row, row["days_any_stroke"]), True, sub)
            counts["stroke"] += 1
        if not np.isnan(row["days_hf"]):
            sub = "hfref" if not np.isnan(row["days_hfref"]) else "hfpef"
            if b.p(0.12):
                sub = "hfmref"
            _hf(b, pid, D(row, row["days_hf"]), True, sub)
            counts["hf"] += 1
        if not np.isnan(row["days_scd"]):
            _scd(b, pid, D(row, row["days_scd"]), True)
            counts["scd"] += 1
        if not np.isnan(row["days_af"]) and not row["afib"]:
            _af(b, pid, D(row, row["days_af"]), True)
            counts["af"] += 1

    fu = base["followup_days"].to_numpy()
    false_rate = {"ami": 0.30, "stroke": 0.30, "hf": 0.30, "scd": 0.35, "af": 0.30}
    fns = {"ami": _ami, "stroke": _stroke, "hf": _hf, "scd": _scd, "af": _af}
    for fam, k in counts.items():
        m = int(k * false_rate[fam])
        for i in r.integers(0, n, m):
            if fam == "af" and base.at[i, "afib"]:
                continue
            day = int(r.integers(1, max(2, int(fu[i]))))
            fns[fam](b, int(base.at[i, "person_id"]), base.at[i, "index_date"] + pd.Timedelta(days=day), False)

    # Baseline imaging reports (CAC / LVEF) -> features come from NLP extraction, like the real pipeline
    bnotes, bt = [], []
    for row in base.to_dict("records"):
        cac = None if np.isnan(row["cac_score"]) else row["cac_score"]
        ef = None if np.isnan(row["lvef"]) else row["lvef"]
        if cac is None and ef is None:
            continue
        note, truth = _baseline_note(b, row["person_id"], row["index_date"] - pd.Timedelta(days=int(r.integers(20, 700))), cac, ef)
        bnotes.append(note)
        bt.append(truth)
    bnotes = pd.DataFrame(bnotes)
    feats = base.drop(columns=[c for c in base.columns if c.startswith("days_")] + ["cac_score", "lvef"])
    img = parse_imaging_reports(bnotes)
    feats = feats.merge(img, left_on="person_id", right_index=True, how="left")

    return {
        "features": feats, "candidate_codes": pd.DataFrame(b.codes), "evidence_procedures": pd.DataFrame(b.procs),
        "evidence_labs": pd.DataFrame(b.labs), "event_notes": pd.DataFrame(b.notes), "baseline_imaging_notes": bnotes,
        "truth": pd.DataFrame(b.truth), "truth_annotations": pd.DataFrame(bt),
    }


def write_raw(out: str | Path, tables: dict[str, pd.DataFrame]) -> None:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(out / f"{name}.parquet", index=False)
    (out / "SYNTHETIC").write_text("Synthetic data — not derived from patients. Ground truth present for simulation/testing only.\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60000)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", default="data/raw")
    a = ap.parse_args()
    t = generate_raw(a.n, a.seed)
    write_raw(a.out, t)
    print({k: len(v) for k, v in t.items()})


if __name__ == "__main__":
    main()
