"""Build mock OMOP CDM tables (+ mock Mayo native tables DIM_PATIENT, FACT_ECHO_TEST, FACT_DIAGNOSIS, FACT_MORTALITY_STATUS, FACT_ECG) from the synthetic raw dataset,
so the Spark SQL extraction can be tested end to end without patient data."""
from __future__ import annotations

import numpy as np
import pandas as pd

from training import codesets as cs

UNIT_CONCEPTS = {"cm": 8582, "kg": 9529, "mmHg": 8876, "mg/dL": 8840, "%": 8554, "mg/L": 8751,
                 "mL/min/{1.73_m2}": 8795, "ng/L": 8725, "pg/mL": 8845}
FEATURE_UNITS = {"height_cm": "cm", "weight_kg": "kg", "sbp": "mmHg", "dbp": "mmHg", "total_chol": "mg/dL", "hdl": "mg/dL",
                 "ldl": "mg/dL", "trig": "mg/dL", "glucose": "mg/dL", "hba1c": "%", "hscrp": "mg/L", "egfr": "mL/min/{1.73_m2}"}
HISTORY_CODES = {"prior_mi": "I25.2", "prior_stroke": "Z86.73", "heart_failure": "I50.9", "afib": "I48.91",
                 "hypertension": "I10", "pad": "I73.9", "family_hx_cad": "Z82.49"}
DRUG_ATC = {"statin": "C10AA05", "aspirin": "B01AC06", "acei_arb": "C09AA02", "beta_blocker": "C07AB02",
            "anticoagulant": "B01AF01", "antihypertensive": "C08CA01"}
RACE = {"white": (8527, 0), "black": (8516, 0), "asian": (8515, 0), "hispanic": (8527, 38003563), "other": (0, 0)}


class Concepts:
    def __init__(self):
        self.rows, self.ids = [], {}

    def id(self, vocab: str, code: str) -> int:
        k = (vocab, code)
        if k not in self.ids:
            self.ids[k] = 2_000_000 + len(self.ids)
            self.rows.append({"concept_id": self.ids[k], "vocabulary_id": vocab, "concept_code": code})
        return self.ids[k]


