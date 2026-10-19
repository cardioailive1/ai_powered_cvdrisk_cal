"""Model bundle: the hand-off format between the Mayo Clinic Platform Discover
training workspace and the Render web app.

    <bundle_dir>/
        manifest.json                    provenance, versions, reference values, metrics
        models/<endpoint>__<horizon>.joblib   calibrated classifier per endpoint × horizon

Only trained model weights + aggregate metrics are in a bundle — never
patient-level rows — so it can go through Mayo Clinic Platform's model
export review.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from .schema import CALCULATORS, FEATURE_LABELS, HORIZONS, MODEL_FEATURES, build_feature_row

log = logging.getLogger("cardioai.bundle")


class ModelBundle:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.manifest: dict[str, Any] = json.loads((self.path / "manifest.json").read_text())
        self.reference: dict[str, float] = self.manifest["reference_values"]
        self.models: dict[tuple[str, str], Any] = {}
        for key, info in self.manifest["models"].items():
            endpoint, horizon = key.split("__")
            self.models[(endpoint, horizon)] = joblib.load(self.path / info["file"])
        self._check_versions()
        log.info("Loaded model bundle %s (%d models, provenance=%s)",
                 self.manifest.get("bundle_id"), len(self.models), self.provenance)

    # ------------------------------------------------------------------
    @property
    def provenance(self) -> str:
        return self.manifest.get("provenance", "unknown")

    @property
    def is_demo(self) -> bool:
        return self.provenance != "mayo-clinic-platform"

    def _check_versions(self) -> None:
        import sklearn
        trained = self.manifest.get("sklearn_version")
        if trained and trained != sklearn.__version__:
            log.warning("Bundle trained with scikit-learn %s but runtime has %s — pin the same version.",
                        trained, sklearn.__version__)

    def metrics(self, endpoint: str, horizon: str) -> dict[str, Any] | None:
        return self.manifest["models"].get(f"{endpoint}__{horizon}", {}).get("metrics")

    # ------------------------------------------------------------------
    def _frame(self, rows: list[dict[str, float]]) -> pd.DataFrame:
        return pd.DataFrame(rows, columns=MODEL_FEATURES).astype(float)

    def predict(self, calculator: str, patient: dict[str, Any]) -> dict[str, Any]:
        calc = CALCULATORS[calculator]
        feats = build_feature_row(patient)
        X = self._frame([feats])
        out: dict[str, Any] = {}
        for endpoint in calc["endpoints"]:
            out[endpoint] = {}
            for h in HORIZONS:
                model = self.models.get((endpoint, h))
                if model is None:
                    continue
                out[endpoint][h] = float(model.predict_proba(X)[0, 1])
            # Cumulative risk cannot decrease with a longer horizon
            prev = 0.0
            for h in HORIZONS:
                if h in out[endpoint]:
                    out[endpoint][h] = max(out[endpoint][h], prev)
                    prev = out[endpoint][h]
        return {"features": feats, "probabilities": out}

    def explain(self, endpoint: str, horizon: str, feats: dict[str, float], top: int = 8) -> list[dict[str, Any]]:
        """Per-patient contributions: change in predicted risk when each feature
        is replaced by the cohort reference value (median / mode)."""
        model = self.models.get((endpoint, horizon))
        if model is None:
            return []
        rows = [feats]
        names: list[str] = []
        for f in MODEL_FEATURES:
            ref = self.reference.get(f, math.nan)
            cur = feats[f]
            if (isinstance(cur, float) and math.isnan(cur)) or cur == ref:
                continue
            r = dict(feats)
            r[f] = ref
            rows.append(r)
            names.append(f)
        if not names:
            return []
        p = model.predict_proba(self._frame(rows))[:, 1]
        base = p[0]
        contribs = [
            {"feature": f, "label": FEATURE_LABELS.get(f, f), "value": feats[f],
             "reference": self.reference.get(f), "delta": float(base - p[i + 1])}
            for i, f in enumerate(names)
        ]
        contribs.sort(key=lambda c: abs(c["delta"]), reverse=True)
        return [c for c in contribs[:top] if abs(c["delta"]) >= 1e-4]


def load_bundle(path: str | Path | None) -> ModelBundle | None:
    if not path:
        return None
    p = Path(path)
    if not (p / "manifest.json").exists():
        log.warning("No model bundle at %s", p)
        return None
    return ModelBundle(p)


def nan_to_none(obj: Any) -> Any:
    """Make dicts JSON-safe (NaN -> None)."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, (np.floating,)):
        return nan_to_none(float(obj))
    if isinstance(obj, dict):
        return {k: nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [nan_to_none(v) for v in obj]
    return obj
