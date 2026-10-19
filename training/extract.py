"""Extract the raw CVD curation dataset from the OMOP CDM on Mayo Clinic Platform.

Run INSIDE your Mayo Clinic Platform_Discover workspace. Patient-level data never
leaves the workspace. Output is the RAW input to the curation pipeline
(auto-labelling -> audit -> data-scientist review -> cardiologist adjudication
-> release); it is NOT a training set until a release is approved.

    python -m training.extract --render-only                      # write SQL for review
    python -m training.extract --cdm my-project.omop_cdm --scratch my-project.my_scratch \
        --index-start 2012-01-01 --index-end 2019-12-31 --out data/raw

Writes to --out:
    features.parquet                one row per patient: index date, baseline features, follow-up, death
    candidate_codes.parquet         every post-index outcome diagnosis code (row level)
    evidence_procedures.parquet     post-index procedures used as adjudication evidence
    evidence_labs.parquet           post-index troponin / BNP / NT-proBNP results
    event_notes.parquet             notes within the window of candidate events (for NLP + adjudicators)
    baseline_imaging_notes.parquet  pre-index imaging/echo reports (CAC, LVEF annotation)

Cohort design (landmark): index = one hash-deterministic outpatient visit per patient in
[index-start, index-end] with a lipid result in the prior year and >=365 d prior
observation; ages 18-90. SQL dialect: BigQuery standard SQL.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from . import codesets as cs
from .imaging_nlp import parse_imaging_reports

log = logging.getLogger("cardioai.extract")
SQL_DIR = Path(__file__).parent / "sql"


# ----------------------------------------------------------------- SQL ----
def _lit(v) -> str:
    if v is True:
        return "TRUE"
    if v is False:
        return "FALSE"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _structs(rows: list[tuple], names: tuple[str, ...]) -> str:
    first = "STRUCT(" + ", ".join(f"{_lit(v)} AS {n}" for v, n in zip(rows[0], names)) + ")"
    rest = ["STRUCT(" + ", ".join(_lit(v) for v in r) + ")" for r in rows[1:]]
    return "UNNEST([\n    " + ",\n    ".join([first] + rest) + "])"


def _icd_rows(spec: dict) -> list[tuple]:
    rows = []
    for name, s in spec.items():
        for vocab, key in (("ICD10CM", "icd10"), ("ICD9CM", "icd9")):
            if s.get(key):
                rows.append((name, vocab, "|".join(s[key])))
    return rows


def _proc_rows() -> list[tuple]:
    return [(cat, vocab, "|".join(pats)) for cat, spec in cs.PROCEDURES.items() for vocab, pats in spec.items() if pats]


def render_sql(cdm: str, scratch: str, index_start: str, index_end: str, lookback_days: int = 730) -> dict[str, str]:
    C = f"`{cdm}"
    S = f"`{scratch}"
    cohort = f"{S}.cvd_cohort`"
    lipid_codes = "'2093-3', '2085-8'"
    outpatient = ", ".join(map(str, cs.OUTPATIENT_VISITS))

    sql: dict[str, str] = {}
    sql["01_cohort"] = f"""-- Landmark cohort: one index outpatient visit per patient
