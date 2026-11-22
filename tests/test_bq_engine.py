"""BigQuery engine (Mayo Clinic Platform): the extraction and preflight SQL, translated to BigQuery, run end to end.

There is no BigQuery here, so a stand-in backend receives exactly the BigQuery SQL the engine generates (hyphenated
project ids, backtick quoting, <project>.temp_tables for intermediate tables) and runs it in DuckDB on the mock OMOP +
Mayo native tables. The results are checked like the Spark extraction's and, when pyspark is installed, compared
with the Spark extraction table by table."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
duckdb = pytest.importorskip("duckdb")
sqlglot = pytest.importorskip("sqlglot")

from omop_mock import INELIGIBLE, NATIVE, build_omop, make_ineligible  # noqa: E402
from training import extract  # noqa: E402
from training.bq_engine import BigQueryEngine, to_bigquery  # noqa: E402
from training.labeling import build_episodes  # noqa: E402
from training.synthetic_raw import generate_raw  # noqa: E402

DATA = "mcp-ss-data-p-test.vw_accelerate_core_v1"        # same shape as MCP's: hyphenated project, one dataset
LOCAL_PROJECT = "mcp-acc-000-dbg-p-test"


def _q(path: str) -> str:
    return ".".join(f'"{p}"' for p in path.split("."))


class DuckBackend:
    """Stand-in for BigQuery: takes BigQuery SQL, runs it in DuckDB."""

    def __init__(self, tables: dict[str, pd.DataFrame]):
        self.project = LOCAL_PROJECT
        self.con = duckdb.connect()
        proj, ds = DATA.split(".")
        for cat, schema in ((proj, ds), (LOCAL_PROJECT, "temp_tables")):
            self.con.execute(f'ATTACH \':memory:\' AS "{cat}"')
            self.con.execute(f'CREATE SCHEMA "{cat}"."{schema}"')
        self.con.execute("CREATE MACRO farm_fingerprint(x) AS hash(x)")
        for name, df in tables.items():
            self.con.register("_df", df)
            self.con.execute(f'CREATE TABLE {_q(DATA)}."{name}" AS SELECT * FROM _df')
            self.con.unregister("_df")
        self.sql_seen: list[str] = []

    def _duck(self, bq_sql: str) -> str:
        self.sql_seen.append(bq_sql)
        return sqlglot.transpile(bq_sql, read="bigquery", write="duckdb")[0]

    def query(self, bq_sql: str) -> pd.DataFrame:
        return self.con.execute(self._duck(bq_sql)).df()

    def create_table(self, path: str, select_sql: str) -> None:
        self.con.execute(f"CREATE OR REPLACE TABLE {_q(path)} AS {self._duck(select_sql)}")

    def columns(self, path: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.con.execute(f"DESCRIBE {_q(path)}").fetchall()]


@pytest.fixture(scope="module")
def bq(tmp_path_factory):
    d = tmp_path_factory.mktemp("bq")
    raw = generate_raw(n=400, seed=9)
    tables = make_ineligible(raw, build_omop(raw))
    inel = dict(INELIGIBLE)
    backend = DuckBackend({k: v for k, v in tables.items() if not v.empty or k == "observation"})
    cfg = d / "native.json"
    cfg.write_text(json.dumps(NATIVE))
    a = extract.parse_args(["--engine", "bigquery", "--cdm", DATA, "--native-dataset", DATA,
                            "--native-config", str(cfg), "--out", str(d / "raw")])
    eng, a = extract.session(a, backend=backend)
    extract.run_extract(eng, a)
    out = {p.stem: pd.read_parquet(p) for p in (d / "raw").glob("*.parquet")}
    out["attrition"] = json.loads((d / "raw" / "cohort_attrition.json").read_text())
    return {"raw": raw, "out": out, "backend": backend, "eng": eng, "inel": inel, "cfg": cfg}


def test_sql_is_bigquery(bq):
    seen = "\n".join(bq["backend"].sql_seen)
    assert "`mcp-ss-data-p-test`.`vw_accelerate_core_v1`.`person`" in seen        # real, quoted names
    assert f"`{LOCAL_PROJECT}`.`temp_tables`.`cvd_cohort`" in seen               # intermediate tables in temp_tables
    for spark_only in ("xxhash64", "/*+", "__CDM__", "__NAT__", "RLIKE", "make_date"):
        assert spark_only not in seen, spark_only
    assert "FARM_FINGERPRINT" in seen and "DATE_ADD(" in seen


def test_bigquery_extract(bq):
    raw, out, inel = bq["raw"], bq["out"], bq["inel"]
    f = out["features"]
    assert not f["person_id"].isin(inel).any() and len(f) == raw["features"]["person_id"].nunique() - len(inel)
    assert f["age"].between(18, 90).all()
    notes = out["event_notes"]
    assert notes["note_id"].str.startswith("ECHO-").any() and notes["note_id"].str.startswith("ECG-").any()
    assert notes["note_id"].is_unique
    c = out["candidate_codes"]
    assert set(c["primary_native"]) == {0, 1}
    cands = build_episodes(c, out["evidence_procedures"], f)
    assert set(cands["family"]) == {"ami", "stroke", "hf", "scd", "af"}
    assert out["attrition"]["counts"]


def test_same_as_spark(bq, tmp_path_factory):
    """Table by table, the BigQuery extract holds the same rows as the Spark extract."""
    pytest.importorskip("pyspark.sql")
    from test_spark_extract import run_spark_extract
    spark_out = run_spark_extract(tmp_path_factory.mktemp("spark_for_bq"))[1]
    b = bq["out"]
    keys = {"features": ["person_id", "index_date", "sex", "age"], "candidate_codes": ["person_id", "event_date", "code", "family"],
            "evidence_labs": ["person_id", "analyte", "value"], "evidence_procedures": ["person_id", "procedure_date", "code"],
            "event_notes": ["person_id", "note_id", "note_date"], "baseline_imaging_notes": ["person_id", "note_id"]}
    for name, k in keys.items():
        s, q = spark_out[name], b[name]
        assert len(s) == len(q), (name, len(s), len(q))
        norm = lambda df: set(map(tuple, df[k].astype(str).apply(lambda c: c.str[:10] if "date" in c.name else c).values))  # noqa: E731
        assert norm(s) == norm(q), name
    assert spark_out["attrition"] == b["attrition"]


def test_preflight_bigquery(bq):
    from training.preflight import run_preflight
    eng = BigQueryEngine(DATA, DATA, backend=bq["backend"])
    rep = run_preflight(eng, "__CDM__", "__NAT__", str(bq["cfg"]))
    assert all(rep["omop_tables"].values()) and rep["fact_echo_test_exists"]
    assert rep["echo_person_bridge"]["match_rate"] == 1.0 and rep["ecg_person_link"]["match_rate"] == 1.0
    assert rep["omop_person_column_candidates"][0] == {"column": "person_source_value", "match_rate": 1.0}
    assert rep["condition_status"] and rep["troponin_by_year"] and rep["imaging_notes_by_year"]


def test_dataset_names_validated():
    with pytest.raises(SystemExit):
        BigQueryEngine("just_a_dataset", backend=object())
    sql = to_bigquery("SELECT /*+ BROADCAST(c) */ xxhash64(concat(a, b)) FROM __CDM__.person JOIN cvd_cohort c ON 1=1",
                      {"__CDM__": "p-1.ds"}, {"cvd_cohort": "p-2.temp_tables.cvd_cohort"})
    assert "`p-1`.`ds`.`person`" in sql and "`p-2`.`temp_tables`.`cvd_cohort`" in sql and "FARM_FINGERPRINT" in sql


def test_preflight_reports_real_errors(bq, monkeypatch):
    """A missing package or a permission error is reported as such, not as 'missing OMOP tables'."""
    from training import preflight
    eng = BigQueryEngine(DATA, DATA, backend=bq["backend"])

    def no_sqlglot(*a, **k):
        raise ImportError("No module named 'sqlglot'")
    monkeypatch.setattr(eng, "translate", no_sqlglot)
    rep = preflight.run_preflight(eng, "__CDM__", "__NAT__", str(bq["cfg"]))
    assert "package is missing" in rep["error"] and "sqlglot" in rep["error"]

    class Denied(Exception):
        pass

    def denied(*a, **k):
        raise Denied("403 Access Denied: User does not have bigquery.tables.getData permission")
    monkeypatch.setattr(bq["backend"], "query", denied)
    eng2 = BigQueryEngine(DATA, DATA, backend=bq["backend"])
    rep = preflight.run_preflight(eng2, "__CDM__", "__NAT__", str(bq["cfg"]))
    assert "Access Denied" in rep["error"] and "Missing OMOP" not in rep["error"]


def test_preflight_missing_table_still_reported(bq):
    from training import preflight
    eng = BigQueryEngine("mcp-ss-data-p-test.no_such_dataset", DATA, backend=bq["backend"])
    rep = preflight.run_preflight(eng, "__CDM__", "__NAT__", str(bq["cfg"]))
    assert rep["error"].startswith("Missing OMOP tables")
