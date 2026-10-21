"""Extract the raw CVD curation dataset from the OMOP CDM on Mayo Clinic Platform.

Run INSIDE your Mayo Clinic Platform_Discover workspace. Patient-level data never
leaves the workspace. Output is the RAW input to the curation pipeline
(auto-labelling -> audit -> data-scientist review -> cardiologist adjudication
-> release); it is NOT a training set until a release is approved.

    python -m training.extract --render-only                      # write Spark SQL for review
    python -m training.extract --cdm <catalog>.<omop_schema> --native-dataset <catalog>.<native_schema> \
        --index-start 2012-01-01 --index-end 2019-12-31 --out data/raw

Writes to --out:
    features.parquet                one row per patient: index date, baseline features, follow-up, death
    candidate_codes.parquet         every post-index outcome diagnosis code (row level)
    evidence_procedures.parquet     post-index procedures used as adjudication evidence
    evidence_labs.parquet           post-index troponin / BNP / NT-proBNP results
    event_notes.parquet             notes within the window of candidate events (for NLP + adjudicators)
    baseline_imaging_notes.parquet  pre-index imaging/echo reports (CAC, LVEF annotation)

Note sources: OMOP `note` (includes radiology reports = FACT_RADIOLOGY.RADIOLOGY_REPORT) plus, with
--native-dataset, echo notes from the Mayo Clinic Platform native table FACT_ECHO_TEST.ECHO_NOTE.
Columns not yet confirmed by Mayo Clinic Platform are "CONFIRM:" placeholders (training/codesets.py);
set them in training/native_sources.json — a real run refuses while any remain.

Cohort design (landmark): index = one hash-deterministic outpatient visit per patient in
[index-start, index-end] with a lipid result in the prior year and >=365 d prior
observation; ADULTS ONLY (18-89 at index; see cardioai_core.schema.ELIGIBILITY). SQL dialect: Spark SQL (Spark >= 3.3), run through the workspace's
Spark session; intermediate results are cached temporary views (no scratch schema needed).
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from cardioai_core.schema import AGE_MAX, AGE_MIN, INDEX_WINDOW

from . import codesets as cs
from .imaging_nlp import parse_imaging_reports

log = logging.getLogger("cardioai.extract")
SQL_DIR = Path(__file__).parent / "sql"


# ----------------------------------------------------------------- SQL ----
# Spark SQL (Mayo Clinic Platform queries data with SparkSQL). Intermediate tables (cohort, candidate
# codes, evidence procedures) are registered as cached TEMPORARY VIEWS in the Spark session, so no
# write access to a scratch schema is needed. Requires Spark >= 3.3.
VIEWS = {"01_cohort": "cvd_cohort", "05_candidate_codes": "cvd_candidate_codes", "07_evidence_procedures": "cvd_evidence_procedures"}


def _lit(v) -> str:
    if v is True:
        return "TRUE"
    if v is False:
        return "FALSE"
    # Spark string literals process backslash escapes, so regex backslashes must be doubled
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _values(rows: list[tuple], names: tuple[str, ...]) -> str:
    """Inline lookup table: (VALUES (...), (...) AS t(col, ...))."""
    body = ",\n    ".join("(" + ", ".join(_lit(v) for v in r) + ")" for r in rows)
    return f"(SELECT * FROM VALUES\n    {body}\n  AS t({', '.join(names)}))"


def _icd_rows(spec: dict) -> list[tuple]:
    rows = []
    for name, s in spec.items():
        for vocab, key in (("ICD10CM", "icd10"), ("ICD9CM", "icd9")):
            if s.get(key):
                rows.append((name, vocab, "|".join(s[key])))
    return rows


def _outcome_concepts(C: str) -> str:
    """Candidate diagnosis codes, matched EXACTLY against the expanded list (training/code_lists/candidate_codes.csv)."""
    from .codelists import load
    rows = [(r["family"], r["vocabulary"], r["code"]) for r in load()]
    return f"""SELECT /*+ BROADCAST(s) */ c.concept_id, s.family
  FROM {C}.concept c
  JOIN {_values(rows, ('family', 'vocab', 'code'))} s
    ON s.vocab = c.vocabulary_id AND c.concept_code = s.code"""


def _proc_rows() -> list[tuple]:
    return [(cat, vocab, "|".join(pats)) for cat, spec in cs.PROCEDURES.items() for vocab, pats in spec.items() if pats]


def _codeset_concepts(C: str, rows: list[tuple], name_col: str) -> str:
    """Resolve a small regex code set to concept_ids once (tiny result, broadcast in later joins)."""
    return f"""SELECT /*+ BROADCAST(s) */ c.concept_id, s.{name_col}
  FROM {C}.concept c
  JOIN {_values(rows, (name_col, 'vocab', 'pattern'))} s
    ON s.vocab = c.vocabulary_id AND c.concept_code RLIKE concat('^(', s.pattern, ')')"""


def render_sql(cdm: str, index_start: str, index_end: str, lookback_days: int = 730) -> dict[str, str]:
    C = cdm
    lipid_codes = "'2093-3', '2085-8'"
    outpatient = ", ".join(map(str, cs.OUTPATIENT_VISITS))
    # cast: OMOP birth fields may be BIGINT; Spark make_date/date_add require INT
    dob = ("date_add(make_date(cast(p.year_of_birth AS INT), cast(coalesce(p.month_of_birth, 7) AS INT), 1), "
           "cast(coalesce(p.day_of_birth, 1) - 1 AS INT))")
    sql: dict[str, str] = {}
    adult = f"age >= {AGE_MIN} AND age < {AGE_MAX + 1}"
    sexes = ", ".join(map(str, cs.GENDER))
    base = f"""WITH visits AS (
  SELECT DISTINCT v.person_id, v.visit_start_date AS index_date,
         cast(datediff(v.visit_start_date, {dob}) AS DOUBLE) / 365.25 AS age
  FROM {C}.visit_occurrence v
  JOIN {C}.person p ON p.person_id = v.person_id
  JOIN {C}.observation_period op
    ON op.person_id = v.person_id
   AND v.visit_start_date BETWEEN date_add(op.observation_period_start_date, 365) AND op.observation_period_end_date
  JOIN {C}.measurement m
    ON m.person_id = v.person_id
   AND m.measurement_date BETWEEN date_sub(v.visit_start_date, 365) AND v.visit_start_date
  JOIN {C}.concept c
    ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC' AND c.concept_code IN ({lipid_codes})
  WHERE v.visit_concept_id IN ({outpatient})
    AND v.visit_start_date BETWEEN DATE '{index_start}' AND DATE '{index_end}'
),
candidate_visits AS (SELECT * FROM visits WHERE {adult}),   -- I1 / E1 / E2: adults {AGE_MIN}-{AGE_MAX} at the visit
ranked AS (
  SELECT person_id, index_date, age,
         ROW_NUMBER() OVER (PARTITION BY person_id
                            ORDER BY xxhash64(concat(cast(person_id AS STRING), cast(index_date AS STRING)))) AS rn
  FROM candidate_visits
),
idx AS (
  SELECT r.person_id, r.index_date, r.age, p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id
  FROM ranked r
  JOIN {C}.person p ON p.person_id = r.person_id
  WHERE r.rn = 1
),
obs AS (
  SELECT i.person_id, MAX(op.observation_period_end_date) AS obs_end
  FROM idx i
  JOIN {C}.observation_period op
    ON op.person_id = i.person_id AND i.index_date BETWEEN op.observation_period_start_date AND op.observation_period_end_date
  GROUP BY i.person_id
),
dth AS (SELECT person_id, MIN(death_date) AS death_date FROM {C}.death GROUP BY person_id),
screened AS (
  SELECT i.person_id, i.index_date, i.age, i.gender_concept_id, i.race_concept_id, i.ethnicity_concept_id,
         o.obs_end, d.death_date,
         i.gender_concept_id IN ({sexes}) AS sex_ok,                                  -- I5 / E3
         (d.death_date IS NULL OR d.death_date > i.index_date) AS alive_at_index,      -- E4
         (o.obs_end > i.index_date) AS has_followup                                    -- E5
  FROM idx i
  LEFT JOIN obs o ON o.person_id = i.person_id
  LEFT JOIN dth d ON d.person_id = i.person_id
)"""
    sql["01_cohort"] = f"""-- Landmark cohort: one index outpatient visit per ADULT patient  [-> temp view cvd_cohort]
