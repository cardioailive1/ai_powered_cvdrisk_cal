"""Build docs/CardioAI_MCP_Import_List.xlsx (the data import list sent to Mayo Clinic Platform) from the code sets.

    pip install -r requirements-dev.txt
    python docs/build_mcp_import_list.py docs/CardioAI_MCP_Import_List.xlsx
"""
import csv
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import openpyxl  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from openpyxl.worksheet.table import Table, TableStyleInfo  # noqa: E402

from cardioai_core.schema import AGE_MAX, AGE_MIN, CALCULATORS, FIELDS, INDEX_WINDOW  # noqa: E402
from training import codesets as cs  # noqa: E402
from training.labeling import EVIDENCE_WINDOW, LVEF_WINDOW, NOTE_WINDOW  # noqa: E402

OUT = Path(sys.argv[1])
CALC_NAMES = {"ami": "AMI", "stroke": "Stroke", "hf": "HF", "scd": "SCD", "af": "AF"}
ALL5 = "All 5"
FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="FF1F3A5F")
TITLE_FONT = Font(name=FONT, size=14, bold=True, color="FF1F3A5F")
NOTE_FONT = Font(name=FONT, size=10, italic=True, color="FF444444")
HDR_FONT = Font(name=FONT, size=10, bold=True, color="FFFFFFFF")
BODY = Font(name=FONT, size=10)
BOLD = Font(name=FONT, size=10, bold=True)
INPUT_FILL = PatternFill("solid", fgColor="FFFFF4CC")
THIN = Side(style="thin", color="FFD0D7E2")
WRAP = Alignment(wrap_text=True, vertical="top")

wb = openpyxl.Workbook()
wb.remove(wb.active)
TABLE_N = [0]


