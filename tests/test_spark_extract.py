"""Round-trip test of the Spark SQL extraction: synthetic patients -> mock OMOP + FACT_ECHO_TEST tables in a
local Spark session -> training.extract -> raw curation dataset -> auto-labelling.
Skipped automatically when pyspark (and Java) are not available."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
pyspark = pytest.importorskip("pyspark.sql")

from omop_mock import build_omop  # noqa: E402
from training import extract  # noqa: E402
from training.labeling import build_episodes  # noqa: E402
from training.synthetic_raw import generate_raw  # noqa: E402


from omop_mock import INELIGIBLE, LATE_DEATH, NATIVE  # noqa: E402,F401  (shared with test_bq_engine)
from omop_mock import make_ineligible as _make_ineligible  # noqa: E402


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    return run_spark_extract(tmp_path_factory.mktemp("spark"))


def run_spark_extract(d):
    raw = generate_raw(n=400, seed=9)
    spark = (pyspark.SparkSession.builder.master("local[2]").appName("cardioai-test")
             .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "4").getOrCreate())
    for name, df in _make_ineligible(raw, build_omop(raw)).items():
        sdf = (spark.createDataFrame([], "person_id bigint, observation_source_concept_id bigint, observation_date date")
               if df.empty else spark.createDataFrame(df))
        sdf.createOrReplaceGlobalTempView(name)
    cfg = d / "native.json"
    cfg.write_text(json.dumps(NATIVE))
    a = extract.parse_args(["--cdm", "global_temp", "--native-dataset", "global_temp", "--native-config", str(cfg),
                            "--out", str(d / "raw")])
    extract.run_extract(spark, a)
    out = {p.stem: pd.read_parquet(p) for p in (d / "raw").glob("*.parquet")}
    out["attrition"] = json.loads((d / "raw" / "cohort_attrition.json").read_text())
    for k in ("features", "candidate_codes", "evidence_labs", "evidence_procedures"):   # compare eligible patients only
        raw[k] = raw[k][~raw[k]["person_id"].isin(INELIGIBLE)]
    return raw, out


def test_eligibility_adults_only(extracted):
    raw, out = extracted
    f = out["features"]
    assert not f["person_id"].isin(INELIGIBLE).any()                    # all four ineligible patients removed
    assert f["age"].min() >= 18 and f["age"].max() < 90                 # adults 18-89 only, no pediatric patients
    a = out["attrition"]
    assert a["age_min"] == 18 and a["age_max"] == 89
    c = a["counts"]
    assert c["final_cohort"] == len(f) and c["with_eligible_visit"] == len(f) + 5
    for k in ("excluded_pediatric_only", "excluded_age_90_plus_only", "excluded_sex_not_recorded", "excluded_died_before_index",
              "excluded_died_before_index_external_source"):
        assert c[k] == "<11", k                                          # 1 each, suppressed as a small cell


def test_cohort_and_features(extracted):
    raw, out = extracted
    f, src = out["features"].set_index("person_id"), raw["features"].set_index("person_id")
    assert len(f) == len(src)                                              # every eligible synthetic patient enters the cohort
    j = f.join(src, rsuffix="_src")
    assert (j["sex"] == j["sex_src"]).all() and (j["race"] == j["race_src"]).all()
    assert (abs(j["age"] - j["age_src"]) < 0.01).all()
    for k in ["sbp", "total_chol", "hdl", "egfr"]:
        m = j[k + "_src"].notna()
        assert (abs(j.loc[m, k] - j.loc[m, k + "_src"]) < 1e-6).all(), k
    for k in ["prior_mi", "heart_failure", "afib", "statin", "anticoagulant"]:
        assert (j[k] == j[k + "_src"]).all(), k
    assert ((j["smoking"] == j["smoking_src"]).mean() > 0.99) and ((j["diabetes"] == j["diabetes_src"]).mean() > 0.9)
    assert (abs(j["followup_days"] - j["followup_days_src"]) <= 1).all()
    (late,) = LATE_DEATH                                                   # death 3 years after last contact: censored
    assert pd.isna(f.loc[late, "death_days"])


def test_primary_flag_from_fact_diagnosis(extracted):
    from training.codesets import PRIMARY_STATUS
    raw, out = extracted
    c = out["candidate_codes"]
    assert set(c["primary_native"]) == {0, 1}
    assert (c["primary_native"] == c["condition_status_concept_id"].isin(PRIMARY_STATUS).astype(int)).all()


def test_candidates_evidence_and_notes(extracted):
    raw, out = extracted
    key = ["person_id", "family", "code"]
    assert len(out["candidate_codes"].drop_duplicates(key + ["event_date"])) == len(raw["candidate_codes"].drop_duplicates(key + ["event_date"]))
    assert len(out["evidence_labs"]) == len(raw["evidence_labs"])
    assert set(out["evidence_procedures"]["category"]) == set(raw["evidence_procedures"]["category"])
    notes = out["event_notes"]
    assert notes["note_id"].str.startswith("ECHO-").any()                  # echo notes came from FACT_ECHO_TEST
    ecg = notes[notes["note_id"].str.startswith("ECG-")]                   # signed ECGs came from FACT_ECG
    raw_ecg = raw["event_notes"][(raw["event_notes"]["note_title"] == "ECG report")
                                 & ~raw["event_notes"]["person_id"].isin(INELIGIBLE)]
    assert len(ecg) == len(raw_ecg) and (ecg["note_title"] == "ECG interpretation").all()   # one row per ECG, not per snippet
    assert notes["note_id"].is_unique
    cands = build_episodes(out["candidate_codes"], out["evidence_procedures"], out["features"])
    assert set(cands["family"]) == {"ami", "stroke", "hf", "scd", "af"}


def test_render_only_has_no_bigquery_syntax():
    sql = extract.render_sql("cat.omop", "2012-01-01", "2019-12-31")
    blob = "\n".join(sql.values())
    for bq in ("REGEXP_CONTAINS", "FARM_FINGERPRINT", "UNNEST(", "INTERVAL", "STARTS_WITH", "DATE_DIFF"):
        assert bq not in blob, bq


def test_preflight_on_mock(extracted, tmp_path):
    from training.preflight import run_preflight
    spark = pyspark.SparkSession.builder.getOrCreate()
    cfg = tmp_path / "n.json"
    cfg.write_text(json.dumps(NATIVE))
    rep = run_preflight(spark, "global_temp", "global_temp", str(cfg))
    assert all(rep["omop_tables"].values()) and rep["fact_echo_test_exists"]
    assert {c["name"] for c in rep["fact_echo_test_columns"]} >= {"ECHO_NOTE", "ECHO_PERFORMED_DTM", "PATIENT_DK"}
    assert rep["native_unconfirmed"] == []
    assert rep["echo_person_bridge"]["match_rate"] == 1.0                 # including echoes filed under a merged DK
    assert rep["diagnosis_person_link"]["match_rate"] == 1.0 and rep["mortality_person_link"]["match_rate"] == 1.0
    assert rep["omop_person_column_candidates"][0] == {"column": "person_source_value", "match_rate": 1.0}
    assert rep["dim_patient"]["dks_with_several_clinic_numbers"] == 0
    assert rep["dim_patient"]["clinic_numbers_with_several_unmerged_rows"] == 0
    assert rep["clinic_numbers_with_several_person_ids"] == 0
    assert {r["value"] for r in rep["primary_diagnosis_flag_values"]} == {"Y", "N"}
    assert rep["condition_status"] and rep["troponin_by_year"] and rep["imaging_notes_by_year"]