-- Eligibility (cardioai_core.schema.ELIGIBILITY): I1-I5 inclusion, E1-E5 exclusion; adults {AGE_MIN}-{AGE_MAX} only, no pediatric patients
{base}
SELECT person_id, index_date, age, gender_concept_id, race_concept_id, ethnicity_concept_id, obs_end, death_date
FROM screened
WHERE sex_ok AND alive_at_index AND has_followup
"""
    sql["00_cohort_attrition"] = f"""-- Patient counts removed by each eligibility criterion (aggregate only; small cells suppressed in Python)
{base},
people AS (
  SELECT person_id, MAX(CASE WHEN {adult} THEN 1 ELSE 0 END) AS any_adult,
         MAX(CASE WHEN age < {AGE_MIN} THEN 1 ELSE 0 END) AS any_pediatric
  FROM visits GROUP BY person_id
)
SELECT
  (SELECT count(*) FROM people) AS with_eligible_visit,
  (SELECT count(*) FROM people WHERE any_adult = 0 AND any_pediatric = 1) AS excluded_pediatric_only,
  (SELECT count(*) FROM people WHERE any_adult = 0 AND any_pediatric = 0) AS excluded_age_90_plus_only,
  (SELECT count(*) FROM screened) AS adults_with_index_visit,
  (SELECT count(*) FROM screened WHERE NOT sex_ok) AS excluded_sex_not_recorded,
  (SELECT count(*) FROM screened WHERE sex_ok AND NOT alive_at_index) AS excluded_died_before_index,
  (SELECT count(*) FROM screened WHERE sex_ok AND alive_at_index AND NOT has_followup) AS excluded_no_followup,
  (SELECT count(*) FROM screened WHERE sex_ok AND alive_at_index AND has_followup) AS final_cohort
