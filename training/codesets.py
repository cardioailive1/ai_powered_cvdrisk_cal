"""Code sets for cohorts, features, candidate outcome events and adjudication
evidence on the OMOP CDM (Mayo Clinic Platform).

Single source of truth: the SQL in extract.py is rendered from these lists.
REVIEW WITH YOUR CLINICAL LEAD AND CODING TEAM BEFORE TRAINING; the data
scientist review step in the curation workbench records that sign-off.

ICD patterns are regex prefixes on concept_code WITH the dot (e.g. 'I21.4').
CPT4 / ICD10PCS patterns are regex prefixes on concept_code without dots.
"""

# ----------------------------------------------------------------------------
# Candidate outcome events, grouped by outcome FAMILY (unit of adjudication).
# Diagnosis codes only create CANDIDATES; whether a candidate is a real event
# is decided by auto-labelling + cardiologist adjudication, not by the code.
# ----------------------------------------------------------------------------
# The candidate diagnosis codes are defined in training/codelists.py (RULES) and expanded, code by code with
# official descriptions, into training/code_lists/candidate_codes.csv, which the extraction SQL matches exactly.

# ----------------------------------------------------------------------------
# History flags (any time on or before index)
# ----------------------------------------------------------------------------
HISTORY = {
    "prior_mi": {"icd10": [r"I21", r"I22", r"I25\.2"], "icd9": [r"410", r"412"]},
    "prior_stroke": {"icd10": [r"I6[0-3]", r"G45", r"Z86\.73"], "icd9": [r"43[0-6]", r"V12\.54"]},
    "angina": {"icd10": [r"I20"], "icd9": [r"413"]},
    "heart_failure": {"icd10": [r"I50", r"I11\.0", r"I13\.0", r"I13\.2"], "icd9": [r"428"]},
    "afib": {"icd10": [r"I48"], "icd9": [r"427\.3"]},
    "family_hx_cad": {"icd10": [r"Z82\.49"], "icd9": [r"V17\.3"]},
    "hypertension": {"icd10": [r"I1[0-6]"], "icd9": [r"40[1-5]"]},
    "pad": {"icd10": [r"I70\.2", r"I73\.9"], "icd9": [r"440\.2", r"443\.9"]},
    "smoking_current": {"icd10": [r"F17\.2", r"Z72\.0"], "icd9": [r"305\.1"]},
    "smoking_former": {"icd10": [r"Z87\.891"], "icd9": [r"V15\.82"]},
    "diabetes_t1": {"icd10": [r"E10"], "icd9": [r"250\.\d[13]"]},
    "diabetes_t2": {"icd10": [r"E11"], "icd9": [r"250\.\d[02]"]},
    "prediabetes": {"icd10": [r"R73\.0"], "icd9": [r"790\.2"]},
}

# ----------------------------------------------------------------------------
# Medications: ATC class prefixes, drug exposure in the 365 days before index
# ----------------------------------------------------------------------------
DRUGS = {
    "statin": ["C10AA", "C10BA", "C10BX"],
    "aspirin": ["B01AC06"],
    "acei_arb": ["C09"],
    "beta_blocker": ["C07"],
    "anticoagulant": ["B01AA", "B01AE", "B01AF"],
    "antihypertensive": ["C02", "C03", "C07", "C08", "C09"],
}

# ----------------------------------------------------------------------------
# Baseline labs & vitals (features): LOINC -> feature, latest value in look-back
# ----------------------------------------------------------------------------
LOINC = {
    "height_cm": ["8302-2", "3137-7"],
    "weight_kg": ["29463-7", "3141-9"],
    "bmi": ["39156-5"],
    "sbp": ["8480-6", "8459-0"],
    "dbp": ["8462-4", "8453-3"],
    "total_chol": ["2093-3"],
    "hdl": ["2085-8"],
    "ldl": ["13457-7", "18262-6", "2089-1"],
    "trig": ["2571-8"],
    "glucose": ["1558-6", "2345-7"],
    "hba1c": ["4548-4", "17856-6"],
    "hscrp": ["30522-7", "1988-5"],
    "egfr": ["33914-3", "62238-1", "98979-8", "48642-3", "48643-1"],
}

