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
    assert "`temp_tables`.`cvd_09_event_notes` WHERE MOD(person_id, NULLIF(20, 0)) = 19" in seen   # notes fetched in slices


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


def test_lipid_window_edges():
    """I3 (lipid panel 0-365 days before the visit), done with the ordered per-person list in 00a_visits, keeps exactly
    the visits the plain date-range join would keep: same day and day 365 count; day 366 and the day after do not."""
    v0 = pd.Timestamp("2015-06-01")
    lipid_offsets = {1: 0, 2: 365, 3: 366, 4: -1, 5: 100}       # person -> lipid days before the visit (-1 = day after)
    people = [1, 2, 3, 4, 5, 6]                                 # person 6: no lipid at all
    person = pd.DataFrame({"person_id": people, "year_of_birth": 1960, "month_of_birth": 1, "day_of_birth": 1,
                           "gender_concept_id": 8507, "race_concept_id": 0, "ethnicity_concept_id": 0})
    visits = pd.DataFrame({"person_id": people, "visit_concept_id": 9202, "visit_start_date": v0})
    visits = pd.concat([visits, pd.DataFrame({"person_id": [5], "visit_concept_id": [9202],    # 2nd visit, lipid 401 days before
                                              "visit_start_date": [v0 + pd.Timedelta(days=301)]})])
    op = pd.DataFrame({"person_id": people, "observation_period_start_date": pd.Timestamp("2010-01-01"),
                       "observation_period_end_date": pd.Timestamp("2022-01-01")})
    meas = pd.DataFrame({"person_id": list(lipid_offsets), "measurement_concept_id": 3027114,
                         "measurement_date": [v0 - pd.Timedelta(days=d) for d in lipid_offsets.values()]})
    concept = pd.DataFrame({"concept_id": [3027114], "vocabulary_id": ["LOINC"], "concept_code": ["2093-3"]})
    for df in (visits, op, meas):
        for c in df.columns:
            if c.endswith("_date"):
                df[c] = df[c].dt.date
    backend = DuckBackend({"person": person, "visit_occurrence": visits, "observation_period": op,
                           "measurement": meas, "concept": concept})
    eng = BigQueryEngine(DATA, backend=backend)
    sql = extract.render_sql("__CDM__", "2012-01-01", "2019-12-31")["00a_visits"]
    got = {(int(r["person_id"]), str(r["index_date"])[:10]) for r in eng.sql(sql).collect()}
    assert got == {(1, "2015-06-01"), (2, "2015-06-01"), (5, "2015-06-01")}


def test_resume_reuses_finished_note_files(bq, tmp_path, monkeypatch):
    """--resume keeps note files a stopped run finished; a file cut off mid-write is downloaded again. The saved
    imaging notes are re-read in small batches, so one patient's reports fall in several batches (as on MCP)."""
    import shutil
    monkeypatch.setattr(extract, "RESUME_BATCH_ROWS", 7)
    raw = tmp_path / "raw"
    shutil.copytree(Path(bq["cfg"]).parent / "raw", raw)
    full = (raw / "event_notes.parquet").read_bytes()
    (raw / "event_notes.parquet").write_bytes(full[: len(full) // 2])          # as if killed while writing
    for f in ("features", "candidate_codes"):
        (raw / f"{f}.parquet").unlink()
    backend = bq["backend"]
    seen_before = len(backend.sql_seen)
    a = extract.parse_args(["--engine", "bigquery", "--cdm", DATA, "--native-dataset", DATA, "--resume",
                            "--native-config", str(bq["cfg"]), "--out", str(raw)])
    eng, a = extract.session(a, backend=backend)
    extract.run_extract(eng, a)
    new_sql = "\n".join(backend.sql_seen[seen_before:])
    assert "cvd_06_baseline_imaging_notes" not in new_sql                       # imaging notes reused
    assert "cvd_09_event_notes" in new_sql                                      # partial event notes redone
    for f in ("features", "candidate_codes", "event_notes", "baseline_imaging_notes"):
        k = list(bq["out"][f].columns)
        again = pd.read_parquet(raw / f"{f}.parquet")
        norm = lambda d: d[k].astype(str).sort_values(k).reset_index(drop=True)   # noqa: E731
        assert norm(again).equals(norm(bq["out"][f])), f


def test_missing_values_from_bigquery_are_nan():
    """BigQuery returns a missing unit or note text as NaN (a float), not None."""
    nan = float("nan")
    from training.imaging_nlp import extract_cac, extract_lvef
    from training.nlp import sentences
    assert extract._harmonize("ldl", 3.0, nan) == 3.0
    assert extract_cac(nan) is None and extract_lvef(nan) is None
    assert sentences(nan) == []


def test_imaging_values_combine_across_batches():
    """One patient's reports split over two download batches: one row per patient, latest value of each kind."""
    from training.imaging_nlp import imaging_values, latest_imaging
    a = pd.DataFrame({"person_id": [1, 2], "note_date": ["2015-01-01", "2015-02-01"],
                      "note_text": ["Agatston calcium score 120. LVEF 40%.", "No measurements."]})
    b = pd.DataFrame({"person_id": [1], "note_date": ["2016-01-01"], "note_text": ["Ejection fraction 55%."]})
    out = latest_imaging(pd.concat([imaging_values(a), imaging_values(b)]))
    assert out.index.is_unique and list(out.index) == [1]
    assert out.loc[1, "lvef"] == 55 and out.loc[1, "cac_score"] == 120
    cohort = pd.DataFrame({"person_id": [1, 2], "index_date": ["2016-06-01"] * 2, "age": [60.0, 61.0],
                           "gender_concept_id": [8507, 8532], "race_concept_id": [0, 0], "ethnicity_concept_id": [0, 0],
                           "obs_end": ["2020-01-01"] * 2, "death_date": [None, None]})
    empty = pd.DataFrame(columns=["person_id", "feature", "value", "unit"])
    f = extract.assemble_features(cohort, empty, pd.DataFrame(columns=["person_id", "flag", "last_date"]),
                                  pd.DataFrame(columns=["person_id", "flag"]), None, imaging_parsed=out)
    assert f.set_index("person_id").loc[1, "lvef"] == 55 and pd.isna(f.set_index("person_id").loc[2, "lvef"])