"""

    loinc_rows = [(code, feat) for feat, codes in cs.LOINC.items() for code in codes]
    sql["02_measurements"] = f"""-- Latest baseline lab / vital per feature in the look-back window (units harmonised in Python)
WITH loinc_map AS {_values(loinc_rows, ('code', 'feature'))},
ranked AS (
  SELECT co.person_id, lm.feature, m.value_as_number AS value,
         coalesce(u.concept_code, m.unit_source_value) AS unit,
         ROW_NUMBER() OVER (PARTITION BY co.person_id, lm.feature ORDER BY m.measurement_date DESC, m.measurement_id DESC) AS rn
  FROM cvd_cohort co
  JOIN {C}.measurement m
    ON m.person_id = co.person_id
   AND m.measurement_date BETWEEN date_sub(co.index_date, {lookback_days}) AND co.index_date
  JOIN {C}.concept c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
  JOIN loinc_map lm ON lm.code = c.concept_code
  LEFT JOIN {C}.concept u ON u.concept_id = m.unit_concept_id
  WHERE m.value_as_number IS NOT NULL
)
SELECT person_id, feature, value, unit FROM ranked WHERE rn = 1
"""

    sql["03_history"] = f"""-- Diagnosis history on or before index (conditions + observation-domain Z codes)
WITH hist_concepts AS ({_codeset_concepts(C, _icd_rows(cs.HISTORY), 'name')}),
events AS (
  SELECT co.person_id, x.condition_source_concept_id AS src, x.condition_start_date AS dt
  FROM cvd_cohort co
  JOIN {C}.condition_occurrence x ON x.person_id = co.person_id AND x.condition_start_date <= co.index_date
  UNION ALL
  SELECT co.person_id, o.observation_source_concept_id, o.observation_date
  FROM cvd_cohort co
  JOIN {C}.observation o ON o.person_id = co.person_id AND o.observation_date <= co.index_date
)
SELECT e.person_id, h.name AS flag, MAX(e.dt) AS last_date
FROM events e
JOIN hist_concepts h ON h.concept_id = e.src
GROUP BY e.person_id, h.name
"""

    drug_rows = [(name, p) for name, prefixes in cs.DRUGS.items() for p in prefixes]
    sql["04_drugs"] = f"""-- Drug exposure (ATC classes -> descendant RxNorm concepts) in the 365 days before index
WITH atc_prefix AS {_values(drug_rows, ('name', 'prefix'))},
atc_drugs AS (
  SELECT DISTINCT p.name, ca.descendant_concept_id AS concept_id
  FROM atc_prefix p
  JOIN {C}.concept a ON a.vocabulary_id = 'ATC' AND a.concept_code LIKE concat(p.prefix, '%')
  JOIN {C}.concept_ancestor ca ON ca.ancestor_concept_id = a.concept_id
)
SELECT DISTINCT co.person_id, d.name AS flag
FROM cvd_cohort co
JOIN {C}.drug_exposure de
  ON de.person_id = co.person_id
 AND de.drug_exposure_start_date BETWEEN date_sub(co.index_date, 365) AND co.index_date
