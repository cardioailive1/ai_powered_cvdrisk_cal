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


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    d = tmp_path_factory.mktemp("spark")
    raw = generate_raw(n=400, seed=9)
    spark = (pyspark.SparkSession.builder.master("local[2]").appName("cardioai-test")
             .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "4").getOrCreate())
    for name, df in build_omop(raw).items():
        sdf = (spark.createDataFrame([], "person_id bigint, observation_source_concept_id bigint, observation_date date")
               if df.empty else spark.createDataFrame(df))
        sdf.createOrReplaceGlobalTempView(name)
    cfg = d / "native.json"
    cfg.write_text(json.dumps({"echo": {"patient_column": "PATIENT_CLINIC_NUMBER", "date_column": "ECHO_DTM",
                                        "id_column": "ECHO_ID", "person_bridge": "person_source_value"}}))
    a = extract.parse_args(["--cdm", "global_temp", "--native-dataset", "global_temp", "--native-config", str(cfg),
                            "--out", str(d / "raw")])
    extract.run_extract(spark, a)
    out = {p.stem: pd.read_parquet(p) for p in (d / "raw").glob("*.parquet")}
    return raw, out


def test_cohort_and_features(extracted):
    raw, out = extracted
    f, src = out["features"].set_index("person_id"), raw["features"].set_index("person_id")
    assert len(f) == len(src)                                              # every synthetic patient enters the cohort
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


def test_candidates_evidence_and_notes(extracted):
    raw, out = extracted
    key = ["person_id", "family", "code"]
    assert len(out["candidate_codes"].drop_duplicates(key + ["event_date"])) == len(raw["candidate_codes"].drop_duplicates(key + ["event_date"]))
    assert len(out["evidence_labs"]) == len(raw["evidence_labs"])
    assert set(out["evidence_procedures"]["category"]) == set(raw["evidence_procedures"]["category"])
    notes = out["event_notes"]
    assert notes["note_id"].str.startswith("ECHO-").any()                  # echo notes came from FACT_ECHO_TEST
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
    cfg.write_text(json.dumps({"echo": {"patient_column": "PATIENT_CLINIC_NUMBER", "date_column": "ECHO_DTM",
                                        "id_column": "ECHO_ID", "person_bridge": "person_source_value"}}))
    rep = run_preflight(spark, "global_temp", "global_temp", str(cfg))
    assert all(rep["omop_tables"].values()) and rep["fact_echo_test_exists"]
    assert {c["name"] for c in rep["fact_echo_test_columns"]} >= {"ECHO_NOTE", "ECHO_DTM", "PATIENT_CLINIC_NUMBER"}
    assert rep["echo_person_bridge"]["match_rate"] == 1.0
    assert rep["condition_status"] and rep["troponin_by_year"] and rep["imaging_notes_by_year"]