# ----------------------------------------------------------------------------
# Adjudication evidence — biomarkers after index (verify assay codes locally)
# ----------------------------------------------------------------------------
EVIDENCE_LOINC = {
    "troponin_i": ["10839-9", "42757-5", "49563-0"],
    "troponin_t": ["6598-7"],
    "hs_troponin_i": ["89579-7"],
    "hs_troponin_t": ["67151-1"],
    "bnp": ["30934-4", "42637-9"],
    "nt_probnp": ["33762-6", "83107-3"],
}
# Upper reference limits in ng/L, used when measurement.range_high is missing.
# hs_troponin_t: Mayo Clinic Laboratories test TRPS "Troponin T, 5th Generation, Plasma"
#   (Roche cobas Gen 5, LOINC 67151-1): males <=15 ng/L, females <=10 ng/L (Mayo-derived, sex-specific).
#   https://www.mayocliniclabs.com/test-catalog/overview/65832
# Others: generic manufacturer 99th percentiles (conventional cTnT 0.01 ng/mL = 10 ng/L), used mainly for
#   older results before the Gen 5 assay; confirm the switch-over date with Mayo Clinic Platform.
DEFAULT_URL_NG_L = {
    "hs_troponin_t": {"male": 15.0, "female": 10.0},
    "troponin_t": {"male": 10.0, "female": 10.0},
    "troponin_i": {"male": 40.0, "female": 40.0},
    "hs_troponin_i": {"male": 34.0, "female": 16.0},
}
# Mayo TRPS interpretation: a rise of >=10 ng/L over 2 h or >=12 ng/L over 6 h with values above the
# reference interval is highly suggestive of acute cardiac injury (used alongside a >=20% relative change).
HS_TNT_DELTA_NG_L = [(2, 10.0), (6, 12.0)]
# NT-proBNP: Mayo test PBNP1 (Roche proBNP II, LOINC 83107-3) — age/sex reference upper limits (pg/mL),
# shown to adjudicators as H/N flags. Labelling still uses acute-HF decision cut-points (rule-in 450/900/1800
# by age, rule-out <300), which are clinical thresholds, not reference ranges.
#   https://www.mayocliniclabs.com/test-catalog/overview/615897
NT_PROBNP_REF = [  # (min_age, max_age, male_upper, female_upper)
    (19, 39, 79, 160), (40, 44, 72, 162), (45, 54, 87, 141), (55, 64, 88, 226), (65, 200, 540, 540)]

# ----------------------------------------------------------------------------
# Adjudication evidence — procedures (CPT4 / ICD10PCS). VERIFY with coding team.
# ----------------------------------------------------------------------------
PROCEDURES = {
    "pci": {"CPT4": [r"929(2\d|3\d|4[0-4])", r"9298[0-4]"], "ICD10PCS": [r"027[0-3]"]},
    "coronary_angiography": {"CPT4": [r"934(5[4-9]|6[01])", r"9350[18]"], "ICD10PCS": [r"B21[01]"]},
    "head_ct": {"CPT4": [r"70450", r"70460", r"70470", r"70496"], "ICD10PCS": [r"B020"]},
    "head_mri": {"CPT4": [r"7055[1-3]", r"7054[4-6]"], "ICD10PCS": [r"B030"]},
    "echo": {"CPT4": [r"9330[3-8]", r"9331[2-8]", r"9335[01]"], "ICD10PCS": [r"B24"]},
    "thrombolysis": {"CPT4": [r"37195"], "ICD10PCS": [r"3E03317"]},
    "thrombectomy": {"CPT4": [r"61645"], "ICD10PCS": [r"03C[G-V]3"]},
    "cpr": {"CPT4": [r"92950"], "ICD10PCS": [r"5A12012"]},
    "rhythm_restoration": {"CPT4": [], "ICD10PCS": [r"5A2204Z"]},   # defibrillation OR cardioversion
    "cardioversion": {"CPT4": [r"9296[01]"], "ICD10PCS": []},
    "icd_implant": {"CPT4": [r"3324[09]", r"3323[01]", r"33270"], "ICD10PCS": [r"0JH[68]0[89]Z"]},
    "cardiac_surgery": {"CPT4": [r"335(1\d|2\d|3[0-6])", r"334(0[5-9]|1\d|2\d|30)"],
                        "ICD10PCS": [r"021[0-3]0", r"02R[FGHJ]0"]},
}
# Procedures that create an "evidence-only" candidate when no diagnosis code is
# nearby — used to find events the codes missed (adjudicated like any other).
TRIGGER_PROCEDURES = {"pci": "ami", "thrombolysis": "stroke", "thrombectomy": "stroke",
                      "cpr": "scd", "cardioversion": "af"}