JOIN atc_drugs d ON d.concept_id = de.drug_concept_id
"""

    sql["05_candidate_codes"] = f"""-- Every post-index outcome diagnosis code (row level). Codes only create CANDIDATES.  [-> temp view cvd_candidate_codes]
WITH outcome_concepts AS ({_outcome_concepts(C)})
SELECT DISTINCT co.person_id, oc.family, x.condition_start_date AS event_date, c.concept_code AS code,
       v.visit_concept_id, x.condition_status_concept_id
FROM cvd_cohort co
JOIN {C}.condition_occurrence x ON x.person_id = co.person_id AND x.condition_start_date > co.index_date
JOIN outcome_concepts oc ON oc.concept_id = x.condition_source_concept_id
JOIN {C}.concept c ON c.concept_id = x.condition_source_concept_id
LEFT JOIN {C}.visit_occurrence v ON v.visit_occurrence_id = x.visit_occurrence_id
"""

    sql["06_baseline_imaging_notes"] = f"""-- Pre-index imaging / echo reports for CAC + LVEF annotation (radiology reports are in OMOP note;
-- Mayo Clinic Platform Radiology guide: FACT_RADIOLOGY.RADIOLOGY_REPORT == note.note_text). Text stays in the workspace.
SELECT co.person_id, n.note_id, n.note_date, n.note_text
FROM cvd_cohort co
JOIN {C}.note n
  ON n.person_id = co.person_id
 AND n.note_date BETWEEN date_sub(co.index_date, {lookback_days}) AND co.index_date
WHERE lower(n.note_text) RLIKE 'agatston|calcium score|ejection fraction|lvef'
"""

    sql["07_evidence_procedures"] = f"""-- Post-index procedures used as adjudication evidence  [-> temp view cvd_evidence_procedures]
WITH proc_concepts AS ({_codeset_concepts(C, _proc_rows(), 'category')})
SELECT DISTINCT co.person_id, pc.category, p.procedure_date, c.concept_code AS code
FROM cvd_cohort co
JOIN {C}.procedure_occurrence p ON p.person_id = co.person_id AND p.procedure_date >= co.index_date
JOIN proc_concepts pc ON pc.concept_id = IF(p.procedure_source_concept_id > 0, p.procedure_source_concept_id, p.procedure_concept_id)
JOIN {C}.concept c ON c.concept_id = pc.concept_id
"""

    ev_rows = [(code, analyte) for analyte, codes in cs.EVIDENCE_LOINC.items() for code in codes]
    sql["08_evidence_labs"] = f"""-- Post-index troponin / natriuretic peptide results (with reference range when available)
WITH loinc_map AS {_values(ev_rows, ('code', 'analyte'))}
SELECT co.person_id, lm.analyte,
       coalesce(m.measurement_datetime, cast(m.measurement_date AS TIMESTAMP)) AS measured_at,
       m.value_as_number AS value, coalesce(u.concept_code, m.unit_source_value) AS unit, m.range_high
FROM cvd_cohort co
JOIN {C}.measurement m ON m.person_id = co.person_id AND m.measurement_date > co.index_date
JOIN {C}.concept c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
JOIN loinc_map lm ON lm.code = c.concept_code
LEFT JOIN {C}.concept u ON u.concept_id = m.unit_concept_id
WHERE m.value_as_number IS NOT NULL
"""

    trig = ", ".join(_lit(k) for k in cs.TRIGGER_PROCEDURES)
    sql["09_event_notes"] = f"""-- Notes around candidate events (codes or trigger procedures): -3 to +30 days.
WITH triggers AS (
  SELECT DISTINCT person_id, event_date AS d FROM cvd_candidate_codes
  UNION
  SELECT DISTINCT person_id, procedure_date AS d FROM cvd_evidence_procedures WHERE category IN ({trig})
)
SELECT DISTINCT n.person_id, n.note_id, n.note_date, n.note_title, n.note_text
FROM triggers t
JOIN {C}.note n
  ON n.person_id = t.person_id
 AND n.note_date BETWEEN date_sub(t.d, 3) AND date_add(t.d, 30)
