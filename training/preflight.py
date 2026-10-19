"""Preflight checks run in the Mayo Clinic Platform workspace BEFORE extraction (Spark SQL, read-only).

Answers the open setup questions from the data itself and writes data/preflight.json:
  * which OMOP tables exist in --cdm
  * FACT_ECHO_TEST columns (to fill training/native_sources.json) and, once configured, how many echo
    rows join to OMOP person_id through the configured bridge
  * condition_status_concept_id distribution (which concepts mean "primary diagnosis")
  * troponin results by LOINC code and year, and how often range_high is filled (assay switch-over)
  * notes mentioning LVEF / calcium score by year (report coverage)

    python -m training.preflight --cdm <catalog>.<omop_schema> --native-dataset <catalog>.<native_schema>

Prints aggregate counts only (no patient-level rows or note text).
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import codesets as cs
from .extract import _lit, get_spark, load_native_config, unconfirmed

log = logging.getLogger("cardioai.preflight")
OMOP_TABLES = ["person", "observation_period", "visit_occurrence", "condition_occurrence", "observation", "measurement",
               "drug_exposure", "procedure_occurrence", "note", "death", "concept", "concept_ancestor"]


def _rows(spark, q: str, limit: int = 50) -> list[dict]:
    return [r.asDict() for r in spark.sql(q).limit(limit).collect()]


def _exists(spark, table: str) -> bool:
    try:
        spark.sql(f"SELECT 1 FROM {table} LIMIT 1").collect()
        return True
    except Exception:
        return False


def run_preflight(spark, cdm: str, native: str | None, native_config: str | None = None) -> dict:
    rep: dict = {"cdm": cdm, "native": native}
    rep["omop_tables"] = {t: _exists(spark, f"{cdm}.{t}") for t in OMOP_TABLES}
    missing = [t for t, ok in rep["omop_tables"].items() if not ok]
    if missing:
        rep["error"] = f"Missing OMOP tables in {cdm}: {', '.join(missing)}"
        return rep
    has_name = "concept_name" in [f.name for f in spark.table(f"{cdm}.concept").schema.fields]
    name = "c.concept_name" if has_name else "cast(NULL AS STRING)"
    rep["condition_status"] = _rows(spark, f"""
        SELECT x.condition_status_concept_id AS concept_id, {name} AS concept_name, count(*) AS n
        FROM {cdm}.condition_occurrence x LEFT JOIN {cdm}.concept c ON c.concept_id = x.condition_status_concept_id
        GROUP BY 1, 2 ORDER BY n DESC""", 25)
    rep["primary_status_configured"] = cs.PRIMARY_STATUS
    trop = ", ".join(_lit(c) for a, codes in cs.EVIDENCE_LOINC.items() if "troponin" in a for c in codes)
    rep["troponin_by_year"] = _rows(spark, f"""
        SELECT c.concept_code AS loinc, year(m.measurement_date) AS year, count(*) AS n,
               round(avg(CASE WHEN m.range_high IS NOT NULL THEN 1.0 ELSE 0.0 END), 3) AS range_high_filled,
               percentile_approx(m.range_high, 0.5) AS median_range_high
        FROM {cdm}.measurement m JOIN {cdm}.concept c ON c.concept_id = m.measurement_concept_id
        WHERE c.vocabulary_id = 'LOINC' AND c.concept_code IN ({trop})
        GROUP BY 1, 2 ORDER BY 2, 1""", 200)
    rep["imaging_notes_by_year"] = _rows(spark, f"""
        SELECT year(note_date) AS year,
               sum(CASE WHEN lower(note_text) RLIKE 'ejection fraction|lvef' THEN 1 ELSE 0 END) AS lvef_notes,
               sum(CASE WHEN lower(note_text) RLIKE 'agatston|calcium score' THEN 1 ELSE 0 END) AS cac_notes
        FROM {cdm}.note GROUP BY 1 ORDER BY 1""", 100)
    if native:
        cfg = load_native_config(native_config)
        e = cfg["echo"]
        tbl = f"{native}.{e['table']}"
        rep["fact_echo_test_exists"] = _exists(spark, tbl)
        if rep["fact_echo_test_exists"]:
            rep["fact_echo_test_columns"] = [{"name": f.name, "type": f.dataType.simpleString()} for f in spark.table(tbl).schema.fields]
            rep["fact_echo_test_rows"] = spark.sql(f"SELECT count(*) AS n FROM {tbl}").collect()[0]["n"]
            todo = unconfirmed(cfg)
            rep["native_unconfirmed"] = todo
            if not todo:
                r = spark.sql(f"""
                    SELECT count(*) AS echo_rows, count(p.person_id) AS matched_to_omop
                    FROM {tbl} x LEFT JOIN {cdm}.person p ON cast(p.{e['person_bridge']} AS STRING) = cast(x.{e['patient_column']} AS STRING)
                """).collect()[0].asDict()
                r["match_rate"] = round(r["matched_to_omop"] / max(r["echo_rows"], 1), 4)
                rep["echo_person_bridge"] = r
    return rep


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cdm", required=True)
    ap.add_argument("--native-dataset")
    ap.add_argument("--native-config")
    ap.add_argument("--out", default="data/preflight.json")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rep = run_preflight(get_spark(), a.cdm, a.native_dataset, a.native_config)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep, indent=2, default=str))
    print(f"\nWritten to {a.out}")


if __name__ == "__main__":
    main()