def sheet(name, title, note, headers, rows, widths, table=True, fill_cols=()):
    ws = wb.create_sheet(name)
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = note
    ws["A2"].font = NOTE_FONT
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
    ws.row_dimensions[2].height = max(30, 15 * (1 + len(note) // 140))
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=j, value=h)
        c.font, c.fill = HDR_FONT, HDR_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for i, r in enumerate(rows, 5):
        for j, v in enumerate(r, 1):
            c = ws.cell(row=i, column=j, value=v)
            c.font, c.alignment = BODY, WRAP
            c.border = Border(bottom=THIN)
            if j in fill_cols:
                c.fill = INPUT_FILL
    for j, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = "A5"
    if table and rows:
        TABLE_N[0] += 1
        ref = f"A4:{get_column_letter(len(headers))}{4 + len(rows)}"
        t = Table(displayName=f"T{TABLE_N[0]}_{re.sub(r'[^A-Za-z]', '', name)}", ref=ref)
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
        ws.add_table(t)
    ws.sheet_view.zoomScale = 100
    return ws


# --------------------------------------------------------------- code expansion helpers
def icd10_all():
    import simple_icd_10_cm as cm
    out = []
    for raw in cm.get_all_codes(False):
        if not (cm.is_category_or_subcategory(raw) or cm.is_extended_subcategory(raw)):
            continue
        code = raw if len(raw) <= 3 else raw[:3] + "." + raw[3:]
        out.append((code, raw))
    return out, cm


def icd9_all():
    import os

    import icd9cms
    import pandas as pd
    x = pd.read_excel(os.path.join(os.path.dirname(icd9cms.__file__), "data", "CMS32_DESC_LONG_SHORT_DX.xlsx"), dtype=str)
    out = []
    for raw, desc in zip(x["DIAGNOSIS CODE"], x["LONG DESCRIPTION"]):
        raw = str(raw).strip()
        cut = 4 if raw[:1] == "E" else 3
        code = raw if len(raw) <= cut else raw[:cut] + "." + raw[cut:]
        out.append((code, str(desc).strip()))
    return out


PCS_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # ICD-10-PCS never uses I or O


def expand_pcs(pattern):
    """'03C[G-V]3' -> ['03CG3', '03CH3', ...] (character classes only)."""
    out = [""]
    for lit, cls in re.findall(r"([^\[\]])|\[([^\]]+)\]", pattern):
        if lit:
            chars = lit
        else:
            chars = ""
            for a, b in re.findall(r"(.)(?:-(.))?", cls):
                chars += "".join(ch for ch in PCS_CHARS if a <= ch <= (b or a))
        out = [p + ch for p in out for ch in chars]
    return out


def expand_cpt(pattern):
    rx = re.compile(f"^(?:{pattern})")
    return [f"{n:05d}" for n in range(100000) if rx.match(f"{n:05d}")]


def compress(codes):
    """Consecutive numeric CPT codes -> 'a–b' ranges."""
    nums = sorted(int(c) for c in codes)
    out, start, prev = [], None, None
    for n in nums + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            out.append(f"{start:05d}" if start == prev else f"{start:05d}–{prev:05d}")
            start = prev = n
    return ", ".join(out)


# --------------------------------------------------------------- reference text
LOINC_NAMES = {
    "8302-2": "Body height", "3137-7": "Body height Measured", "29463-7": "Body weight", "3141-9": "Body weight Measured",
    "39156-5": "Body mass index (BMI) [Ratio]", "8480-6": "Systolic blood pressure", "8459-0": "Systolic blood pressure--sitting",
    "8462-4": "Diastolic blood pressure", "8453-3": "Diastolic blood pressure--sitting",
    "2093-3": "Cholesterol [Mass/volume] in Serum or Plasma", "2085-8": "Cholesterol in HDL [Mass/volume] in Serum or Plasma",
    "13457-7": "Cholesterol in LDL [Mass/volume] in Serum or Plasma by calculation",
    "18262-6": "Cholesterol in LDL [Mass/volume] in Serum or Plasma by Direct assay",
    "2089-1": "Cholesterol in LDL [Mass/volume] in Serum or Plasma", "2571-8": "Triglyceride [Mass/volume] in Serum or Plasma",
    "1558-6": "Fasting glucose [Mass/volume] in Serum or Plasma", "2345-7": "Glucose [Mass/volume] in Serum or Plasma",
    "4548-4": "Hemoglobin A1c/Hemoglobin.total in Blood", "17856-6": "Hemoglobin A1c/Hemoglobin.total in Blood by HPLC",
    "30522-7": "C reactive protein [Mass/volume] in Serum or Plasma by High sensitivity method",
    "1988-5": "C reactive protein [Mass/volume] in Serum or Plasma",
    "33914-3": "eGFR (MDRD) in Serum or Plasma", "62238-1": "eGFR (CKD-EPI) in Serum, Plasma or Blood",
    "98979-8": "eGFR (CKD-EPI 2021) in Serum, Plasma or Blood", "48642-3": "eGFR (MDRD) among non-blacks",
    "48643-1": "eGFR (MDRD) among blacks",
    "10839-9": "Troponin I.cardiac [Mass/volume] in Serum or Plasma", "42757-5": "Troponin I.cardiac [Mass/volume] in Blood",
    "49563-0": "Troponin I.cardiac [Mass/volume] in Serum or Plasma by Detection limit <= 0.01 ng/mL",
    "6598-7": "Troponin T.cardiac [Mass/volume] in Serum or Plasma",
    "89579-7": "Troponin I.cardiac [Mass/volume] in Serum or Plasma by High sensitivity method",
    "67151-1": "Troponin T.cardiac [Mass/volume] in Serum or Plasma by High sensitivity method (Mayo TRPS, Roche Gen 5)",
    "30934-4": "Natriuretic peptide B [Mass/volume] in Serum or Plasma", "42637-9": "Natriuretic peptide B [Mass/volume] in Blood",
    "33762-6": "NT-proBNP [Mass/volume] in Serum or Plasma",
    "83107-3": "NT-proBNP [Mass/volume] in Serum, Plasma or Blood by Immunoassay (Mayo PBNP1)",
}
FIELD = {f["key"]: f for f in FIELDS}
EVIDENCE_LABEL = {"troponin_i": ("Troponin I", "ami"), "troponin_t": ("Troponin T (conventional)", "ami"),
                  "hs_troponin_i": ("hs-Troponin I", "ami"), "hs_troponin_t": ("hs-Troponin T", "ami"),
                  "bnp": ("BNP", "hf"), "nt_probnp": ("NT-proBNP", "hf")}
EVIDENCE_UNITS = {"troponin": "ng/L or ng/mL (converted to ng/L)", "bnp": "pg/mL (pmol/L converted)", "nt_probnp": "pg/mL (pmol/L converted)"}

ATC_NAMES = {
    "C10AA": "HMG CoA reductase inhibitors (statins)", "C10BA": "Statin combinations with other lipid-modifying agents",
    "C10BX": "Statins, other combinations", "B01AC06": "Acetylsalicylic acid (aspirin)",
    "C09": "Agents acting on the renin–angiotensin system (ACE inhibitors, ARBs)", "C07": "Beta-blocking agents",
    "B01AA": "Vitamin K antagonists (e.g. warfarin)", "B01AE": "Direct thrombin inhibitors (e.g. dabigatran)",
    "B01AF": "Direct factor Xa inhibitors (e.g. apixaban, rivaroxaban)", "C02": "Antihypertensives (centrally/peripherally acting)",
    "C03": "Diuretics", "C08": "Calcium channel blockers",
}
DRUG_LABEL = {k: FIELD[k]["label"] for k in cs.DRUGS}

PROC_INFO = {   # label, family it informs, how it is used
    "pci": ("Percutaneous coronary intervention", "AMI", "Evidence (coronary_procedure vote); also creates an evidence-only candidate"),
    "coronary_angiography": ("Coronary angiography", "AMI", "Evidence (coronary_procedure vote)"),
    "head_ct": ("Head CT", "Stroke", "Evidence (neuroimaging vote)"),
    "head_mri": ("Head / brain MRI", "Stroke", "Evidence (neuroimaging vote)"),
    "echo": ("Echocardiogram", "HF", "Evidence (echo vote; LVEF for HFrEF/HFpEF subtype)"),
    "thrombolysis": ("Thrombolysis", "Stroke", "Evidence (reperfusion vote); also creates an evidence-only candidate"),
    "thrombectomy": ("Mechanical thrombectomy", "Stroke", "Evidence (reperfusion vote); also creates an evidence-only candidate"),
    "cpr": ("Cardiopulmonary resuscitation", "SCD", "Evidence (resuscitation vote); also creates an evidence-only candidate"),
    "rhythm_restoration": ("Defibrillation / cardioversion (ICD-10-PCS 5A2204Z)", "SCD", "Evidence (resuscitation vote)"),
    "cardioversion": ("Elective cardioversion", "AF", "Evidence (cardioversion vote); also creates an evidence-only candidate"),
    "icd_implant": ("Implantable cardioverter-defibrillator implant", "SCD", "Context shown to adjudicators"),
    "cardiac_surgery": ("Cardiac surgery (CABG, valve replacement)", "AF", "Rule O2: AF ≤ 30 days after cardiac surgery is post-operative, not new-onset"),
}
HISTORY_FIELD = {"smoking_current": "smoking (current)", "smoking_former": "smoking (former)", "diabetes_t1": "diabetes (type 1)",
                 "diabetes_t2": "diabetes (type 2)", "prediabetes": "diabetes (prediabetes)"}

# =============================================================== tabs
lb, drug_lb = 730, 365
i0, i1 = INDEX_WINDOW if isinstance(INDEX_WINDOW, (tuple, list)) else ("2012-01-01", "2019-12-31")

# ---- Labs & vitals
labs = []
for feat, codes in cs.LOINC.items():
    f = FIELD.get(feat)
    for c in codes:
        labs.append([ALL5, f["label"] if f else "BMI", "Model input (baseline)", "LOINC", c, LOINC_NAMES.get(c, ""),
                     f.get("unit", "") if f else "kg/m²", "measurement", f"Latest value in the {lb} days up to and including index",
                     "BMI is also computed from height and weight" if feat == "bmi" else ""])
for feat in ("total_chol", "hdl"):
    for c in cs.LOINC[feat]:
        labs.append([ALL5, FIELD[feat]["label"], "Eligibility (I4)", "LOINC", c, LOINC_NAMES.get(c, ""), "mg/dL", "measurement",
                     "Any value in the 365 days before index", "Inclusion I4: total or HDL cholesterol measured"])
for feat, codes in cs.EVIDENCE_LOINC.items():
    label, fam = EVIDENCE_LABEL[feat]
    for c in codes:
        unit = EVIDENCE_UNITS["troponin" if "troponin" in feat else feat]
        labs.append([CALC_NAMES[fam], label, "Adjudication evidence (after index)", "LOINC", c, LOINC_NAMES.get(c, ""), unit,
                     "measurement (incl. range_high)", f"All results after index; reviewed {EVIDENCE_WINDOW[fam][0]} to +{EVIDENCE_WINDOW[fam][1]} days around each candidate event",
                     "range_high is used as the upper reference limit when present" if "troponin" in feat else ""])

# ---- History diagnoses (expanded)
print("expanding history codes …")
i10, cm = icd10_all()
i9 = icd9_all()
hist = []
for flag, spec in cs.HISTORY.items():
    label = HISTORY_FIELD.get(flag) or FIELD[flag]["label"]
    for vocab, pats, universe in (("ICD10CM", spec["icd10"], i10), ("ICD9CM", spec["icd9"], i9)):
        rx = re.compile("^(?:" + "|".join(pats) + ")")
        for code, extra in universe:
            if not rx.match(code):
                continue
            if vocab == "ICD10CM":
                desc, status = cm.get_description(extra), ("billable" if cm.is_leaf(extra) else "header")
            else:
                desc, status = extra, "billable"
            window = ("Within 730 days before index = current; older = former" if flag == "smoking_current"
                      else "Any time on or before index")
            hist.append([ALL5, label, "Model input (history flag)", vocab, code, desc, status,
                         "condition_occurrence + observation", window])
hist_counts = {}
for r in hist:
    hist_counts[(r[1], r[3])] = hist_counts.get((r[1], r[3]), 0) + 1

# ---- Outcome candidate codes
with (REPO / "training/code_lists/candidate_codes.csv").open() as f:
    cand = list(csv.DictReader(f))
outc = [[CALC_NAMES[r["family"]], r["vocabulary"], r["code"], r["description"], r["status"], r["subtype_hint"] or "",
         r["included"], r["reason"], "condition_occurrence", "Any date after index (models use 30-day, 1-year, 5-year horizons)"]
        for r in cand]

# ---- Procedures
print("expanding procedure codes …")
procs = []
for key, spec in cs.PROCEDURES.items():
    label, fam, use = PROC_INFO[key]
    cpt = sorted({c for p in spec["CPT4"] for c in expand_cpt(p)})
    if cpt:
        procs.append([fam, label, "CPT4", compress(cpt), len(cpt), ", ".join(spec["CPT4"]), use, "procedure_occurrence",
                      f"After index; reviewed {EVIDENCE_WINDOW[fam.lower()][0]} to +{EVIDENCE_WINDOW[fam.lower()][1]} days around candidates"
                      if key != "cardiac_surgery" else "After index; 30 days before an AF candidate"])
    pcs = sorted({c for p in spec["ICD10PCS"] for c in expand_pcs(p)})
    if pcs:
        procs.append([fam, label, "ICD10PCS", ", ".join(c if len(c) == 7 else c + "*" for c in pcs), len(pcs),
                      ", ".join(spec["ICD10PCS"]), use, "procedure_occurrence",
                      f"After index; reviewed {EVIDENCE_WINDOW[fam.lower()][0]} to +{EVIDENCE_WINDOW[fam.lower()][1]} days around candidates"
                      if key != "cardiac_surgery" else "After index; 30 days before an AF candidate"])

# ---- Medications
meds = []
for k, prefixes in cs.DRUGS.items():
    for p in prefixes:
        meds.append([ALL5, DRUG_LABEL[k], "ATC", p, ATC_NAMES[p], "drug_exposure (RxNorm concepts that descend from the ATC class via concept_ancestor)",
                     f"Any exposure in the {drug_lb} days before index", "Ingredient-level mapping; all routes and formulations"])

# ---- Notes & reports
notes = [
    ["All 5", "Coronary artery calcium (Agatston)", "Model input (baseline)", "OMOP note (radiology reports = FACT_RADIOLOGY.RADIOLOGY_REPORT)",
     "note_id, person_id, note_date, note_title, note_text", f"{lb} days up to index", "Text filter: agatston | calcium score", "Radiology reports"],
    ["All 5", "LV ejection fraction", "Model input (baseline)", "OMOP note + native FACT_ECHO_TEST.ECHO_NOTE",
     "ECHO_NOTE (confirmed); patient key, echo date, echo id (to confirm)", f"{lb} days up to index", "Text filter: ejection fraction | lvef", "Echo and imaging reports"],
    ["AMI, Stroke, HF, SCD, AF", "Event-window clinical notes", "Adjudication evidence", "OMOP note", "note_id, person_id, note_date, note_title, note_text",
     "−3 to +30 days around each candidate (labelling reads " + "; ".join(f"{CALC_NAMES[k]} {a} to +{b}" for k, (a, b) in NOTE_WINDOW.items()) + ")",
     "Keyword prefilter: " + cs.NOTE_PREFILTER.replace("\\b", ""), "Discharge summaries, progress notes, ED notes, imaging reports"],
    ["HF", "Event-window echo reports", "Adjudication evidence (HFrEF / HFpEF subtype)", "native FACT_ECHO_TEST.ECHO_NOTE", "as above",
     f"{LVEF_WINDOW[0]} to +{LVEF_WINDOW[1]} days around each candidate", "None (all echo reports in the window)", "Echo reports"],
    ["Stroke", "Neuroimaging reports", "Adjudication evidence", "OMOP note (FACT_RADIOLOGY)", "as above", "−3 to +30 days around each candidate",
     "Keyword prefilter (as above)", "Head CT / MRI reports: infarct, hemorrhage, negative study"],
]

# ---- Demographics, visits, concepts
conc = [
    [ALL5, "Age", "person", "year_of_birth, month_of_birth, day_of_birth (birth_datetime)", "—", f"Age at index; eligible {AGE_MIN}–{AGE_MAX}", "Model input; eligibility I1/E1/E2"],
    [ALL5, "Sex", "person", "gender_concept_id", "8507 = male, 8532 = female", "Others excluded (E3)", "Model input; eligibility I5"],
    [ALL5, "Race", "person", "race_concept_id", "8527 = white, 8516 = black, 8515 = asian; any other = other", "", "Model input"],
    [ALL5, "Ethnicity", "person", "ethnicity_concept_id", f"{cs.HISPANIC_ETHNICITY} = Hispanic or Latino (→ race 'hispanic')", "", "Model input"],
    [ALL5, "Index visit", "visit_occurrence", "visit_occurrence_id, visit_concept_id, visit_start_date",
     "9202 = outpatient visit", f"{i0} to {i1}; one random eligible visit per patient", "Eligibility I2"],
    ["AMI, Stroke, HF, SCD", "Acute care setting of a candidate", "visit_occurrence", "visit_concept_id, visit_start_date, visit_end_date",
     "9201 = inpatient, 9203 = emergency room, 262 = ER and inpatient", "Visit containing the candidate code", "Adjudication evidence (acute_visit vote)"],
    [ALL5, "Observation time", "observation_period", "observation_period_start_date, observation_period_end_date", "—",
     "≥ 365 days before index; > 0 days after", "Eligibility I3/E5; follow-up and censoring"],
    [ALL5, "Death", "death", "death_date, cause_concept_id", "—", "Any date", "Exclusion E4; censoring; SCD evidence (death within 1 day)"],
    ["AMI, Stroke, HF, SCD", "Primary diagnosis", "condition_occurrence", "condition_status_concept_id (or condition_type_concept_id)",
     ", ".join(map(str, cs.PRIMARY_STATUS)) + " = primary / principal (to confirm with MCP)", "Candidate code rows", "Adjudication evidence (primary_dx vote)"],
    [ALL5, "Diagnosis codes", "condition_occurrence; observation", "*_concept_id, *_source_concept_id, *_source_value, *_date, visit_occurrence_id",
     "ICD10CM, ICD9CM source concepts", "See History and Outcome tabs", "History flags; outcome candidates"],
    [ALL5, "Code look-up", "concept; concept_ancestor", "concept_id, concept_code, vocabulary_id, concept_name; ancestor/descendant ids",
     "Vocabularies: ICD10CM, ICD9CM, LOINC, CPT4, ICD10PCS, ATC, RxNorm, UCUM", "—", "Maps every code in this workbook to concept_ids"],
    [ALL5, "Measurement units", "measurement; concept", "unit_concept_id, unit_source_value, value_as_number, range_high", "UCUM", "—",
     "Unit harmonisation (e.g. cholesterol mmol/L → mg/dL)"],
]

# ---- Source tables
tables = [
    ["person", "OMOP CDM", "person_id, gender_concept_id, year/month/day_of_birth, birth_datetime, race_concept_id, ethnicity_concept_id", "All 5", "Demographics, eligibility", "Yes"],
    ["observation_period", "OMOP CDM", "person_id, observation_period_start_date, observation_period_end_date", "All 5", "Eligibility, follow-up", "Yes"],
    ["visit_occurrence", "OMOP CDM", "visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date", "All 5", "Index visit; acute setting", "Yes"],
    ["condition_occurrence", "OMOP CDM", "person_id, condition_concept_id, condition_source_concept_id, condition_source_value, condition_start_date, condition_status_concept_id, condition_type_concept_id, visit_occurrence_id", "All 5", "History flags; outcome candidates", "Yes"],
    ["observation", "OMOP CDM", "person_id, observation_concept_id, observation_source_concept_id, observation_source_value, observation_date", "All 5", "History Z/V codes (family history, smoking, prior stroke)", "Yes"],
    ["measurement", "OMOP CDM", "measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime, value_as_number, unit_concept_id, unit_source_value, range_high", "All 5", "Labs/vitals; troponin, BNP evidence", "Yes"],
    ["procedure_occurrence", "OMOP CDM", "person_id, procedure_concept_id, procedure_source_concept_id, procedure_source_value, procedure_date", "All 5", "Adjudication evidence", "Yes"],
    ["drug_exposure", "OMOP CDM", "person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date", "All 5", "Medication flags", "Yes"],
    ["death", "OMOP CDM", "person_id, death_date, cause_concept_id", "All 5", "Exclusion, censoring, SCD evidence", "Yes"],
    ["note", "OMOP CDM", "note_id, person_id, note_date, note_title, note_text", "All 5", "CAC, LVEF, event-window notes (includes FACT_RADIOLOGY reports)", "Yes"],
    ["concept", "OMOP vocabulary", "concept_id, concept_code, vocabulary_id, concept_name, domain_id", "All 5", "Code look-up", "Yes"],
    ["concept_ancestor", "OMOP vocabulary", "ancestor_concept_id, descendant_concept_id", "All 5", "ATC → RxNorm drug mapping", "Yes"],
    ["FACT_ECHO_TEST", "Mayo native", "ECHO_NOTE (confirmed); patient key, echo date/time, echo id (to confirm)", "HF, SCD, AF (LVEF)", "LVEF at baseline and around HF candidates", "Yes"],
    ["FACT_RADIOLOGY", "Mayo native", "RADIOLOGY_REPORT (already in OMOP note.note_text)", "All 5", "Only if OMOP note does not hold every report", "Confirm"],
]

# ---- Python packages
pkgs = [
    ["pyspark", "Workspace Spark runtime (do not pip install)", "Spark SQL extraction from OMOP / FACT tables", "training/extract.py", "Required", "Provided by MCP; tested with Spark 4.0.1"],
    ["pyarrow", ">=17", "Fast Spark → pandas transfer (spark.sql.execution.arrow)", "training/extract.py", "Required", "requirements-training.txt"],
    ["numpy", "2.5.3", "Numerical work", "training/*, curation/*, cardioai_core/bundle.py", "Required", "requirements.txt"],
    ["pandas", "3.0.5", "Data frames for extracts, labels, audit", "training/*, curation/*", "Required", "requirements.txt"],
    ["scikit-learn", "1.9.1", "Model training and calibration", "training/train.py, cardioai_core/bundle.py, curation/pipeline.py", "Required", "requirements.txt (import name sklearn)"],
    ["joblib", "1.6.0", "Saving the model bundle", "training/train.py, cardioai_core/bundle.py", "Required", "requirements.txt"],
    ["sqlalchemy", "2.1.5", "Curation workbench store (local SQLite in the workspace)", "curation/*", "Required", "requirements.txt"],
    ["fastapi", "0.146.0", "Curation workbench web server (127.0.0.1 only)", "curation/server.py", "Required", "requirements.txt"],
    ["uvicorn[standard]", "0.53.0", "Runs the curation workbench", "curation/server.py", "Required", "requirements.txt"],
    ["pydantic", "(installed with fastapi)", "Request validation in the workbench", "curation/server.py", "Required", "dependency of fastapi"],
    ["PyJWT", "2.14.0", "Workbench sign-in tokens", "curation/server.py, cardioai_core/security.py", "Required", "requirements.txt (import name jwt)"],
    ["psycopg[binary]", ">=3.2", "Postgres driver for the Render app", "app/", "Installed, not used in workspace", "requirements.txt"],
    ["cryptography", "50.0.2", "MFA secret encryption in the Render app", "app/mfa.py", "Installed, not used in workspace", "requirements.txt"],
    ["segno", "1.6.6", "MFA QR codes in the Render app", "app/mfa.py", "Installed, not used in workspace", "requirements.txt"],
    ["pytest", ">=8", "Workspace self-test (Runbook 1.2)", "tests/", "Recommended", "requirements-dev.txt"],
    ["httpx2", ">=2.13", "Test client for the workbench tests", "tests/", "Recommended", "requirements-dev.txt"],
    ["simple-icd-10-cm", "1.5.0", "Regenerating the candidate code CSV", "training/codelists.py", "Not needed in workspace", "Development only; the CSV is committed"],
    ["icd9cms", "0.2.1", "Regenerating the candidate code CSV", "training/codelists.py", "Not needed in workspace", "Development only"],
    ["openpyxl", ">=3.1", "Reading the ICD-9-CM description file", "training/codelists.py", "Not needed in workspace", "Development only"],
    ["Python standard library", "Python 3.12", "argparse, csv, json, re, math, datetime, hashlib, hmac, secrets, logging, pathlib, decimal, dataclasses, functools, collections, shutil, getpass, base64, io, copy", "all", "Required", "Python ≥ 3.11; tested on 3.12"],
]

# ---- Open confirmations
src = openpyxl.load_workbook(REPO / "docs/CardioAI_Models_Sheet.xlsx")["MCP open items"]
open_rows = [list(r)[:9] for r in src.iter_rows(min_row=3, values_only=True) if r[0] is not None]

# ---- Eligibility
src = openpyxl.load_workbook(REPO / "docs/CardioAI_Models_Sheet.xlsx")["Eligibility"]
elig = [list(r)[:7] for r in src.iter_rows(min_row=3, values_only=True) if r[0] is not None]

# =============================================================== write
readme = wb.create_sheet("Read me")
readme.column_dimensions["A"].width = 30
readme.column_dimensions["B"].width = 110
readme["A1"] = "CardioAI: data import list for Mayo Clinic Platform"
readme["A1"].font = TITLE_FONT
info = [
    ("Purpose", "Every data element, code and table the five CardioAI risk calculators (AMI, Stroke, Heart Failure, Sudden Cardiac Death, Atrial Fibrillation) import from Mayo Clinic Platform for training, plus the Python packages the workspace needs."),
    ("Prepared by", "CardioAI (cardioailive.com)"),
    ("Date", date.today().isoformat()),
    ("Source of truth", "Generated from the CardioAI code sets (cardioai_core/schema.py, training/codesets.py, training/code_lists/candidate_codes.csv). The extraction SQL uses exactly these lists."),
    ("Data model", "OMOP CDM on Spark SQL, plus Mayo native tables FACT_ECHO_TEST and FACT_RADIOLOGY."),
    ("Population", f"Adults aged {AGE_MIN}–{AGE_MAX} at the index visit. No pediatric patients. Full criteria on the Eligibility tab."),
    ("Index date", f"One outpatient visit per patient, {i0} to {i1}."),
    ("Time windows", f"Baseline labs, vitals and reports: {lb} days up to index. Medications: {drug_lb} days before index. History: any time on or before index. Outcomes: any time after index. Adjudication evidence: around each candidate event (see each tab)."),
    ("Data leaving the workspace", "None at patient level. Only the export-reviewed model bundle and aggregate metrics (cells < 11 suppressed)."),
    ("Codes in this workbook", None),
]
r = 3
for k, v in info:
    readme.cell(row=r, column=1, value=k).font = BOLD
    c = readme.cell(row=r, column=2, value=v)
    c.font, c.alignment = BODY, WRAP
    r += 1
counts = [
    ("Labs & vitals (LOINC)", "'Labs & vitals'!E5:E1000"),
    ("History diagnoses (ICD)", "'History diagnoses'!E5:E5000"),
    ("Outcome diagnoses (ICD)", "'Outcome diagnoses'!C5:C5000"),
    ("Procedure code groups", "Procedures!D5:D1000"),
    ("Medication classes (ATC)", "Medications!D5:D1000"),
]
for label, rng in counts:
    readme.cell(row=r, column=1, value="   " + label).font = BODY
    c = readme.cell(row=r, column=2, value=f"=COUNTA({rng})")
    c.font, c.alignment = BODY, Alignment(horizontal="left")
    r += 1
r += 1
readme.cell(row=r, column=1, value="Tabs").font = BOLD
r += 1
TABS = [
    ("By calculator", "What each calculator imports, and code counts per calculator"),
    ("Source tables", "OMOP and native tables and the columns read"),
    ("Labs & vitals", "LOINC codes: model inputs, eligibility, adjudication evidence"),
    ("History diagnoses", "Every ICD-10-CM / ICD-9-CM code behind each history flag"),
    ("Outcome diagnoses", "Every ICD code that creates a candidate outcome event (included = yes), and the excluded ones"),
    ("Procedures", "CPT4 and ICD-10-PCS codes used as adjudication evidence"),
    ("Medications", "ATC classes (mapped to RxNorm in the workspace)"),
    ("Notes & reports", "Clinical notes, radiology and echo reports, and their windows"),
    ("Concepts & demographics", "Person, visit, death and status concepts"),
    ("Eligibility", "Inclusion and exclusion criteria, with age bounds"),
    ("Python packages", "Packages the code imports, versions, and whether the workspace needs them"),
    ("Open confirmations", "Questions for MCP. Yellow cells are for MCP's answers"),
]
for t, d in TABS:
    c = readme.cell(row=r, column=1, value=t)
    c.hyperlink, c.font = f"#'{t}'!A1", Font(name=FONT, size=10, color="FF1F5FAF", underline="single")
    readme.cell(row=r, column=2, value=d).font = BODY
    r += 1
r += 1
readme.cell(row=r, column=1, value="Notes").font = BOLD
readme.cell(row=r, column=2, value="ICD-10-CM codes: CMS/CDC release of 1 April 2026; ICD-9-CM: CMS v32 (final). 'header' codes are not billable today but appear in older records. CPT descriptions are not reproduced (AMA licence); MCP's concept table has them. LOINC names are short forms; verify assay codes against Mayo's lab catalogue.").font = BODY
readme.cell(row=r, column=2).alignment = WRAP
readme.row_dimensions[r].height = 45

# By calculator
bc_rows = []
groups = [
    ("Demographics", "age, sex, race/ethnicity, height, weight, BMI", [1] * 5),
    ("History flags", "prior MI, prior stroke/TIA, angina, heart failure, AF, family history CAD, hypertension, PAD, smoking, diabetes", [1] * 5),
    ("Vitals and labs", "SBP, DBP, total/HDL/LDL cholesterol, triglycerides, glucose, HbA1c, hs-CRP, eGFR", [1] * 5),
    ("Imaging values", "Coronary calcium (radiology reports), LVEF (echo)", [1] * 5),
    ("Medications", "statin, aspirin, ACE-I/ARB, beta blocker, anticoagulant, any antihypertensive", [1] * 5),
    ("Outcome diagnosis codes", "Candidate events after index (one family per calculator)", [1] * 5),
    ("Troponin I / T, hs-troponin", "AMI adjudication evidence", [1, 0, 0, 0, 0]),
    ("BNP / NT-proBNP", "HF adjudication evidence", [0, 0, 1, 0, 0]),
    ("Coronary angiography, PCI", "AMI adjudication evidence", [1, 0, 0, 0, 0]),
    ("Head CT/MRI, thrombolysis, thrombectomy", "Stroke adjudication evidence", [0, 1, 0, 0, 0]),
    ("Echocardiogram, event-window echo reports", "HF adjudication evidence (HFrEF / HFpEF)", [0, 0, 1, 0, 0]),
    ("CPR, defibrillation, ICD implant, death date", "SCD adjudication evidence", [0, 0, 0, 1, 0]),
    ("Cardioversion, cardiac surgery", "AF adjudication evidence; post-operative AF rule", [0, 0, 0, 0, 1]),
    ("Event-window clinical notes", "Adjudication evidence for every calculator", [1] * 5),
    ("Visit setting, primary diagnosis", "Adjudication evidence", [1, 1, 1, 1, 0]),
]
for g, d, flags in groups:
    bc_rows.append([g, d] + ["Y" if f else "" for f in flags])
ws = sheet("By calculator", "What each calculator imports",
           "Y = the calculator uses this element. All five models share the same baseline inputs; they differ in outcome codes and adjudication evidence. Code counts below are formulas over the code tabs.",
           ["Data element group", "Includes", "AMI", "Stroke", "HF", "SCD", "AF"], bc_rows, [36, 80, 9, 9, 9, 9, 9])
r = 6 + len(bc_rows)
ws.cell(row=r, column=1, value="Codes per calculator").font = BOLD
r += 1
hdr = ["Count", "", "AMI", "Stroke", "HF", "SCD", "AF"]
for j, h in enumerate(hdr, 1):
    c = ws.cell(row=r, column=j, value=h or None)
    c.font, c.fill = HDR_FONT, HDR_FILL
lines = [
    ("Outcome ICD codes (included)", lambda k: f"=COUNTIFS('Outcome diagnoses'!$A$5:$A$5000,\"{k}\",'Outcome diagnoses'!$G$5:$G$5000,\"yes\")"),
    ("Outcome ICD codes (excluded on purpose)", lambda k: f"=COUNTIFS('Outcome diagnoses'!$A$5:$A$5000,\"{k}\",'Outcome diagnoses'!$G$5:$G$5000,\"no\")"),
    ("Evidence LOINC codes", lambda k: f"=COUNTIFS('Labs & vitals'!$A$5:$A$1000,\"{k}\")"),
    ("Evidence procedure code groups", lambda k: f"=COUNTIFS(Procedures!$A$5:$A$1000,\"{k}\")"),
    ("Shared baseline LOINC codes", lambda k: "=COUNTIFS('Labs & vitals'!$A$5:$A$1000,\"All 5\",'Labs & vitals'!$C$5:$C$1000,\"Model input (baseline)\")"),
    ("Shared history ICD codes", lambda k: "=COUNTA('History diagnoses'!$E$5:$E$5000)"),
]
for label, fn in lines:
    r += 1
    ws.cell(row=r, column=1, value=label).font = BODY
    for j, k in enumerate(CALC_NAMES.values(), 3):
        ws.cell(row=r, column=j, value=fn(k)).font = BODY

sheet("Source tables", "Tables and columns read", "Read-only access is needed to these tables in the de-identified OMOP CDM and the native FACT tables. Please confirm the Spark catalog and schema names (Open confirmations #2).",
      ["Table", "Model", "Columns read", "Calculators", "Used for", "Access needed"], tables, [24, 16, 80, 18, 44, 14])
sheet("Labs & vitals", "Labs and vitals (OMOP measurement, LOINC)", "Baseline values are the latest in the look-back window, harmonised to the units shown. Evidence labs are pulled for every result after index and read around each candidate event.",
      ["Calculators", "Data element", "Purpose", "Vocabulary", "Code", "LOINC name", "Unit", "OMOP table", "Time window", "Notes"],
      labs, [12, 24, 26, 11, 11, 52, 20, 22, 48, 36])
sheet("History diagnoses", "History diagnoses (every code)", "A flag is set when any of these codes is recorded on or before index, in condition_occurrence or observation. Expanded from the code-set patterns against the official code sets.",
      ["Calculators", "History flag", "Purpose", "Vocabulary", "Code", "Description", "Status", "OMOP table", "Time window"],
      hist, [10, 22, 22, 11, 11, 70, 12, 26, 34])
sheet("Outcome diagnoses", "Outcome diagnoses (candidate events)", "A code recorded after index creates a CANDIDATE event; auto-labelling and cardiologist adjudication decide whether it is a real event. included = no rows are deliberately not used (reason given).",
      ["Calculator", "Vocabulary", "Code", "Description", "Status", "Subtype hint", "Included", "Reason excluded", "OMOP table", "Time window"],
      outc, [10, 11, 11, 70, 22, 13, 9, 44, 20, 34])
sheet("Procedures", "Procedures (adjudication evidence)", "CPT codes are listed as ranges (not every number in a range is an active CPT code). ICD-10-PCS entries ending in * mean every code starting with those characters.",
      ["Calculator", "Procedure", "Vocabulary", "Codes", "Count", "Pattern in code set", "How it is used", "OMOP table", "Time window"],
      procs, [10, 30, 11, 48, 7, 26, 46, 20, 36])
sheet("Medications", "Medications (OMOP drug_exposure)", "ATC classes are mapped to every descendant RxNorm concept in the workspace (concept_ancestor), so no RxNorm list is needed from MCP.",
      ["Calculators", "Data element", "Vocabulary", "ATC code", "ATC class", "OMOP table / mapping", "Time window", "Notes"],
      meds, [10, 22, 10, 10, 52, 50, 30, 34])
sheet("Notes & reports", "Clinical notes and reports", "Notes are read only inside the workspace (keyword prefilter in SQL, NLP in Python). Note text never leaves the workspace.",
      ["Calculators", "Data element", "Purpose", "Source", "Columns", "Time window", "Text filter", "Note types"],
      notes, [16, 28, 24, 36, 34, 40, 50, 34])
sheet("Concepts & demographics", "Person, visit, death and status concepts", "Concept IDs are standard OMOP concepts; please confirm the primary-diagnosis status values (Open confirmations #3).",
      ["Calculators", "Data element", "OMOP table", "Columns", "Concept IDs / values", "Window", "Used for"],
      conc, [16, 28, 26, 50, 50, 34, 40])
sheet("Eligibility", "Inclusion and exclusion criteria", f"Adults {AGE_MIN}–{AGE_MAX} only; pediatric patients (under {AGE_MIN}) are excluded from training, validation and use.",
      ["ID", "Type", "Criterion", "Applies to", "Lower bound", "Upper bound", "Enforced in"], elig, [6, 20, 70, 20, 18, 20, 34])
sheet("Python packages", "Python packages imported by the code", "Install with: pip install -r requirements-training.txt (pyspark comes from the workspace Spark runtime). 'Installed, not used' packages come in through requirements.txt for the web app.",
      ["Package", "Version", "What it is for", "Imported by", "Workspace need", "Notes"], pkgs, [24, 30, 50, 44, 26, 40])
sheet("Open confirmations", "Questions that need MCP's answer", "Yellow cells are for MCP: fill in the response, who answered and when.",
      ["#", "Topic", "Question for MCP", "Why it matters", "What it blocks / where it is used", "Needed by", "Status", "MCP response", "Answered by / date"],
      open_rows, [5, 22, 50, 40, 38, 18, 10, 44, 18], fill_cols=(8, 9))

for ws in wb.worksheets:
    ws.sheet_properties.tabColor = "1F3A5F" if ws.title in ("Read me", "By calculator", "Open confirmations") else "8AA4C8"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
for row in readme.iter_rows(min_col=1, max_col=1):
    row[0].alignment = Alignment(vertical="top")
bcs = wb["By calculator"]
for row in bcs.iter_rows(min_row=5, min_col=3, max_col=7):
    for c in row:
        c.alignment = Alignment(horizontal="center", vertical="top")
wb.save(OUT)
print("saved", OUT, {"labs": len(labs), "history": len(hist), "outcome": len(outc), "procs": len(procs), "meds": len(meds)})