WHERE lower(n.note_text) RLIKE {_lit(cs.NOTE_PREFILTER)}
"""
    return sql


# ------------------------------------------------- native (non-OMOP) notes ----
NATIVE_CONFIG = Path(__file__).parent / "native_sources.json"


def load_native_config(path: str | Path | None = None) -> dict:
    import json
    cfg = json.loads(json.dumps(cs.NATIVE_NOTE_SOURCES))
    p = Path(path) if path else NATIVE_CONFIG
    if p.exists():
        for src, over in json.loads(p.read_text()).items():
            cfg.setdefault(src, {}).update(over)
    return cfg


def unconfirmed(cfg: dict) -> list[str]:
    return [f"{src}.{k}: {v[len('CONFIRM:'):]}" for src, c in cfg.items() for k, v in c.items()
            if isinstance(v, str) and v.startswith("CONFIRM:") and k != "id_column"]


def _col(v: str) -> str:
    return f"/* {v} */ CONFIRM_COLUMN" if v.startswith("CONFIRM:") else v


def render_native_sql(native: str, cdm: str, cfg: dict, lookback_days: int = 730) -> dict[str, str]:
    e = cfg["echo"]
    txt, pat, dt_, br = _col(e["text_column"]), _col(e["patient_column"]), _col(e["date_column"]), _col(e["person_bridge"])
    nid = (f"cast(x.{e['id_column']} AS STRING)" if not e["id_column"].startswith("CONFIRM:")
           else f"cast(xxhash64(concat(cast(x.{pat} AS STRING), cast(x.{dt_} AS STRING), x.{txt})) AS STRING)")
    base = f"""SELECT {{who}}, concat('ECHO-', {nid}) AS note_id, to_date(x.{dt_}) AS note_date,
       '{e['title']}' AS note_title, x.{txt} AS note_text
FROM {{src}}
JOIN {cdm}.person p ON p.person_id = {{pid}}
JOIN {native}.{e['table']} x ON cast(x.{pat} AS STRING) = cast(p.{br} AS STRING)
WHERE x.{txt} IS NOT NULL"""
    return {
        "10_echo_notes_baseline": "-- Pre-index echo reports (LVEF annotation) from the native FACT_ECHO_TEST table\n"
        + base.format(who="co.person_id", src="cvd_cohort co", pid="co.person_id")
        + f"\n  AND to_date(x.{dt_}) BETWEEN date_sub(co.index_date, {lookback_days}) AND co.index_date\n",
        "11_echo_notes_event": "-- Echo reports around candidate events (-7 to +30 days: LVEF for HF subtype)\n"
        + "WITH triggers AS (SELECT DISTINCT person_id, event_date AS d FROM cvd_candidate_codes)\n"
        + base.format(who="DISTINCT t.person_id", src="triggers t", pid="t.person_id")
        + f"\n  AND to_date(x.{dt_}) BETWEEN date_sub(t.d, 7) AND date_add(t.d, 30)\n",
    }


def merge_notes(*frames: pd.DataFrame) -> pd.DataFrame:
    """Union note sources; drop duplicates of the same text on the same day (OMOP note vs native copy)."""
    df = pd.concat([f for f in frames if f is not None and len(f)], ignore_index=True)
    if df.empty:
        return df
    df["note_id"] = df["note_id"].astype(str)
    df["note_date"] = pd.to_datetime(df["note_date"])
    key = df["note_text"].fillna("").str.strip().str.lower().str[:2000]
    return df.loc[~pd.DataFrame({"p": df["person_id"], "d": df["note_date"], "k": key}).duplicated()].reset_index(drop=True)


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
def get_spark():
    """The workspace's Spark session (notebooks usually provide `spark`; getOrCreate returns it)."""
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.appName("cardioai-extract").getOrCreate()
    spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
    return spark


def _decimals_to_float(pdf: pd.DataFrame) -> pd.DataFrame:
    """OMOP numeric columns are often DECIMAL; Spark hands them to pandas as decimal.Decimal objects."""
    import decimal
    for c in pdf.columns:
        if pdf[c].dtype == object:
            first = pdf[c].dropna().head(1)
            if len(first) and isinstance(first.iloc[0], decimal.Decimal):
                pdf[c] = pdf[c].astype(float)
    return pdf


def write_attrition(df: pd.DataFrame, out: Path, min_cell: int = 11) -> dict:
    """Eligibility flow (how many patients each criterion removed). Aggregate counts only, cells < min_cell suppressed."""
    import json
    row = {k: int(v) for k, v in df.iloc[0].items()} if len(df) else {}
    shown = {k: (v if v == 0 or v >= min_cell else f"<{min_cell}") for k, v in row.items()}
    rep = {"age_min": AGE_MIN, "age_max": AGE_MAX, "counts": shown,
           "note": "Adults only. Patients with no adult visit in the index window are excluded; pediatric patients never enter the cohort."}
    (out / "cohort_attrition.json").write_text(json.dumps(rep, indent=2))
    return shown


