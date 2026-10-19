"""Build a locked, signed training release from an approved label run.

Final label per candidate:
  adjudicated (final)   -> cardiologist decision (event / not_event / indeterminate)
  not adjudicated       -> auto-label: confirmed -> event, rejected -> not_event, uncertain -> unknown
Definition rules from governance (type 2 MI, post-operative AF) are then applied.

Per model endpoint the release stores
  days_<endpoint>     first confirmed event day after index
  unknown_<endpoint>  first unresolved candidate day (indeterminate / unadjudicated uncertain /
                      event with unknown subtype for subtype endpoints)
train.py treats a horizon as unlabelled for a patient when an unknown occurs
within it before any confirmed event, rather than guessing.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from cardioai_core.schema import ENDPOINT_FAMILY, ENDPOINT_SUBTYPES, FIELDS
from cardioai_core.security import sha256_file

from . import gates as gates_mod
from . import governance
from .adjudication import metrics as adj_metrics
from .runs import latest_audit, load_raw
from .store import Approval, Case, LabelRun, Release, User, log_event


class ReleaseError(RuntimeError):
    pass


def final_labels(labels: pd.DataFrame, cases: list[Case], gov: dict) -> pd.DataFrame:
    L = labels[["candidate_id", "person_id", "family", "event_date", "days_from_index", "auto_label", "subtype_auto", "source"]].copy()
    L["event_date"] = pd.to_datetime(L["event_date"])
    L["status"] = L["auto_label"].map({"confirmed": "event", "rejected": "not_event", "uncertain": "unknown"})
    L["subtype"] = np.where(L["status"] == "event", L["subtype_auto"], None)
    L["label_source"] = "auto"
    adj = {c.ref_id: c.final for c in cases if c.case_type == "event" and c.status == "final"}
    m = L["candidate_id"].isin(adj)
    for i in np.flatnonzero(m.to_numpy()):
        f = adj[L.iat[i, 0]]
        L.iat[i, L.columns.get_loc("status")] = "unknown" if f["outcome"] == "indeterminate" else f["outcome"]
        L.iat[i, L.columns.get_loc("subtype")] = f.get("subtype")
        L.iat[i, L.columns.get_loc("label_source")] = "adjudicated"
        if f["outcome"] == "event" and f.get("event_date"):
            new_d = pd.Timestamp(f["event_date"])
            shift = (new_d - L.iat[i, L.columns.get_loc("event_date")]).days
            L.iat[i, L.columns.get_loc("event_date")] = new_d
            L.iat[i, L.columns.get_loc("days_from_index")] = L.iat[i, L.columns.get_loc("days_from_index")] + shift
    D = gov["definitions"]
    if not D["ami_include_type2"]:
        L.loc[(L["family"] == "ami") & (L["status"] == "event") & (L["subtype"] == "type2"), "status"] = "not_event"
    if D["af_exclude_postoperative"]:
        L.loc[(L["family"] == "af") & (L["status"] == "event") & (L["subtype"] == "postoperative"), "status"] = "not_event"
    # an adjudicated event moved onto/before the index date is not a post-index outcome
    L.loc[(L["status"] == "event") & (L["days_from_index"] <= 0), "status"] = "not_event"
    return L


def endpoint_columns(L: pd.DataFrame, person_ids: pd.Series) -> pd.DataFrame:
    out = pd.DataFrame({"person_id": person_ids})
    for ep, fam in ENDPOINT_FAMILY.items():
        g = L[L["family"] == fam]
        if ep in ENDPOINT_SUBTYPES:
            spec = ENDPOINT_SUBTYPES[ep]
            is_ev = (g["status"] == "event") & g["subtype"].isin(spec["match"])
            is_unk = (g["status"] == "unknown") | ((g["status"] == "event") & ~g["subtype"].isin(spec["match"] | spec["known_other"]))
        else:
            is_ev = g["status"] == "event"
            is_unk = g["status"] == "unknown"
        ev = g[is_ev].groupby("person_id")["days_from_index"].min()
        unk = g[is_unk].groupby("person_id")["days_from_index"].min()
        out[f"days_{ep}"] = out["person_id"].map(ev)
        out[f"unknown_{ep}"] = out["person_id"].map(unk)
    return out


def build_release(db: Session, data_dir, run: LabelRun, user: User) -> Release:
    gov = governance.load(data_dir)
    labels = pd.read_parquet(Path(run.path) / "labels.parquet")
    ar = latest_audit(db, run.id)
    from .runs import get_run_data
    rd = get_run_data(data_dir, run)
    ann = {nid: rd.annotation_packet(nid)["extracted"] for nid in
           [c.ref_id for c in db.scalars(select(Case).where(Case.label_run_id == run.id, Case.case_type == "annotation"))]}
    m = adj_metrics(db, run.id, labels, ann)
    gates = gates_mod.evaluate(db, data_dir, run, ar.report if ar else None, m)
    if not gates_mod.releasable(gates):
        bad = [f"{g['id']} ({g['status']})" for g in gates if g["status"] not in ("pass", "waived")]
        raise ReleaseError("Release blocked by gates: " + ", ".join(bad))

    cases = list(db.scalars(select(Case).where(Case.label_run_id == run.id)))
    L = final_labels(labels, cases, gov)
    raw = load_raw(data_dir)
    feats = raw["features"].copy()
    cleaned = {}
    for f in FIELDS:
        if f["type"] == "number" and f["key"] in feats:
            bad = (feats[f["key"]] < f["min"]) | (feats[f["key"]] > f["max"])
            cleaned[f["key"]] = int(bad.sum())
            feats.loc[bad, f["key"]] = np.nan
    ds = feats.merge(endpoint_columns(L, feats["person_id"]), on="person_id", how="left")

    rid = f"REL-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    out = Path(data_dir) / "releases" / rid
    out.mkdir(parents=True, exist_ok=True)
    ds.to_parquet(out / "dataset.parquet", index=False)
    L.to_parquet(out / "final_labels.parquet", index=False)
    dsha = sha256_file(out / "dataset.parquet")
    approvals = [{"stage": a.stage, "decision": a.decision, "user": db.get(User, a.user_id).username,
                  "role": db.get(User, a.user_id).role, "at": a.created_at.isoformat(), "comment": a.comment}
                 for a in db.scalars(select(Approval).where(Approval.label_run_id == run.id).order_by(Approval.id))]
    counts = {ep: {"events": int(ds[f"days_{ep}"].notna().sum()), "unknown_any": int(ds[f"unknown_{ep}"].notna().sum())}
              for ep in ENDPOINT_FAMILY}
    src = L[L["status"] == "event"]["label_source"].value_counts().to_dict()
    summary = {
        "release_id": rid, "label_run_id": run.id, "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "created_by": user.username, "provenance": run.summary.get("provenance"), "synthetic_data": bool(run.summary.get("synthetic")),
        "simulated_adjudication": bool(run.summary.get("simulated_adjudication")),
        "dataset_sha256": dsha, "inputs_sha256": run.inputs_sha, "label_model": run.label_model_version,
        "label_model_sha256": run.label_model_sha, "governance_sha256": governance.sha(gov), "governance": gov,
        "audit_report_sha256": ar.sha if ar else None, "gates": gates, "approvals": approvals,
        "adjudication": {"families": {f: {k: v for k, v in x.items() if k != "strata"} for f, x in m["families"].items()},
                         "annotation": m["annotation"], "totals": m["totals"]},
        "event_label_sources": {k: int(v) for k, v in src.items()}, "endpoint_counts": counts, "implausible_values_cleared": cleaned,
        "n_patients": int(len(ds)),
    }
    (out / "release.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "RELEASE_REPORT.md").write_text(release_markdown(summary))
    rel = Release(id=rid, label_run_id=run.id, created_by=user.username, path=str(out), dataset_sha=dsha,
                  summary={k: summary[k] for k in ("endpoint_counts", "event_label_sources", "n_patients", "simulated_adjudication")})
    db.add(rel)
    log_event(db, user.username, "release.created", rid, {"label_run": run.id, "dataset_sha256": dsha, "path": str(out)})
    db.commit()
    return rel


def release_markdown(s: dict) -> str:
    L = [f"# Training release {s['release_id']}", "",
         f"- Label run: {s['label_run_id']} · label model {s['label_model']} · provenance **{s['provenance']}**",
         f"- Dataset SHA-256: `{s['dataset_sha256']}`", f"- Patients: {s['n_patients']:,}"]
    if s["synthetic_data"] or s["simulated_adjudication"]:
        L += ["", "> **NOT FOR CLINICAL USE** — " + ("synthetic data; " if s["synthetic_data"] else "")
              + ("adjudication was SIMULATED, not performed by cardiologists." if s["simulated_adjudication"] else "")]
    L += ["", "## Sign-offs", ""] + [f"- {a['stage']}: **{a['decision']}** by {a['user']} ({a['role']}) at {a['at']} — {a['comment']}" for a in s["approvals"]]
    L += ["", "## Adjudication", "", "| Family | Cases | κ | PPV auto-confirmed | NPV auto-rejected | Lead-resolved | Indeterminate |", "|---|---|---|---|---|---|---|"]
    fmt = lambda v: "—" if v is None else f"{v:.2f}"  # noqa: E731
    for f, x in s["adjudication"]["families"].items():
        L.append(f"| {f} | {x['cases']} | {fmt(x['kappa'])} | {fmt(x['ppv_auto_confirmed'])} | {fmt(x['npv_auto_rejected'])} | "
                 f"{x['lead_resolved']} | {fmt(x['indeterminate_fraction'])} |")
    a = s["adjudication"]["annotation"]
    L += ["", f"Annotation accuracy — LVEF {fmt(a['lvef']['accuracy'])}, CAC {fmt(a['cac_score']['accuracy'])}", "",
          "## Gates", ""] + [f"- {g['status'].upper()}: {g['title']} — {g['detail']}" for g in s["gates"]]
    L += ["", "## Endpoint events", "", "| Endpoint | Events | Patients with unresolved candidates |", "|---|---|---|"]
    L += [f"| {k} | {v['events']:,} | {v['unknown_any']:,} |" for k, v in s["endpoint_counts"].items()]
    return "\n".join(L) + "\n"