# ----------------------------------------------------------------------------
# Mayo Clinic Platform native tables (not OMOP).
# Confirmed by Mayo Clinic Platform (A. Resnick):
#   14 Oct 2026  echo notes are in FACT_ECHO_TEST.ECHO_NOTE; radiology reports are in FACT_RADIOLOGY (also mapped
#                to OMOP note.note_text, which extract.py already uses).
#   21 Oct 2026  DIM_PATIENT has one row per medical-record system; PATIENT_CLINIC_NUMBER is the same across
#                systems (the person-level key); PATIENT_MERGED_FLAG = 'N' gives one row per person. FACT tables
#                carry PATIENT_DK, linked to PATIENT_CLINIC_NUMBER through DIM_PATIENT.
#                FACT_DIAGNOSIS.PRIMARY_DIAGNOSIS_FLAG marks the primary diagnosis (ENCOUNTER_NUMBER links to
#                FACT_ENCOUNTERS.ENCOUNTER_TYPE). FACT_MORTALITY_STATUS combines the Mayo record with third-party
#                death data (Veritas Data Research); EHR_LAST_LIVE_INTERACTION_DTM = last completed appointment,
#                immunization, lab collection or surgical case. Cause of death is NOT populated.
#                Dates are shifted by the same amount across a patient's whole record.
# Values starting with "CONFIRM:" are not yet confirmed: --render-only shows them, a real run with
# --native-dataset refuses until they are set in native_sources.json (run training.preflight to see the columns).
# ----------------------------------------------------------------------------
NATIVE_SOURCES = {
    "patient_link": {
        "table": "DIM_PATIENT",                                       # confirmed 21 Oct 2026
        "dk_column": "PATIENT_DK",                                    # confirmed
        "clinic_number_column": "PATIENT_CLINIC_NUMBER",              # confirmed: same person across systems
        "merged_flag_column": "PATIENT_MERGED_FLAG",                  # confirmed
        "unmerged_value": "N",                                        # confirmed: one row per person
        "omop_person_column": "CONFIRM:OMOP person column holding PATIENT_CLINIC_NUMBER (e.g. person_source_value)",
    },
    "echo": {
        "table": "FACT_ECHO_TEST",
        "text_column": "ECHO_NOTE",                                   # confirmed 14 Oct 2026
        # PATIENT_DK (joined through DIM_PATIENT) or PATIENT_CLINIC_NUMBER (joined directly)
        "patient_column": "CONFIRM:patient key column in FACT_ECHO_TEST (PATIENT_DK or PATIENT_CLINIC_NUMBER)",
        "date_column": "CONFIRM:date/time the echo was performed",
        "id_column": "CONFIRM:unique echo test id (or leave as row number)",
        "title": "Echocardiogram report",
    },
    "diagnosis": {
        "table": "FACT_DIAGNOSIS",                                    # confirmed 21 Oct 2026
        "patient_column": "CONFIRM:patient key column in FACT_DIAGNOSIS (PATIENT_DK or PATIENT_CLINIC_NUMBER)",
        "date_column": "CONFIRM:diagnosis date/time column",
        "code_column": "CONFIRM:ICD diagnosis code column (with or without the dot)",
        "primary_flag_column": "PRIMARY_DIAGNOSIS_FLAG",              # confirmed
        "primary_value": "CONFIRM:value of PRIMARY_DIAGNOSIS_FLAG meaning primary (e.g. Y)",
        "encounter_column": "ENCOUNTER_NUMBER",                       # confirmed (links to FACT_ENCOUNTERS)
    },
    "mortality": {
        "table": "FACT_MORTALITY_STATUS",                             # confirmed 21 Oct 2026
        "patient_column": "CONFIRM:patient key column in FACT_MORTALITY_STATUS (PATIENT_DK or PATIENT_CLINIC_NUMBER)",
        "death_date_column": "CONFIRM:date of death column",
        "last_live_column": "EHR_LAST_LIVE_INTERACTION_DTM",          # confirmed
    },
}
NATIVE_NOTE_SOURCES = NATIVE_SOURCES          # old name, kept for existing imports

# A death counts as an observed outcome-period death when it falls within this many days after the last contact
# (end of OMOP observation or last live interaction). Later deaths (e.g. found only by the third-party death source
# years after the patient left Mayo care) do not end follow-up as a death: the patient is censored at last contact,
# because events between last contact and death are not observed. CLINICAL LEAD TO CONFIRM.
DEATH_GRACE_DAYS = 30

# Regex prefilter for event-window notes (SQL side; full NLP runs in Python)
NOTE_PREFILTER = (r"infarct|stemi|troponin|acute coronary|stroke|cva|hemorrhage|transient ischemic|\btia\b|"
                  r"heart failure|\bchf\b|ejection fraction|lvef|\bbnp\b|diuresis|cardiac arrest|rosc|"
                  r"ventricular (fibrillation|tachycardia)|\bvf\b|\bvt\b|nsvt|atrial fib|atrial flutter|afib")

# OMOP visit concepts
INPATIENT_ED_VISITS = [9201, 9203, 262]   # inpatient, emergency room, ER+inpatient
OUTPATIENT_VISITS = [9202]
# OMOP condition_status concepts meaning "primary/principal diagnosis". Fallback only: with --native-dataset the
# primary flag comes from FACT_DIAGNOSIS.PRIMARY_DIAGNOSIS_FLAG (MCP, 21 Oct 2026); a code is primary if either says so.
PRIMARY_STATUS = [32901, 32902]
# OMOP person concepts
GENDER = {8507: "male", 8532: "female"}
RACE = {8527: "white", 8516: "black", 8515: "asian"}
HISPANIC_ETHNICITY = 38003563
