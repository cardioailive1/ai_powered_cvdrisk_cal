"""The expanded candidate code list: complete, documented, in sync with its rules, and what the SQL uses."""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from training import codelists, extract  # noqa: E402
from training.labeling import subtype  # noqa: E402

ROWS = list(csv.DictReader(codelists.CSV_PATH.open()))
BY = {(r["family"], r["code"]): r for r in ROWS}


def test_csv_matches_rules_and_official_code_sets():
    pytest.importorskip("simple_icd_10_cm")
    pytest.importorskip("icd9cms")
    assert codelists.CSV_PATH.read_text() == codelists.to_csv(codelists.expand()), "run: python -m training.codelists"


def test_every_row_documented_and_unique():
    keys = [(r["family"], r["vocabulary"], r["code"]) for r in ROWS]
    assert len(keys) == len(set(keys))
    assert all(r["description"] for r in ROWS)
    assert all(r["reason"] for r in ROWS if r["included"] == "no")
    assert {r["family"] for r in ROWS} == {"ami", "stroke", "hf", "scd", "af"}


@pytest.mark.parametrize("fam,code,included,hint", [
    ("ami", "I21.A1", "yes", "type2"), ("ami", "I21.02", "yes", "stemi"), ("ami", "410.71", "yes", "nstemi"),
    ("ami", "410.72", "no", "nstemi"),                     # subsequent episode of care: not a new MI
    ("stroke", "I63.411", "yes", "ischemic"), ("stroke", "I61.9", "yes", "hemorrhagic"), ("stroke", "G45.9", "yes", "tia"),
    ("stroke", "I62.01", "no", "hemorrhagic"), ("stroke", "G45.4", "no", "tia"), ("stroke", "433.11", "yes", "ischemic"),
    ("hf", "I50.22", "yes", "hfref"), ("hf", "I50.32", "yes", "hfpef"), ("hf", "428.0", "yes", ""),
    ("scd", "I47.2", "yes", "vt"), ("scd", "I47.20", "yes", "vt"), ("scd", "I49.01", "yes", "vf"), ("scd", "427.5", "yes", "arrest"),
    ("af", "I48.11", "yes", ""), ("af", "I48.1", "yes", ""), ("af", "I48.92", "yes", "flutter"), ("af", "427.31", "yes", ""),
])
def test_key_codes(fam, code, included, hint):
    r = BY[(fam, code)]
    assert r["included"] == included and r["subtype_hint"] == hint


def test_sql_matches_codes_exactly():
    sql = extract.render_sql("cat.omop", "2012-01-01", "2019-12-31")["05_candidate_codes"]
    assert "c.concept_code = s.code" in sql and "RLIKE" not in sql
    assert "'I21.A1'" in sql and "'410.71'" in sql
    assert "'410.72'" not in sql and "'I62.01'" not in sql and "'G45.4'" not in sql


def test_icd9_ami_subtypes_now_recognised():
    ctx = {"mentions": {}, "lvef": None}
    assert subtype("ami", ctx, "410.71", False) == "nstemi"
    assert subtype("ami", ctx, "410.11", False) == "stemi"
    assert subtype("ami", ctx, "I21.A1;I21.4", False) == "type2"
    assert subtype("af", ctx, "I48.92;427.32", False) == "flutter"
    assert subtype("af", ctx, "I48.92;I48.0", False) == "af"
