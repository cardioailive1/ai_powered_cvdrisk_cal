"""Build mock OMOP CDM tables (+ mock Mayo native tables DIM_PATIENT, FACT_ECHO_TEST, FACT_DIAGNOSIS, FACT_MORTALITY_STATUS) from the synthetic raw dataset,
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
    notes = pd.concat([ev[~is_echo], raw["baseline_imaging_notes"].assign(note_title="Imaging report")], ignore_index=True)
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
    fact_echo = pd.DataFrame({"ECHO_ID": echo["note_id"].astype(str), "PATIENT_DK": dk(echo["person_id"]),
                              "ECHO_DTM": pd.to_datetime(echo["note_date"]) + pd.Timedelta(hours=10), "ECHO_NOTE": echo["note_text"]})
    cc = raw["candidate_codes"]
    fact_dx = pd.DataFrame({"PATIENT_DK": cc["person_id"].astype("int64") * 10,
                            "DIAGNOSIS_DTM": pd.to_datetime(cc["event_date"]) + pd.Timedelta(hours=9),
                            "DIAGNOSIS_CODE": cc["code"].str.replace(".", "", regex=False),
                            "PRIMARY_DIAGNOSIS_FLAG": np.where(cc["condition_status_concept_id"].isin(cs.PRIMARY_STATUS), "Y", "N"),
                            "ENCOUNTER_NUMBER": np.arange(len(cc)).astype(str)})
    death_ts = pd.to_datetime(F["index_date"] + pd.to_timedelta(F["death_days"], unit="D"))
    mort = pd.DataFrame({"PATIENT_DK": pid * 10, "DEATH_DTM": death_ts,
                         "EHR_LAST_LIVE_INTERACTION_DTM": F["index_date"] + pd.to_timedelta(F["followup_days"], unit="D")})
    empty_obs = pd.DataFrame({"person_id": pd.Series(dtype="int64"), "observation_source_concept_id": pd.Series(dtype="int64"),
                              "observation_date": pd.Series(dtype="object")})
    return {"person": person, "observation_period": obs, "death": death, "visit_occurrence": pd.DataFrame(visits),
            "measurement": pd.DataFrame(meas), "condition_occurrence": pd.DataFrame(cond), "observation": empty_obs,
            "drug_exposure": pd.DataFrame(drugs), "concept_ancestor": pd.DataFrame(anc),
            "procedure_occurrence": pd.DataFrame(procs), "note": note, "concept": pd.DataFrame(C.rows),
            "FACT_ECHO_TEST": fact_echo, "DIM_PATIENT": dim, "FACT_DIAGNOSIS": fact_dx, "FACT_MORTALITY_STATUS": mort}