def run_extract(spark, a) -> Path:
    sql = render_sql(a.cdm, a.index_start, a.index_end, a.lookback_days)
    ncfg = load_native_config(a.native_config)
    todo = unconfirmed(ncfg)
    if a.native_dataset:
        if todo:
            raise SystemExit("Confirm these FACT_ECHO_TEST settings in training/native_sources.json before extracting:\n  "
                             + "\n  ".join(todo))
        sql.update(render_native_sql(a.native_dataset, a.cdm, ncfg, a.lookback_days))
    else:
        log.warning("--native-dataset not given: echo notes from FACT_ECHO_TEST are NOT included (LVEF coverage will be lower)")

    def q(name: str) -> pd.DataFrame:
        log.info("Running %s ...", name)
        df = spark.sql(sql[name])
        if name in VIEWS:                      # materialise once, reuse in later queries
            df = df.cache()
            df.createOrReplaceTempView(VIEWS[name])
            log.info("  %s: %d rows", VIEWS[name], df.count())
        return _decimals_to_float(df.toPandas())

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    attrition = write_attrition(q("00_cohort_attrition"), out)
    log.info("Eligibility: %s", attrition)
    cohort = q("01_cohort")
    log.info("Cohort: %d adult patients", len(cohort))
    imaging = q("06_baseline_imaging_notes")
    imaging["note_title"] = "Imaging report"
    if a.native_dataset:
        imaging = merge_notes(imaging, q("10_echo_notes_baseline"))
    imaging["note_id"] = imaging["note_id"].astype(str)
    feats = assemble_features(cohort, q("02_measurements"), q("03_history"), q("04_drugs"), imaging)
    feats.to_parquet(out / "features.parquet", index=False)
    imaging.to_parquet(out / "baseline_imaging_notes.parquet", index=False)
    q("05_candidate_codes").to_parquet(out / "candidate_codes.parquet", index=False)
    q("07_evidence_procedures").to_parquet(out / "evidence_procedures.parquet", index=False)
    q("08_evidence_labs").to_parquet(out / "evidence_labs.parquet", index=False)
    notes = q("09_event_notes")
    if a.native_dataset:
        notes = merge_notes(notes, q("11_echo_notes_event"))
    notes["note_id"] = notes["note_id"].astype(str)
    notes.to_parquet(out / "event_notes.parquet", index=False)
    log.info("Raw curation dataset written to %s — next: python -m curation.pipeline label", out)
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cdm", default="omop", help="Spark catalog.schema (or schema) holding the OMOP CDM tables")
    ap.add_argument("--index-start", default=INDEX_WINDOW[0])
    ap.add_argument("--index-end", default=INDEX_WINDOW[1])
    ap.add_argument("--lookback-days", type=int, default=730)
    ap.add_argument("--native-dataset", help="Spark catalog.schema holding Mayo native tables (FACT_ECHO_TEST); enables echo notes")
    ap.add_argument("--native-config", help="JSON overriding training/codesets.NATIVE_NOTE_SOURCES (default training/native_sources.json)")
    ap.add_argument("--render-only", action="store_true", help="write SQL to training/sql/ and exit (no data access)")
    ap.add_argument("--out", default="data/raw")
    return ap.parse_args(argv)


def main(argv=None) -> None:
    a = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.render_only:
        sql = render_sql(a.cdm, a.index_start, a.index_end, a.lookback_days)
        ncfg = load_native_config(a.native_config)
        sql.update(render_native_sql(a.native_dataset or "NATIVE_SCHEMA", a.cdm, ncfg, a.lookback_days))
        SQL_DIR.mkdir(exist_ok=True)
        for old in SQL_DIR.glob("*.sql"):
            old.unlink()
        for name, q in sql.items():
            (SQL_DIR / f"{name}.sql").write_text(q)
        log.info("Wrote %d Spark SQL files to %s", len(sql), SQL_DIR)
        todo = unconfirmed(ncfg)
        if todo:
            log.warning("Echo-note SQL (10_, 11_) has unconfirmed columns:\n  " + "\n  ".join(todo))
        return
    run_extract(get_spark(), a)


if __name__ == "__main__":
    main()
