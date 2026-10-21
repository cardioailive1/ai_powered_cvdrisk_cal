"""Preflight checks run in the Mayo Clinic Platform workspace BEFORE extraction (Spark SQL, read-only).

Answers the open setup questions from the data itself and writes data/preflight.json:
  * which OMOP tables exist in --cdm
  * columns of DIM_PATIENT, FACT_ECHO_TEST, FACT_DIAGNOSIS and FACT_MORTALITY_STATUS (to fill
    training/native_sources.json); which OMOP person column holds PATIENT_CLINIC_NUMBER; once configured, how many
    native rows join to OMOP person_id, and whether any clinic number is split over two person_ids (train/test leak)
  * PRIMARY_DIAGNOSIS_FLAG values in FACT_DIAGNOSIS; deaths by year in FACT_MORTALITY_STATUS
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


def _table(spark, tbl: str) -> dict:
    if not _exists(spark, tbl):
        return {"exists": False}
    return {"exists": True, "rows": spark.sql(f"SELECT count(*) AS n FROM {tbl}").collect()[0]["n"],
            "columns": [{"name": f.name, "type": f.dataType.simpleString()} for f in spark.table(tbl).schema.fields]}


def native_preflight(spark, cdm: str, native: str, cfg: dict) -> dict:
    """Native-table checks: columns of every table (to fill native_sources.json), the DIM_PATIENT person key, how
    OMOP person links to it, and (once configured) match rates and value distributions. Aggregates only."""
    rep: dict = {}
    link = cfg["patient_link"]
    dim = f"{native}.{link['table']}"
    tables = {src: f"{native}.{cfg[src]['table']}" for src in ("echo", "diagnosis", "mortality")}
    rep["native_tables"] = {link["table"]: _table(spark, dim), **{cfg[s]["table"]: _table(spark, t) for s, t in tables.items()}}
    rep["native_unconfirmed"] = unconfirmed(cfg)
    # backwards-compatible keys
    e = rep["native_tables"][cfg["echo"]["table"]]
    rep["fact_echo_test_exists"] = e["exists"]
    if e["exists"]:
        rep["fact_echo_test_columns"], rep["fact_echo_test_rows"] = e["columns"], e["rows"]

    dk, mcn, mf = link["dk_column"], link["clinic_number_column"], link["merged_flag_column"]
    if rep["native_tables"][link["table"]]["exists"]:
        rep["dim_patient"] = spark.sql(f"""
            SELECT count(*) AS rows, count(DISTINCT {dk}) AS patient_dks, count(DISTINCT {mcn}) AS clinic_numbers,
                   sum(CASE WHEN {mf} = {_lit(link['unmerged_value'])} THEN 1 ELSE 0 END) AS unmerged_rows
            FROM {dim}""").collect()[0].asDict()
        # each PATIENT_DK must map to exactly one clinic number; unmerged rows must be one per clinic number
        rep["dim_patient"]["dks_with_several_clinic_numbers"] = spark.sql(f"""
            SELECT count(*) AS n FROM (SELECT {dk} FROM {dim} GROUP BY {dk} HAVING count(DISTINCT {mcn}) > 1)""").collect()[0]["n"]
        rep["dim_patient"]["clinic_numbers_with_several_unmerged_rows"] = spark.sql(f"""
            SELECT count(*) AS n FROM (SELECT {mcn} FROM {dim} WHERE {mf} = {_lit(link['unmerged_value'])}
                                       GROUP BY {mcn} HAVING count(*) > 1)""").collect()[0]["n"]
        # which OMOP person column holds the clinic number: match rate of each string column of person
        cols = [f.name for f in spark.table(f"{cdm}.person").schema.fields
                if f.dataType.simpleString() == "string" or f.name.endswith("source_value")]
        cand = []
        for col in cols:
            r = spark.sql(f"""
                SELECT count(*) AS persons, count(d.k) AS matched
                FROM {cdm}.person p
                LEFT JOIN (SELECT DISTINCT cast({mcn} AS STRING) AS k FROM {dim}) d ON d.k = cast(p.{col} AS STRING)""").collect()[0]
            cand.append({"column": col, "match_rate": round(r["matched"] / max(r["persons"], 1), 4)})
        rep["omop_person_column_candidates"] = sorted(cand, key=lambda x: -x["match_rate"])
        omop = link["omop_person_column"]
        if not omop.startswith("CONFIRM:"):
            # the train/test split is by OMOP person_id: one clinic number must not be split over two person_ids
            rep["clinic_numbers_with_several_person_ids"] = spark.sql(f"""
                SELECT count(*) AS n FROM (SELECT {omop} FROM {cdm}.person WHERE {omop} IS NOT NULL
                                           GROUP BY {omop} HAVING count(DISTINCT person_id) > 1)""").collect()[0]["n"]

    def match(src: str) -> dict | None:
        if unconfirmed(cfg, ("patient_link",)) or cfg[src]["patient_column"].startswith("CONFIRM:"):
            return None
        if not rep["native_tables"][cfg[src]["table"]]["exists"]:
            return None
        x = cfg[src]
        if x["patient_column"] == dk:
            j = (f"LEFT JOIN {dim} d ON d.{dk} = x.{x['patient_column']} "
                 f"LEFT JOIN {cdm}.person p ON cast(p.{link['omop_person_column']} AS STRING) = cast(d.{mcn} AS STRING)")
        else:
            j = f"LEFT JOIN {cdm}.person p ON cast(p.{link['omop_person_column']} AS STRING) = cast(x.{x['patient_column']} AS STRING)"
        r = spark.sql(f"SELECT count(*) AS rows, count(p.person_id) AS matched_to_omop FROM {tables[src]} x {j}").collect()[0].asDict()
        r["match_rate"] = round(r["matched_to_omop"] / max(r["rows"], 1), 4)
        return r

    for src, key in (("echo", "echo_person_bridge"), ("diagnosis", "diagnosis_person_link"), ("mortality", "mortality_person_link")):
        r = match(src)
        if r is not None:
            rep[key] = r
    d = cfg["diagnosis"]
    if rep["native_tables"][d["table"]]["exists"]:
        rep["primary_diagnosis_flag_values"] = _rows(spark, f"""
            SELECT cast({d['primary_flag_column']} AS STRING) AS value, count(*) AS n FROM {tables['diagnosis']} GROUP BY 1 ORDER BY n DESC""", 20)
    m = cfg["mortality"]
    if rep["native_tables"][m["table"]]["exists"]:
        dd = "NULL" if m["death_date_column"].startswith("CONFIRM:") else m["death_date_column"]
        rep["mortality_by_year"] = _rows(spark, f"""
            SELECT year(to_date({dd})) AS death_year, count(*) AS n,
                   round(avg(CASE WHEN {m['last_live_column']} IS NOT NULL THEN 1.0 ELSE 0.0 END), 3) AS last_live_filled
            FROM {tables['mortality']} GROUP BY 1 ORDER BY 1""", 100)
    return rep


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
        rep.update(native_preflight(spark, cdm, native, load_native_config(native_config)))
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
