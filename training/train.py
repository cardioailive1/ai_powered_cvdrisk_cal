"""Train the 5 CVD calculators (every endpoint × 30d/1y/5y) and write a model bundle.

Run inside the Mayo Clinic Platform_Discover workspace on an APPROVED curation
release (auto-labelled, audited, data-scientist reviewed, cardiologist
adjudicated, signed off — see curation/):

    python -m training.train --release data/releases/REL-... --out bundles/mcp-2026-10

Training with provenance mayo-clinic-platform is refused without a release whose
dataset hash matches its signed release.json.

or locally on synthetic data for an end-to-end test:

    python -m training.synthetic --out data/synthetic.parquet
    python -m training.train --data data/synthetic.parquet --provenance synthetic-demo \
        --min-events 25 --out models/synthetic-demo

Labels per horizon H:
    positive  = first confirmed event within H days of index
    negative  = no event within H AND (followed >= H days OR died before H)   [death = competing event]
    excluded  = censored before H without an event, OR an unresolved candidate
                (indeterminate / unadjudicated uncertain) within H and no confirmed event
Model: HistGradientBoosting (handles missing labs natively) + isotonic/sigmoid
calibration (3-fold). Evaluated on a 20% patient-level hold-out that is never
used for fitting or calibration.

The bundle contains ONLY model weights and aggregate metrics (cell counts <11
suppressed) so it can be submitted for Mayo Clinic Platform export review.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardioai_core.equations import charge_af, pooled_cohort_equations  # noqa: E402
from cardioai_core.schema import CALCULATORS, HORIZONS, MODEL_FEATURES, build_feature_row  # noqa: E402

log = logging.getLogger("cardioai.train")
SMALL_CELL = 11  # suppress counts below this in exported metrics


def labels(df: pd.DataFrame, endpoint: str, horizon_days: int) -> pd.Series:
    t = df[f"days_{endpoint}"]
    pos = t.notna() & (t > 0) & (t <= horizon_days)
    died = df["death_days"].notna() & (df["death_days"] <= horizon_days)
    neg = ~pos & ((df["followup_days"] >= horizon_days) | died)
    unk_col = f"unknown_{endpoint}"
    if unk_col in df:
        neg &= ~(df[unk_col].notna() & (df[unk_col] <= horizon_days))
    y = pd.Series(np.nan, index=df.index)
    y[pos] = 1
    y[neg] = 0
    return y


def _cnt(x: int) -> int | str:
    return x if x >= SMALL_CELL else f"<{SMALL_CELL}"


def auc_ci(y: np.ndarray, p: np.ndarray, reps: int = 200, seed: int = 0) -> tuple[float, float, float]:
    auc = roc_auc_score(y, p)
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(reps):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() != y[i].max():
            boots.append(roc_auc_score(y[i], p[i]))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(auc), float(lo), float(hi)


def calibration(y: np.ndarray, p: np.ndarray) -> dict:
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    logit = np.log(pc / (1 - pc)).reshape(-1, 1)
    lr = LogisticRegression(C=1e6, max_iter=1000).fit(logit, y)
    return {"slope": float(lr.coef_[0, 0]), "intercept": float(lr.intercept_[0]),
            "expected_observed": float(p.mean() / max(y.mean(), 1e-9)), "brier": float(brier_score_loss(y, p))}


def deciles(y: np.ndarray, p: np.ndarray) -> list[dict]:
    q = pd.qcut(pd.Series(p).rank(method="first"), 10, labels=False)
    out = []
    for d in range(10):
        m = (q == d).to_numpy()
        out.append({"decile": d + 1, "mean_predicted": float(p[m].mean()), "observed": float(y[m].mean()), "n": int(m.sum())})
    return out


def subgroup_auc(meta: pd.DataFrame, y: np.ndarray, p: np.ndarray) -> list[dict]:
    rows = []
    for col in ["sex", "race"]:
        for g in sorted(meta[col].dropna().unique()):
            m = (meta[col] == g).to_numpy()
            ev = int(y[m].sum())
            if m.sum() >= 100 and ev >= SMALL_CELL and ev < m.sum():
                a, lo, hi = auc_ci(y[m], p[m], reps=100)
                rows.append({"group": f"{col}={g}", "n": int(m.sum()), "events": ev, "auc": a, "ci": [lo, hi]})
            else:
                rows.append({"group": f"{col}={g}", "n": _cnt(int(m.sum())), "events": _cnt(ev), "auc": None,
                             "note": "too few events for a stable estimate"})
    return rows


def make_model(n_pos: int, seed: int) -> CalibratedClassifierCV:
    hgb = HistGradientBoostingClassifier(learning_rate=0.05, max_iter=400, max_leaf_nodes=15, min_samples_leaf=40,
                                         l2_regularization=1.0, early_stopping=True, validation_fraction=0.15,
                                         n_iter_no_change=30, random_state=seed)
    return CalibratedClassifierCV(hgb, method="isotonic" if n_pos >= 1000 else "sigmoid", cv=3)


def equation_comparator(endpoint: str, raw: pd.DataFrame) -> tuple[str, np.ndarray] | None:
    fn = {"ami": pooled_cohort_equations, "af": charge_af}.get(endpoint)
    if fn is None:
        return None
    vals = []
    for rec in raw.to_dict("records"):
        res = fn(rec)
        vals.append(res["risk"] if res.get("applicable") else np.nan)
    return fn({})["name"], np.array(vals, dtype=float)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", help="approved curation release directory (required for Mayo Clinic Platform data)")
    ap.add_argument("--data", help="ungoverned parquet/csv — synthetic/other provenance only")
    ap.add_argument("--out", required=True, help="output bundle directory")
    ap.add_argument("--provenance", choices=["mayo-clinic-platform", "synthetic-demo", "other"])
    ap.add_argument("--data-description", default="")
    ap.add_argument("--min-events", type=int, default=100, help="min training events to fit a model")
    ap.add_argument("--test-size", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    governance_info: dict = {"status": "ungoverned", "note": "Labels taken directly from the input file; no adjudication."}
    if a.release:
        rel_dir = Path(a.release)
        rel = json.loads((rel_dir / "release.json").read_text())
        from cardioai_core.security import sha256_file
        if sha256_file(rel_dir / "dataset.parquet") != rel["dataset_sha256"]:
            sys.exit("Release dataset hash does not match release.json — refusing to train.")
        if not all(g["status"] in ("pass", "waived") for g in rel["gates"]):
            sys.exit("Release has gates that did not pass — refusing to train.")
        if a.provenance and a.provenance != rel["provenance"]:
            sys.exit(f"--provenance {a.provenance} conflicts with release provenance {rel['provenance']}")
        a.provenance = rel["provenance"]
        raw = pd.read_parquet(rel_dir / "dataset.parquet")
        fams = rel["adjudication"]["families"]
        governance_info = {
            "status": "simulated" if rel.get("simulated_adjudication") else "adjudicated",
            "release_id": rel["release_id"], "label_run_id": rel["label_run_id"], "dataset_sha256": rel["dataset_sha256"],
            "label_model": rel["label_model"], "approvals": [{k: x[k] for k in ("stage", "user", "role", "at")} for x in rel["approvals"]],
            "adjudicated_cases": rel["adjudication"]["totals"]["final"],
            "families": {f: {k: x.get(k) for k in ("cases", "kappa", "ppv_auto_confirmed", "npv_auto_rejected")} for f, x in fams.items()},
            "simulated_adjudication": bool(rel.get("simulated_adjudication")), "synthetic_data": bool(rel.get("synthetic_data")),
        }
    else:
        if not a.data or not a.provenance:
            sys.exit("Give --release, or --data with --provenance")
        if a.provenance == "mayo-clinic-platform":
            sys.exit("Mayo Clinic Platform data must be trained from an approved curation release (--release).")
        raw = pd.read_parquet(a.data) if a.data.endswith(".parquet") else pd.read_csv(a.data)
    log.info("Loaded %d rows", len(raw))
    X = pd.DataFrame([build_feature_row(r) for r in raw.to_dict("records")], columns=MODEL_FEATURES).astype(float)

    pid = raw["person_id"].to_numpy()
    train_ids, test_ids = train_test_split(np.unique(pid), test_size=a.test_size, random_state=a.seed)
    is_train = np.isin(pid, train_ids)

    # Reference values for explanations (medians for continuous, mode for binary) — aggregate only
    Xtr = X[is_train]
    reference = {}
    for f in MODEL_FEATURES:
        col = Xtr[f].dropna()
        if col.empty:
            reference[f] = None
        elif set(col.unique()) <= {0.0, 1.0}:
            reference[f] = float(col.mode().iloc[0])
        else:
            reference[f] = float(col.median())

    out = Path(a.out)
    (out / "models").mkdir(parents=True, exist_ok=True)
    manifest_models: dict = {}
    skipped: list[dict] = []

    for calc_id, calc in CALCULATORS.items():
        excl = calc.get("exclude_if")
        eligible = (X[excl] == 0).to_numpy() if excl else np.ones(len(X), bool)
        for endpoint in calc["endpoints"]:
            key_seen = [k for k in manifest_models if k.startswith(endpoint + "__")]
            if key_seen:
                continue
            for h, hdays in HORIZONS.items():
                key = f"{endpoint}__{h}"
                y = labels(raw, endpoint, hdays).to_numpy()
                m = ~np.isnan(y) & eligible
                tr, te = m & is_train, m & ~is_train
                n_pos_tr, n_pos_te = int(y[tr].sum()), int(y[te].sum())
                if n_pos_tr < a.min_events or n_pos_te < SMALL_CELL:
                    log.warning("Skip %s: %d train / %d test events", key, n_pos_tr, n_pos_te)
                    skipped.append({"model": key, "reason": f"insufficient events (train {_cnt(n_pos_tr)}, test {_cnt(n_pos_te)})"})
                    continue
                log.info("Fitting %s: train n=%d events=%d", key, tr.sum(), n_pos_tr)
                model = make_model(n_pos_tr, a.seed).fit(X[tr], y[tr].astype(int))
                yt, pt = y[te].astype(int), model.predict_proba(X[te])[:, 1]
                auc, lo, hi = auc_ci(yt, pt)

                lr = make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
                                   LogisticRegression(max_iter=2000)).fit(X[tr], y[tr].astype(int))
                lr_auc = float(roc_auc_score(yt, lr.predict_proba(X[te])[:, 1]))

                comparator = None
                eq = equation_comparator(endpoint, raw[te]) if h == "5y" else None
                if eq is not None:
                    name, er = eq
                    ok = ~np.isnan(er)
                    if ok.sum() >= 100 and yt[ok].sum() >= SMALL_CELL:
                        comparator = {"equation": name, "n": int(ok.sum()),
                                      "equation_auc": float(roc_auc_score(yt[ok], er[ok])),
                                      "ai_auc_same_patients": float(roc_auc_score(yt[ok], pt[ok]))}

                fname = f"models/{key}.joblib"
                joblib.dump(model, out / fname, compress=3)
                manifest_models[key] = {
                    "file": fname, "calculator": calc_id, "endpoint": endpoint, "horizon": h,
                    "metrics": {
                        "auc": auc, "auc_ci": [lo, hi], "target_auc": calc["target_cstat"],
                        "meets_target": bool(lo >= calc["target_cstat"]),
                        "n_train": int(tr.sum()), "events_train": _cnt(n_pos_tr),
                        "n_test": int(te.sum()), "events_test": _cnt(n_pos_te),
                        "event_rate_test": float(yt.mean()),
                        "calibration": calibration(yt, pt), "deciles": deciles(yt, pt),
                        "logistic_regression_auc": lr_auc,
                        "subgroups": subgroup_auc(raw.loc[te, ["sex", "race"]], yt, pt),
                        "equation_comparison": comparator,
                    },
                }
                log.info("  %s AUC %.3f (%.3f-%.3f) | LR %.3f", key, auc, lo, hi, lr_auc)

    manifest = {
        "bundle_id": out.name,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "provenance": a.provenance,
        "data_description": a.data_description or (
            "SYNTHETIC data — not derived from patients. For pipeline testing only." if a.provenance == "synthetic-demo" else ""),
        "release_id": governance_info.get("release_id"),
        "n_patients": int(len(np.unique(pid))),
        "sklearn_version": sklearn.__version__,
        "feature_names": MODEL_FEATURES,
        "reference_values": reference,
        "label_definition": ("first confirmed event within horizon; death before horizon without event = negative; "
                             "censored or unresolved candidate within horizon = excluded"),
        "label_governance": governance_info,
        "models": manifest_models,
        "skipped": skipped,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=lambda o: None if isinstance(o, float) and math.isnan(o) else o))
    write_report(out, manifest)
    log.info("Bundle written to %s (%d models, %d skipped)", out, len(manifest_models), len(skipped))


def write_report(out: Path, man: dict) -> None:
    L = [f"# Validation report — {man['bundle_id']}", "",
         f"- Provenance: **{man['provenance']}**", f"- Created: {man['created_utc']}",
         f"- Patients: {man['n_patients']:,}", f"- scikit-learn: {man['sklearn_version']}",
         f"- Labels: {man['label_definition']}", ""]
    if man["provenance"] != "mayo-clinic-platform":
        L += ["> **Not for clinical use.** " + (man["data_description"] or "Not trained on Mayo Clinic Platform data."), ""]
    lg = man.get("label_governance", {})
    L += [f"- Label governance: **{lg.get('status')}**" + (f" — release {lg['release_id']}, {lg['adjudicated_cases']} adjudicated cases"
                                                           if lg.get("release_id") else ""), ""]
    if lg.get("simulated_adjudication"):
        L += ["> Adjudication was **simulated** (synthetic readers), not performed by cardiologists.", ""]
    L += ["| Model | Hold-out C-statistic (95% CI) | Target | Meets target* | LR baseline | Cal. slope | E/O | Test n / events |",
          "|---|---|---|---|---|---|---|---|"]
    for k, v in man["models"].items():
        m = v["metrics"]
        c = m["calibration"]
        L.append(f"| {k} | {m['auc']:.3f} ({m['auc_ci'][0]:.3f}-{m['auc_ci'][1]:.3f}) | ≥{m['target_auc']:.2f} | "
                 f"{'Yes' if m['meets_target'] else 'No'} | {m['logistic_regression_auc']:.3f} | {c['slope']:.2f} | "
                 f"{c['expected_observed']:.2f} | {m['n_test']:,} / {m['events_test']} |")
    L += ["", "*Target met only if the lower bound of the 95% CI is at or above the target.", ""]
    comps = [(k, v["metrics"]["equation_comparison"]) for k, v in man["models"].items() if v["metrics"]["equation_comparison"]]
    if comps:
        L += ["## AI vs published equation (same hold-out patients)", "",
              "| Model | Equation | n | Equation C-stat | AI C-stat |", "|---|---|---|---|---|"]
        for k, c in comps:
            L.append(f"| {k} | {c['equation']} | {c['n']:,} | {c['equation_auc']:.3f} | {c['ai_auc_same_patients']:.3f} |")
        L.append("")
    L += ["## Subgroup C-statistics (5-year models)", ""]
    for k, v in man["models"].items():
        if not k.endswith("__5y"):
            continue
        L.append(f"**{k}**: " + "; ".join(
            f"{s['group']} {s['auc']:.3f}" if s.get("auc") else f"{s['group']} n/a" for s in v["metrics"]["subgroups"]))
        L.append("")
    if man["skipped"]:
        L += ["## Not trained", ""] + [f"- {s['model']}: {s['reason']}" for s in man["skipped"]]
    (out / "VALIDATION_REPORT.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