CREATE OR REPLACE TABLE {cohort} AS
WITH candidate_visits AS (
  SELECT DISTINCT v.person_id, v.visit_start_date AS index_date
  FROM {C}.visit_occurrence` v
  JOIN {C}.observation_period` op
    ON op.person_id = v.person_id
   AND v.visit_start_date BETWEEN DATE_ADD(op.observation_period_start_date, INTERVAL 365 DAY)
                              AND op.observation_period_end_date
  JOIN {C}.measurement` m
    ON m.person_id = v.person_id
   AND m.measurement_date BETWEEN DATE_SUB(v.visit_start_date, INTERVAL 365 DAY) AND v.visit_start_date
  JOIN {C}.concept` c
    ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC' AND c.concept_code IN ({lipid_codes})
  WHERE v.visit_concept_id IN ({outpatient})
    AND v.visit_start_date BETWEEN DATE '{index_start}' AND DATE '{index_end}'
),
ranked AS (
  SELECT person_id, index_date,
         ROW_NUMBER() OVER (PARTITION BY person_id
                            ORDER BY FARM_FINGERPRINT(CONCAT(CAST(person_id AS STRING), CAST(index_date AS STRING)))) AS rn
  FROM candidate_visits
)
SELECT r.person_id,
       r.index_date,
       DATE_DIFF(r.index_date, DATE(p.year_of_birth, COALESCE(p.month_of_birth, 7), COALESCE(p.day_of_birth, 1)), DAY) / 365.25 AS age,
       p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id,
       (SELECT MAX(op.observation_period_end_date) FROM {C}.observation_period` op
         WHERE op.person_id = r.person_id AND r.index_date BETWEEN op.observation_period_start_date AND op.observation_period_end_date) AS obs_end,
       (SELECT MIN(d.death_date) FROM {C}.death` d WHERE d.person_id = r.person_id) AS death_date
FROM ranked r
JOIN {C}.person` p ON p.person_id = r.person_id
WHERE r.rn = 1
  AND DATE_DIFF(r.index_date, DATE(p.year_of_birth, COALESCE(p.month_of_birth, 7), COALESCE(p.day_of_birth, 1)), DAY) / 365.25 BETWEEN 18 AND 90
"""

    loinc_rows = [(code, feat) for feat, codes in cs.LOINC.items() for code in codes]
    sql["02_measurements"] = f"""-- Latest baseline lab / vital per feature in the look-back window (units harmonised in Python)
WITH loinc_map AS (SELECT * FROM {_structs(loinc_rows, ('code', 'feature'))}),
ranked AS (
  SELECT co.person_id, lm.feature, m.value_as_number AS value,
         COALESCE(u.concept_code, m.unit_source_value) AS unit,
         ROW_NUMBER() OVER (PARTITION BY co.person_id, lm.feature ORDER BY m.measurement_date DESC, m.measurement_id DESC) AS rn
  FROM {cohort} co
  JOIN {C}.measurement` m
    ON m.person_id = co.person_id
   AND m.measurement_date BETWEEN DATE_SUB(co.index_date, INTERVAL {lookback_days} DAY) AND co.index_date
  JOIN {C}.concept` c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
  JOIN loinc_map lm ON lm.code = c.concept_code
  LEFT JOIN {C}.concept` u ON u.concept_id = m.unit_concept_id
  WHERE m.value_as_number IS NOT NULL
)
SELECT person_id, feature, value, unit FROM ranked WHERE rn = 1
"""

    sql["03_history"] = f"""-- Diagnosis history on or before index (conditions + observation-domain Z codes)
WITH codeset AS (SELECT * FROM {_structs(_icd_rows(cs.HISTORY), ('name', 'vocab', 'pattern'))}),
events AS (
  SELECT co.person_id, x.condition_source_concept_id AS src, x.condition_start_date AS dt
  FROM {cohort} co
  JOIN {C}.condition_occurrence` x ON x.person_id = co.person_id AND x.condition_start_date <= co.index_date
  UNION ALL
  SELECT co.person_id, o.observation_source_concept_id, o.observation_date
  FROM {cohort} co
  JOIN {C}.observation` o ON o.person_id = co.person_id AND o.observation_date <= co.index_date
)
SELECT e.person_id, s.name AS flag, MAX(e.dt) AS last_date
FROM events e
JOIN {C}.concept` c ON c.concept_id = e.src
JOIN codeset s ON s.vocab = c.vocabulary_id AND REGEXP_CONTAINS(c.concept_code, CONCAT('^(', s.pattern, ')'))
GROUP BY 1, 2
"""

    drug_rows = [(name, p) for name, prefixes in cs.DRUGS.items() for p in prefixes]
    sql["04_drugs"] = f"""-- Drug exposure (ATC classes -> descendant RxNorm concepts) in the 365 days before index
WITH atc_prefix AS (SELECT * FROM {_structs(drug_rows, ('name', 'prefix'))}),
atc_drugs AS (
  SELECT DISTINCT p.name, ca.descendant_concept_id AS concept_id
  FROM atc_prefix p
  JOIN {C}.concept` a ON a.vocabulary_id = 'ATC' AND STARTS_WITH(a.concept_code, p.prefix)
  JOIN {C}.concept_ancestor` ca ON ca.ancestor_concept_id = a.concept_id
)
SELECT DISTINCT co.person_id, d.name AS flag
FROM {cohort} co
JOIN {C}.drug_exposure` de
  ON de.person_id = co.person_id
 AND de.drug_exposure_start_date BETWEEN DATE_SUB(co.index_date, INTERVAL 365 DAY) AND co.index_date