def build_omop(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    F = raw["features"].copy()
    F["index_date"] = pd.to_datetime(F["index_date"])
    C = Concepts()
    for u, cid in UNIT_CONCEPTS.items():
        C.rows.append({"concept_id": cid, "vocabulary_id": "UCUM", "concept_code": u})
    dob = F["index_date"] - pd.to_timedelta((F["age"] * 365.25).round(), unit="D")
    race = F["race"].map(RACE)
    person = pd.DataFrame({"person_id": F["person_id"], "year_of_birth": dob.dt.year, "month_of_birth": dob.dt.month,
                           "day_of_birth": dob.dt.day, "gender_concept_id": np.where(F["sex"] == "male", 8507, 8532),
                           "race_concept_id": race.str[0], "ethnicity_concept_id": race.str[1],
                           "person_source_value": "MCN" + F["person_id"].astype(str)})
    obs = pd.DataFrame({"person_id": F["person_id"], "observation_period_start_date": (F["index_date"] - pd.Timedelta(days=1200)).dt.date,
                        "observation_period_end_date": (F["index_date"] + pd.to_timedelta(F["followup_days"], unit="D")).dt.date})
    death = F.loc[F["death_days"].notna(), ["person_id"]].assign(
        death_date=(F["index_date"] + pd.to_timedelta(F["death_days"], unit="D")).dt.date)

    visits = [{"visit_occurrence_id": 1_000_000 + int(p), "person_id": p, "visit_concept_id": 9202, "visit_start_date": d.date()}
              for p, d in zip(F["person_id"], F["index_date"])]
    meas, mid = [], 1
    for r in F.to_dict("records"):
        # exactly 365 d before index: only the index visit has a lipid result in its prior year, so the
        # cohort query (one random eligible outpatient visit) deterministically picks the synthetic index date
        d = (r["index_date"] - pd.Timedelta(days=365)).date()
        for feat, unit in FEATURE_UNITS.items():
            v = r.get(feat)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue
            meas.append({"measurement_id": mid, "person_id": r["person_id"], "measurement_concept_id": C.id("LOINC", cs.LOINC[feat][0]),
                         "measurement_date": d, "measurement_datetime": pd.Timestamp(d), "value_as_number": float(v),
                         "unit_concept_id": UNIT_CONCEPTS[unit], "unit_source_value": unit, "range_high": None})
            mid += 1
    lab_code = {a: codes[0] for a, codes in cs.EVIDENCE_LOINC.items()}
    for r in raw["evidence_labs"].to_dict("records"):
        t = pd.Timestamp(r["measured_at"])
        meas.append({"measurement_id": mid, "person_id": r["person_id"], "measurement_concept_id": C.id("LOINC", lab_code[r["analyte"]]),
                     "measurement_date": t.date(), "measurement_datetime": t, "value_as_number": float(r["value"]),
                     "unit_concept_id": UNIT_CONCEPTS.get(r["unit"].replace("pg/mL", "pg/mL"), 0), "unit_source_value": r["unit"],
                     "range_high": None if pd.isna(r["range_high"]) else float(r["range_high"])})
        mid += 1

    cond, cid_ = [], 1
    vid = 2_000_000
    for r in raw["candidate_codes"].to_dict("records"):
        vid += 1
        vocab = "ICD10CM" if r["code"][0].isalpha() else "ICD9CM"
        visits.append({"visit_occurrence_id": vid, "person_id": r["person_id"], "visit_concept_id": int(r["visit_concept_id"]),
                       "visit_start_date": pd.Timestamp(r["event_date"]).date()})
        cond.append({"condition_occurrence_id": cid_, "person_id": r["person_id"], "condition_start_date": pd.Timestamp(r["event_date"]).date(),
                     "condition_source_concept_id": C.id(vocab, r["code"]), "condition_status_concept_id": int(r["condition_status_concept_id"]),
                     "visit_occurrence_id": vid})
        cid_ += 1
    for r in F.to_dict("records"):
        hd = (r["index_date"] - pd.Timedelta(days=200)).date()
        codes = [c for k, c in HISTORY_CODES.items() if r.get(k)]
        codes += {"current": ["F17.210"], "former": ["Z87.891"]}.get(r["smoking"], [])
        codes += {"type2": ["E11.9"], "type1": ["E10.9"], "prediabetes": ["R73.03"]}.get(r["diabetes"], [])
        for code in codes:
            cond.append({"condition_occurrence_id": cid_, "person_id": r["person_id"], "condition_start_date": hd,
                         "condition_source_concept_id": C.id("ICD10CM", code), "condition_status_concept_id": 32908,
                         "visit_occurrence_id": None})
            cid_ += 1

    drugs, anc = [], []
    for k, atc in DRUG_ATC.items():
        anc.append({"ancestor_concept_id": C.id("ATC", atc), "descendant_concept_id": C.id("RxNorm", f"rx-{k}")})
    for r in F.to_dict("records"):
        for k in DRUG_ATC:
            if r.get(k):
                drugs.append({"person_id": r["person_id"], "drug_concept_id": C.id("RxNorm", f"rx-{k}"),
                              "drug_exposure_start_date": (r["index_date"] - pd.Timedelta(days=60)).date()})

    proc_vocab = lambda code: "CPT4" if code[:1].isdigit() else "ICD10PCS"  # noqa: E731
    procs = [{"person_id": r["person_id"], "procedure_date": pd.Timestamp(r["procedure_date"]).date(),
              "procedure_source_concept_id": C.id(proc_vocab(r["code"]), r["code"]), "procedure_concept_id": 0}
             for r in raw["evidence_procedures"].to_dict("records")]

    ev = raw["event_notes"]
    is_echo = ev["note_title"] == "Echocardiogram report"
    is_ecg = ev["note_title"] == "ECG report"          # signed ECGs live in FACT_ECG, not OMOP note
    notes = pd.concat([ev[~is_echo & ~is_ecg], raw["baseline_imaging_notes"].assign(note_title="Imaging report")], ignore_index=True)
    note = pd.DataFrame({"note_id": notes["note_id"].astype("int64"), "person_id": notes["person_id"],
                         "note_date": pd.to_datetime(notes["note_date"]).dt.date, "note_title": notes["note_title"], "note_text": notes["note_text"]})
    echo = ev[is_echo]
    # DIM_PATIENT: one row per medical-record system. Even person_ids also have a second, merged PATIENT_DK (another
    # system); their echo rows are filed under that second key, so the link must go through the clinic number.
    pid = F["person_id"].astype("int64")
    dim = pd.concat([pd.DataFrame({"PATIENT_DK": pid * 10, "PATIENT_CLINIC_NUMBER": "MCN" + pid.astype(str), "PATIENT_MERGED_FLAG": "N"}),
                     pd.DataFrame({"PATIENT_DK": pid[pid % 2 == 0] * 10 + 1, "PATIENT_CLINIC_NUMBER": "MCN" + pid[pid % 2 == 0].astype(str),
                                   "PATIENT_MERGED_FLAG": "Y"})], ignore_index=True)
    dk = lambda p: p.astype("int64") * 10 + (p.astype("int64") % 2 == 0)  # noqa: E731  (second-system key for even ids)
    # column names as on MCP (preflight, 22 Nov 2026)
    fact_echo = pd.DataFrame({"ECHO_TEST_FPK": echo["note_id"].astype("int64"), "PATIENT_DK": dk(echo["person_id"]),
                              "ECHO_ORDERED_DTM": pd.to_datetime(echo["note_date"]) - pd.Timedelta(days=2),
                              "ECHO_PERFORMED_DTM": pd.to_datetime(echo["note_date"]) + pd.Timedelta(hours=10),
                              "ECHO_NOTE": echo["note_text"]})
    # FACT_ECG: one row per interpretation snippet (ECG_INTERPRET_SEQ_NBR), each repeating the signed full text
    ecg = ev[is_ecg].reset_index(drop=True)
    snip = ecg.loc[ecg.index.repeat(3)].reset_index()
    fact_ecg = pd.DataFrame({"ECG_FPK": (snip["index"] + 900000).astype("int64"), "PATIENT_DK": dk(snip["person_id"]),
                             "ECG_DTM": pd.to_datetime(snip["note_date"]) + pd.Timedelta(hours=8),
                             "ECG_INTERPRET_SEQ_NBR": snip.groupby("index").cumcount() + 1,
                             "ECG_INTERPRET_TEXT": snip.groupby("index").cumcount().map({0: "Atrial fibrillation", 1: "Abnormal ECG", 2: ""}),
                             "ECG_INTERPRET_FULL_TEXT": snip["note_text"],
                             "ECG_INTERPRET_EXTENSION_TEXT": np.where(snip["index"] % 4 == 0, "Compared with prior tracing.", "")})
    cc = raw["candidate_codes"]
    # FACT_DIAGNOSIS holds DIAGNOSIS_CODE_DK, not the code; the code is in a lookup table (as on MCP)
    codes = sorted(cc["code"].str.replace(".", "", regex=False).unique())
    dx_lookup = pd.DataFrame({"DIAGNOSIS_CODE_DK": np.arange(7000, 7000 + len(codes)).astype("int64"), "DIAGNOSIS_CODE": codes})
    code_dk = dict(zip(dx_lookup["DIAGNOSIS_CODE"], dx_lookup["DIAGNOSIS_CODE_DK"]))
    fact_dx = pd.DataFrame({"PATIENT_DK": cc["person_id"].astype("int64") * 10,
                            "DIAGNOSIS_DTM": pd.to_datetime(cc["event_date"]) + pd.Timedelta(hours=9),
                            "DIAGNOSIS_CODE_DK": cc["code"].str.replace(".", "", regex=False).map(code_dk).astype("int64"),
                            "PRIMARY_DIAGNOSIS_FLAG": np.where(cc["condition_status_concept_id"].isin(cs.PRIMARY_STATUS), "Y", "N"),
                            "ENCOUNTER_NUMBER": np.arange(len(cc)).astype(str)})
    death_ts = pd.to_datetime(F["index_date"] + pd.to_timedelta(F["death_days"], unit="D"))
    # FACT_MORTALITY_STATUS is keyed by clinic number; a death counts only when CONSENSUS_STATUS = 'deceased'
    mort = pd.DataFrame({"PATIENT_CLINIC_NUMBER": "MCN" + pid.astype(str), "CONSENSUS_DEATH_DATE": death_ts,
                         "CONSENSUS_STATUS": np.where(death_ts.notna(), "deceased", "living"),
                         "EHR_LAST_LIVE_INTERACTION_DTM": F["index_date"] + pd.to_timedelta(F["followup_days"], unit="D")})
    empty_obs = pd.DataFrame({"person_id": pd.Series(dtype="int64"), "observation_source_concept_id": pd.Series(dtype="int64"),
                              "observation_date": pd.Series(dtype="object")})
    return {"person": person, "observation_period": obs, "death": death, "visit_occurrence": pd.DataFrame(visits),
            "measurement": pd.DataFrame(meas), "condition_occurrence": pd.DataFrame(cond), "observation": empty_obs,
            "drug_exposure": pd.DataFrame(drugs), "concept_ancestor": pd.DataFrame(anc),
            "procedure_occurrence": pd.DataFrame(procs), "note": note, "concept": pd.DataFrame(C.rows),
            "FACT_ECHO_TEST": fact_echo, "DIM_PATIENT": dim, "FACT_DIAGNOSIS": fact_dx, "FACT_MORTALITY_STATUS": mort,
            "FACT_ECG": fact_ecg, "DIM_DIAGNOSIS_CODE": dx_lookup}


# Native-table settings matching the mock tables, and four+ patients the eligibility criteria must remove
# (the defaults in training/codesets.py are MCP's confirmed columns; only the diagnosis-code lookup is still to confirm)
NATIVE = {"diagnosis": {"code_table": "DIM_DIAGNOSIS_CODE", "code_column": "DIAGNOSIS_CODE"}}
INELIGIBLE = {}   # person_id -> reason, filled by the fixture
LATE_DEATH = {}   # person_id -> days: third-party death long after last contact (censored, not a follow-up death)


def make_ineligible(raw, omop):
    """Turn four synthetic patients into people the eligibility criteria must remove."""
    F = raw["features"].set_index("person_id")
    pids = list(F.index[:4])
    person, death = omop["person"], omop["death"]
    idx = pd.to_datetime(F.loc[pids, "index_date"])
    for pid, years in ((pids[0], 15), (pids[1], 92)):          # E1 pediatric, E2 age 90+
        dob = idx[pid] - pd.Timedelta(days=int(years * 365.25 + 30))
        person.loc[person.person_id == pid, ["year_of_birth", "month_of_birth", "day_of_birth"]] = [dob.year, dob.month, dob.day]
    person.loc[person.person_id == pids[2], "gender_concept_id"] = 0                     # E3 sex not recorded
    death = death[death.person_id != pids[3]]
    omop["death"] = pd.concat([death, pd.DataFrame({"person_id": [pids[3]], "death_date": [(idx[pids[3]] - pd.Timedelta(days=10)).date()]})])
    # E4 from the third-party death source only (FACT_MORTALITY_STATUS), and a death 3 years after last contact
    alive = [p for p in F.index[4:] if pd.isna(F.loc[p, "death_days"])][:2]
    mort = omop["FACT_MORTALITY_STATUS"]
    p4, p5 = alive
    mort.loc[mort.PATIENT_CLINIC_NUMBER == f"MCN{p4}", ["CONSENSUS_DEATH_DATE", "CONSENSUS_STATUS"]] = [
        pd.Timestamp(F.loc[p4, "index_date"]) - pd.Timedelta(days=30), "deceased"]
    late = float(F.loc[p5, "followup_days"]) + 3 * 365
    mort.loc[mort.PATIENT_CLINIC_NUMBER == f"MCN{p5}", ["CONSENSUS_DEATH_DATE", "CONSENSUS_STATUS"]] = [
        pd.Timestamp(F.loc[p5, "index_date"]) + pd.Timedelta(days=late), "deceased"]
    INELIGIBLE.update({pids[0]: "pediatric", pids[1]: "age_90_plus", pids[2]: "sex", pids[3]: "died_before_index",
                       p4: "died_before_index_external"})
    LATE_DEATH[p5] = late
    return omop
