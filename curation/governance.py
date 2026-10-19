"""Governance configuration: thresholds, sampling design and outcome-definition
choices. Stored as data/governance.json; its SHA-256 is recorded in every label
run and release so the rules in force are always reproducible."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

DEFAULTS: dict = {
    "provenance": "mayo-clinic-platform",
    "label_thresholds": {"confirm": 0.8, "reject": 0.2},
    "sampling": {
        "max_uncertain_per_family": 300,     # all uncertain cases up to this cap are adjudicated
        "audit_sample_per_stratum": 50,      # random auto-confirmed and auto-rejected cases per family (PPV / NPV)
        "double_read_fraction": 1.0,         # fraction of event cases read by two cardiologists
        "annotation_sample": 100,            # imaging reports checked for LVEF / CAC extraction
        "seed": 20261019,
    },
    "gates": {
        "min_kappa": 0.6,                    # Cohen's kappa between independent readers, per family
        "min_double_reads_for_kappa": 10,
        "min_ppv_auto_confirmed": 0.85,
        "min_npv_auto_rejected": 0.85,
        "min_annotation_accuracy": 0.90,
        "max_indeterminate_fraction": 0.15,
    },
    # Mayo Clinic Platform (A. Resnick, 14 Oct 2026): up to 5 additional annotator seats, each needing a
    # cardioailive.com e-mail; Mayo Clinic staff cannot annotate in this environment (readers must be external).
    "reader_policy": {
        "email_domain": "cardioailive.com",
        "domain_roles": ["cardiologist", "lead_cardiologist"],
        "max_reader_seats": 5,
        "minutes_per_event_case": 4,
        "minutes_per_annotation_case": 1,
        "expected_disagreement_rate": 0.15,
    },
    "definitions": {
        "ami_include_type2": False,          # type 2 MI is not counted as an AMI endpoint event
        "af_exclude_postoperative": True,    # post-operative AF is not counted as new-onset AF
        "date_agreement_days": 7,
    },
    "audit": {
        "min_cohort": 1000,
        "implausible_warn": 0.001, "implausible_fail": 0.01,
        "unit_suspect_warn": 0.001, "unit_suspect_fail": 0.01,
        "core_missing_fail": 0.05, "missing_warn": 0.6,
        "missing_disparity_warn": 0.15,
        "min_subgroup_fraction": 0.01,
        "min_5y_followup": 0.3,
        "after_death_fail": 0.005,
        "max_uncertain_fraction": 0.4,
        "lf_conflict_warn": 0.25,
        "drift_ratio": [0.67, 1.5],
        "annotation_yield_warn": 0.9,
        "prevalence_5y": {"ami": [0.003, 0.08], "stroke": [0.003, 0.08], "hf": [0.005, 0.10],
                          "scd": [0.0005, 0.02], "af": [0.005, 0.12]},
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "governance.json"


def load(data_dir: str | Path) -> dict:
    p = path(data_dir)
    return _merge(DEFAULTS, json.loads(p.read_text())) if p.exists() else copy.deepcopy(DEFAULTS)


def save(data_dir: str | Path, cfg: dict) -> None:
    path(data_dir).write_text(json.dumps(cfg, indent=2))


def sha(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