JOIN atc_drugs d ON d.concept_id = de.drug_concept_id
"""

    sql["05_candidate_codes"] = f"""-- Every post-index outcome diagnosis code (row level). Codes only create CANDIDATES.
CREATE OR REPLACE TABLE {S}.cvd_candidate_codes` AS
WITH codeset AS (SELECT * FROM {_structs(_icd_rows(cs.CANDIDATE_CODES), ('family', 'vocab', 'pattern'))})
SELECT DISTINCT co.person_id, s.family, x.condition_start_date AS event_date, c.concept_code AS code,
       v.visit_concept_id, x.condition_status_concept_id
FROM {cohort} co
JOIN {C}.condition_occurrence` x ON x.person_id = co.person_id AND x.condition_start_date > co.index_date
JOIN {C}.concept` c ON c.concept_id = x.condition_source_concept_id
JOIN codeset s ON s.vocab = c.vocabulary_id AND REGEXP_CONTAINS(c.concept_code, CONCAT('^(', s.pattern, ')'))
LEFT JOIN {C}.visit_occurrence` v ON v.visit_occurrence_id = x.visit_occurrence_id
"""

    sql["06_baseline_imaging_notes"] = f"""-- Pre-index imaging / echo reports for CAC + LVEF annotation (radiology reports live in OMOP `note`;
-- Mayo Clinic Platform Radiology guide: FACT_RADIOLOGY.RADIOLOGY_REPORT == note.note_text). Text stays in the workspace.
SELECT co.person_id, n.note_id, n.note_date, n.note_text
FROM {cohort} co
JOIN {C}.note` n
  ON n.person_id = co.person_id
 AND n.note_date BETWEEN DATE_SUB(co.index_date, INTERVAL {lookback_days} DAY) AND co.index_date
WHERE REGEXP_CONTAINS(LOWER(n.note_text), r'agatston|calcium score|ejection fraction|lvef')
"""

    sql["07_evidence_procedures"] = f"""-- Post-index procedures used as adjudication evidence
CREATE OR REPLACE TABLE {S}.cvd_evidence_procedures` AS
WITH codeset AS (SELECT * FROM {_structs(_proc_rows(), ('category', 'vocab', 'pattern'))})
SELECT DISTINCT co.person_id, s.category, p.procedure_date, c.concept_code AS code
FROM {cohort} co
JOIN {C}.procedure_occurrence` p ON p.person_id = co.person_id AND p.procedure_date >= co.index_date
JOIN {C}.concept` c ON c.concept_id = IF(p.procedure_source_concept_id > 0, p.procedure_source_concept_id, p.procedure_concept_id)
JOIN codeset s ON s.vocab = c.vocabulary_id AND REGEXP_CONTAINS(c.concept_code, CONCAT('^(', s.pattern, ')'))
"""

    ev_rows = [(code, analyte) for analyte, codes in cs.EVIDENCE_LOINC.items() for code in codes]
    sql["08_evidence_labs"] = f"""-- Post-index troponin / natriuretic peptide results (with reference range when available)
WITH loinc_map AS (SELECT * FROM {_structs(ev_rows, ('code', 'analyte'))})
SELECT co.person_id, lm.analyte,
       COALESCE(m.measurement_datetime, CAST(m.measurement_date AS DATETIME)) AS measured_at,
       m.value_as_number AS value, COALESCE(u.concept_code, m.unit_source_value) AS unit, m.range_high
