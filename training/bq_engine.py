"""BigQuery engine for the Mayo Clinic Platform workspace: runs the extraction and preflight SQL (written in Spark SQL)
on BigQuery, through the small part of the Spark API they use.

On MCP the OMOP CDM and the Mayo native tables (DIM_PATIENT, FACT_ECG, FACT_ECHO_TEST, FACT_DIAGNOSIS,
FACT_MORTALITY_STATUS, ...) are BigQuery views in one read-only dataset, e.g.
    mcp-ss-data-p-5o6i.vw_accelerate2605_core_v1
and each project can write only to its own `temp_tables` dataset (Mayo's mcp_data.core.BQClient uses the same rule).

    python -m training.extract   --engine bigquery --cdm mcp-ss-data-p-5o6i.vw_accelerate2605_core_v1 \\
                                 --native-dataset mcp-ss-data-p-5o6i.vw_accelerate2605_core_v1 --out data/raw
    python -m training.preflight --engine bigquery --cdm ... --native-dataset ...

How it works
  * every query is translated from Spark SQL to BigQuery SQL with sqlglot; Spark-only pieces are handled here:
    join hints are dropped, xxhash64 -> FARM_FINGERPRINT (deterministic index-visit pick), make_date -> DATE;
  * dataset names are passed to the SQL builders as placeholders and replaced with the real, backtick-quoted
    names after translation (BigQuery project ids contain hyphens, which Spark SQL cannot parse);
  * the intermediate results the SQL reuses (cvd_cohort, cvd_candidate_codes, cvd_evidence_procedures) are saved as
    tables in <your project>.temp_tables, expiring after TEMP_TABLE_DAYS days; only the final extract reaches the
    workspace folder. Nothing leaves the workspace.
Needs google-cloud-bigquery and db-dtypes (preinstalled for Mayo's notebooks; else pip install them).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

CDM_PLACEHOLDER = "__CDM__"
NATIVE_PLACEHOLDER = "__NAT__"
LOCAL_DATASET = "temp_tables"
TEMP_TABLE_DAYS = 7
_HINT = re.compile(r"/\*\+.*?\*/", re.S)
_RENAME = {"xxhash64": "FARM_FINGERPRINT", "make_date": "DATE"}


def to_bigquery(spark_sql: str, datasets: dict[str, str], views: dict[str, str]) -> str:
    """Spark SQL -> BigQuery SQL. datasets: placeholder -> 'project.dataset'; views: temp-view name -> table path."""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(_HINT.sub("", spark_sql), read="spark")

    def fix(node):
        if isinstance(node, exp.Anonymous) and node.name.lower() in _RENAME:
            return exp.Anonymous(this=_RENAME[node.name.lower()], expressions=node.expressions)
        if isinstance(node, exp.Table):
            if node.db and node.db in datasets:                    # placeholder.table -> project.dataset.table
                project, dataset = datasets[node.db].split(".", 1)
                table = node.name
            elif not node.db and node.name in views:               # temp view -> project.temp_tables.view
                project, dataset, table = views[node.name].split(".", 2)
            else:
                return node
            node.set("catalog", exp.to_identifier(project, quoted=True))
            node.set("db", exp.to_identifier(dataset, quoted=True))
            node.set("this", exp.to_identifier(table, quoted=True))
        return node

    return tree.transform(fix).sql(dialect="bigquery")


class Row(dict):
    """Enough of pyspark.sql.Row for the preflight: row['n'] and row.asDict()."""

    def asDict(self) -> dict:          # noqa: N802 (Spark's name)
        return dict(self)


@dataclass
class _Type:
    name: str

    def simpleString(self) -> str:     # noqa: N802
        return self.name.lower()


@dataclass
class _Field:
    name: str
    dataType: _Type                     # noqa: N815


class _Schema:
    def __init__(self, cols):
        self.fields = [_Field(n, _Type(t)) for n, t in cols]


class _Table:
    def __init__(self, cols):
        self.schema = _Schema(cols)


class Frame:
    """A lazily run query (what spark.sql returns)."""

    def __init__(self, eng: "BigQueryEngine", sql: str):
        self._eng, self._sql = eng, sql

    def cache(self) -> "Frame":
        return self

    def limit(self, n: int) -> "Frame":
        return Frame(self._eng, f"SELECT * FROM ({self._sql}) LIMIT {int(n)}")

    def createOrReplaceTempView(self, name: str) -> None:   # noqa: N802
        path = f"{self._eng.local}.{name}"
        self._eng.backend.create_table(path, self._sql)
        self._eng.views[name] = path
        self._sql = f"SELECT * FROM {_quote(path)}"

    def count(self) -> int:
        return int(self._eng.backend.query(f"SELECT count(*) AS n FROM ({self._sql})").iloc[0, 0])

    def toPandas(self) -> pd.DataFrame:     # noqa: N802
        return self._eng.backend.query(self._sql)

    def collect(self) -> list[Row]:
        df = self.toPandas()
        return [Row({k: (None if _isna(v) else v) for k, v in r.items()}) for r in df.to_dict("records")]


def _isna(v) -> bool:
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _quote(path: str) -> str:
    return ".".join(f"`{p}`" for p in path.split(".", 2)) if path.count(".") == 2 else f"`{path}`"


class BigQueryBackend:
    """Runs BigQuery SQL with google-cloud-bigquery (the workspace's default credentials and project)."""

    def __init__(self, client=None):
        if client is None:
            from google.cloud import bigquery
            client = bigquery.Client()
        self.client = client
        self.project = client.project

    def query(self, sql: str) -> pd.DataFrame:
        df = self.client.query(sql).result().to_dataframe(create_bqstorage_client=False)
        for c in df.columns:                               # db-dtypes DATE/TIME -> python objects (as Spark gives)
            if str(df[c].dtype) in ("dbdate", "dbtime"):
                df[c] = df[c].astype(object)
        return df

    def create_table(self, path: str, select_sql: str) -> None:
        self.client.query(
            f"CREATE OR REPLACE TABLE {_quote(path)} OPTIONS(expiration_timestamp = "
            f"TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {TEMP_TABLE_DAYS} DAY)) AS {select_sql}").result()

    def columns(self, path: str) -> list[tuple[str, str]]:
        return [(f.name, f.field_type) for f in self.client.get_table(path).schema]


class BigQueryEngine:
    """The subset of SparkSession used by training.extract and training.preflight, backed by BigQuery."""

    def __init__(self, cdm: str, native: str | None = None, backend=None, local_dataset: str = LOCAL_DATASET):
        for name, v in (("--cdm", cdm), ("--native-dataset", native)):
            if v is not None and v.count(".") != 1:
                raise SystemExit(f"{name} must be 'project.dataset' for BigQuery, got {v!r}")
        self.backend = backend or BigQueryBackend()
        self.local = f"{self.backend.project}.{local_dataset}"
        self.datasets = {CDM_PLACEHOLDER: cdm, **({NATIVE_PLACEHOLDER: native} if native else {})}
        self.views: dict[str, str] = {}

    def translate(self, spark_sql: str) -> str:
        return to_bigquery(spark_sql, self.datasets, self.views)

    def sql(self, spark_sql: str) -> Frame:
        return Frame(self, self.translate(spark_sql))

    def table(self, name: str) -> _Table:
        db, t = name.rsplit(".", 1)
        return _Table(self.backend.columns(f"{self.datasets.get(db, db)}.{t}"))