FROM {cohort} co
JOIN {C}.measurement` m ON m.person_id = co.person_id AND m.measurement_date > co.index_date
JOIN {C}.concept` c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
JOIN loinc_map lm ON lm.code = c.concept_code
LEFT JOIN {C}.concept` u ON u.concept_id = m.unit_concept_id
WHERE m.value_as_number IS NOT NULL
"""

    trig = ", ".join(_lit(k) for k in cs.TRIGGER_PROCEDURES)
    sql["09_event_notes"] = f"""-- Notes around candidate events (codes or trigger procedures): -3 to +30 days.
WITH triggers AS (
  SELECT DISTINCT person_id, event_date AS d FROM {S}.cvd_candidate_codes`
  UNION DISTINCT
  SELECT DISTINCT person_id, procedure_date FROM {S}.cvd_evidence_procedures` WHERE category IN ({trig})
)
SELECT DISTINCT n.person_id, n.note_id, n.note_date, n.note_title, n.note_text
FROM triggers t
JOIN {C}.note` n
  ON n.person_id = t.person_id
 AND n.note_date BETWEEN DATE_SUB(t.d, INTERVAL 3 DAY) AND DATE_ADD(t.d, INTERVAL 30 DAY)
WHERE REGEXP_CONTAINS(LOWER(n.note_text), r'{cs.NOTE_PREFILTER}')
"""
    return sql


# --------------------------------------------------------- assembly -------
_TO_MGDL = {"total_chol": 38.67, "hdl": 38.67, "ldl": 38.67, "trig": 88.57, "glucose": 18.0}


def _harmonize(feature: str, value: float, unit: str | None) -> float:
    u = (unit or "").lower().replace(" ", "")
    if feature == "height_cm" and u in {"[in_i]", "in", "inch", "inches", "[in_us]"}:
        return value * 2.54
    if feature == "height_cm" and u in {"m"}:
        return value * 100
    if feature == "weight_kg" and u in {"[lb_av]", "lb", "lbs", "pound", "pounds"}:
        return value * 0.453592
    if feature == "weight_kg" and u in {"g"}:
        return value / 1000
    if feature in _TO_MGDL and u in {"mmol/l", "mmol/liter"}:
        return value * _TO_MGDL[feature]
    if feature == "hscrp" and u in {"mg/dl"}:
        return value * 10
    if feature == "hba1c" and u in {"mmol/mol"}:
        return value * 0.0915 + 2.15
    return value


def assemble_features(cohort: pd.DataFrame, meas: pd.DataFrame, hist: pd.DataFrame, drugs: pd.DataFrame,
                      imaging_notes: pd.DataFrame | None) -> pd.DataFrame:
    """Long tables from the SQL -> one row per patient (baseline features + follow-up)."""
    df = cohort.copy()
    df["index_date"] = pd.to_datetime(df["index_date"])
    df["sex"] = df["gender_concept_id"].map(cs.GENDER)
    df["race"] = np.where(df["ethnicity_concept_id"] == cs.HISPANIC_ETHNICITY, "hispanic",
                          df["race_concept_id"].map(cs.RACE).fillna("other"))
    df = df[df["sex"].notna()].set_index("person_id")

    if len(meas):
        meas = meas.copy()
        meas["value"] = [_harmonize(f, v, u) for f, v, u in zip(meas["feature"], meas["value"], meas["unit"])]
        wide = meas.pivot_table(index="person_id", columns="feature", values="value", aggfunc="first")
        df = df.join(wide, how="left")
    for f in cs.LOINC:
        if f not in df:
            df[f] = np.nan

    flags = hist.pivot_table(index="person_id", columns="flag", values="last_date", aggfunc="max") if len(hist) else pd.DataFrame()
    for f in cs.HISTORY:
        df[f + "_last"] = pd.to_datetime(flags[f]).reindex(df.index) if f in flags else pd.NaT
    for f in ["prior_mi", "prior_stroke", "angina", "heart_failure", "afib", "family_hx_cad", "hypertension", "pad"]:
        df[f] = df[f + "_last"].notna().astype(int)
    recent = (df["index_date"] - df["smoking_current_last"]).dt.days <= 730
    df["smoking"] = np.select([recent.fillna(False),
                               df["smoking_current_last"].notna() | df["smoking_former_last"].notna()],
                              ["current", "former"], "never")
    df["diabetes"] = np.select([df["diabetes_t2_last"].notna(), df["diabetes_t1_last"].notna(),
                                df["prediabetes_last"].notna() | (df["hba1c"] >= 5.7)],
                               ["type2", "type1", "prediabetes"], "none")
    df.loc[(df["diabetes"].isin(["none", "prediabetes"])) & (df["hba1c"] >= 6.5), "diabetes"] = "type2"
    df = df.drop(columns=[c for c in df.columns if c.endswith("_last")])

    for f in cs.DRUGS:
        ids = set(drugs.loc[drugs["flag"] == f, "person_id"]) if len(drugs) else set()
        df[f] = df.index.isin(ids).astype(int)

    df["cac_score"] = np.nan
    df["lvef"] = np.nan
    if imaging_notes is not None and len(imaging_notes):
        img = parse_imaging_reports(imaging_notes)
        df.update(img[["cac_score", "lvef"]])

    obs_end = pd.to_datetime(df["obs_end"])
    death = pd.to_datetime(df["death_date"])
    df["death_days"] = (death - df["index_date"]).dt.days
    end = pd.concat([obs_end, death], axis=1).min(axis=1)
    df["followup_days"] = (end - df["index_date"]).dt.days
    df = df.drop(columns=["gender_concept_id", "race_concept_id", "ethnicity_concept_id", "obs_end", "death_date"])
    return df.reset_index()


# -------------------------------------------------------------- main ------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cdm", default="PROJECT.OMOP_DATASET")
    ap.add_argument("--scratch", default="PROJECT.SCRATCH_DATASET")
    ap.add_argument("--index-start", default="2012-01-01")
    ap.add_argument("--index-end", default="2019-12-31")
    ap.add_argument("--lookback-days", type=int, default=730)
    ap.add_argument("--render-only", action="store_true", help="write SQL to training/sql/ and exit")
    ap.add_argument("--out", default="data/raw")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    sql = render_sql(a.cdm, a.scratch, a.index_start, a.index_end, a.lookback_days)
    if a.render_only:
        SQL_DIR.mkdir(exist_ok=True)
        for old in SQL_DIR.glob("*.sql"):
            old.unlink()
        for name, q in sql.items():
            (SQL_DIR / f"{name}.sql").write_text(q)
        log.info("Wrote %d SQL files to %s", len(sql), SQL_DIR)
        return

    from google.cloud import bigquery  # available in the Discover workspace
    client = bigquery.Client()

    def run(name: str) -> None:
        log.info("Running %s ...", name)
        client.query(sql[name]).result()

    def q(name: str) -> pd.DataFrame:
        log.info("Running %s ...", name)
        return client.query(sql[name]).to_dataframe()

    def table(name: str) -> pd.DataFrame:
        return client.query(f"SELECT * FROM `{a.scratch}.{name}`").to_dataframe()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    run("01_cohort")
    cohort = table("cvd_cohort")
    log.info("Cohort: %d patients", len(cohort))
    imaging = q("06_baseline_imaging_notes")
    feats = assemble_features(cohort, q("02_measurements"), q("03_history"), q("04_drugs"), imaging)
    feats.to_parquet(out / "features.parquet", index=False)
    imaging.to_parquet(out / "baseline_imaging_notes.parquet", index=False)

    run("05_candidate_codes")
    table("cvd_candidate_codes").to_parquet(out / "candidate_codes.parquet", index=False)
    run("07_evidence_procedures")
    table("cvd_evidence_procedures").to_parquet(out / "evidence_procedures.parquet", index=False)
    q("08_evidence_labs").to_parquet(out / "evidence_labs.parquet", index=False)
    q("09_event_notes").to_parquet(out / "event_notes.parquet", index=False)
    log.info("Raw curation dataset written to %s — next: python -m curation.pipeline label", out)


if __name__ == "__main__":
    main()
