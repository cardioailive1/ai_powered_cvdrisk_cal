"""CardioAI CVD Risk API + frontend (FastAPI). Start: uvicorn app.main:app"""
from __future__ import annotations

import logging
import tarfile
import tempfile
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import datetime as dt

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cardioai_core.bundle import ModelBundle, load_bundle, nan_to_none
from cardioai_core.equations import run_equations
from cardioai_core.schema import CALCULATORS, FIELDS, HORIZONS, SECTIONS, validate_input

from . import audit, recommendations, routes_admin, routes_auth
from .auth import Ctx, hash_password, require
from .config import settings
from .db import Assessment, SessionLocal, User, get_db, init_db, utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cardioai")
STATIC = Path(__file__).parent / "static"


def _fetch_bundle(url: str, dest: Path) -> None:
    """Download and unpack a model bundle .tar.gz (e.g. from private object storage)."""
    log.info("Downloading model bundle from %s", url.split("?")[0])
    req = urllib.request.Request(url)
    if settings.model_bundle_token:
        req.add_header("Authorization", f"Bearer {settings.model_bundle_token}")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        with urllib.request.urlopen(req, timeout=120) as r:
            tmp.write(r.read())
        tmp.flush()
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tmp.name) as tar:
            tar.extractall(dest, filter="data")
    # allow archives that contain a single top-level folder
    if not (dest / "manifest.json").exists():
        subs = [d for d in dest.iterdir() if (d / "manifest.json").exists()]
        if subs:
            settings.model_dir = str(subs[0])


def _bootstrap_admin() -> None:
    """Create the first administrator from ADMIN_USERNAME / ADMIN_PASSWORD once. An existing account is never
    modified, so the env password stops mattering after the admin changes it. MFA enrolment is forced at first
    sign-in (REQUIRE_MFA)."""
    if not (settings.admin_username and settings.admin_password):
        return
    if len(settings.admin_password) < settings.password_min_length:
        log.error("ADMIN_PASSWORD is shorter than %s characters; first administrator not created", settings.password_min_length)
        return
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == settings.admin_username)) is None:
            u = User(username=settings.admin_username, full_name="Administrator", password_hash=hash_password(settings.admin_password),
                     role="admin", status="active", approved_at=utcnow())
            db.add(u)
            db.commit()
            audit.record("users.bootstrap_admin", actor_label="system", target_type="user", target_id=u.id)
            log.info("Created the first administrator account (id %s)", u.id)


def _purge_old_assessments() -> None:
    """Data retention: delete stored assessments older than ASSESSMENT_RETENTION_DAYS (0 keeps them)."""
    if settings.assessment_retention_days <= 0:
        return
    cutoff = utcnow() - dt.timedelta(days=settings.assessment_retention_days)
    with SessionLocal() as db:
        n = db.execute(delete(Assessment).where(Assessment.created_at < cutoff)).rowcount
        db.commit()
    if n:
        audit.record("retention.assessments_purged", actor_label="system", count=n, retention_days=settings.assessment_retention_days)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _bootstrap_admin()
    _purge_old_assessments()
    model_dir = Path(settings.model_dir)
    if settings.model_bundle_url and not (model_dir / "manifest.json").exists():
        try:
            _fetch_bundle(settings.model_bundle_url, model_dir)
        except Exception:
            log.exception("Could not download model bundle")
    app.state.bundle = load_bundle(settings.model_dir)
    yield


app = FastAPI(title="CardioAI CVD Risk API", version="3.0.0", lifespan=lifespan,
              docs_url=None if settings.production else "/docs", redoc_url=None,
              openapi_url=None if settings.production else "/openapi.json")
if settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token"])
app.include_router(routes_auth.router)
app.include_router(routes_admin.router)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "img-src 'self' data:; connect-src 'self'; font-src 'self' https://fonts.gstatic.com; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if settings.cookie_secure:
        h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    # nothing that may hold clinical data is cached by browsers or proxies
    h["Cache-Control"] = "no-store" if request.url.path.startswith("/api") or request.url.path == "/" else h.get("Cache-Control", "no-cache")
    return resp


def get_bundle(request: Request) -> ModelBundle | None:
    return request.app.state.bundle


# ------------------------------------------------------------------ schemas
class PredictIn(BaseModel):
    calculator: str
    patient: dict[str, Any]


# ------------------------------------------------------------------ routes
@app.get("/healthz")
def healthz(request: Request):
    b = get_bundle(request)
    return {"status": "ok", "models_loaded": bool(b), "bundle": b.manifest.get("bundle_id") if b else None}


@app.get("/api/meta")
def meta(request: Request, ctx: Ctx = Depends(require("models:read"))):
    b = get_bundle(request)
    calcs = {}
    for cid, c in CALCULATORS.items():
        perf = None
        if b:
            m = b.metrics(c["primary_endpoint"], "5y")
            perf = {"auc": m["auc"], "auc_ci": m["auc_ci"], "meets_target": m["meets_target"]} if m else None
        calcs[cid] = {**{k: v for k, v in c.items()}, "performance_5y": perf}
    return nan_to_none({
        "calculators": calcs, "fields": FIELDS, "sections": SECTIONS, "horizons": list(HORIZONS),
        "model": None if not b else {
            "bundle_id": b.manifest.get("bundle_id"), "provenance": b.provenance, "is_demo": b.is_demo,
            "created_utc": b.manifest.get("created_utc"), "n_patients": b.manifest.get("n_patients"),
            "data_description": b.manifest.get("data_description"),
            "label_governance": b.manifest.get("label_governance") or {"status": "ungoverned"},
        },
    })


@app.get("/api/models")
def model_cards(request: Request, ctx: Ctx = Depends(require("models:read"))):
    b = get_bundle(request)
    if not b:
        raise HTTPException(503, "No model bundle loaded")
    m = {k: {kk: vv for kk, vv in v.items() if kk != "file"} for k, v in b.manifest["models"].items()}
    return nan_to_none({"bundle_id": b.manifest.get("bundle_id"), "provenance": b.provenance, "models": m,
                        "skipped": b.manifest.get("skipped", [])})


@app.post("/api/predict")
def predict(body: PredictIn, request: Request, ctx: Ctx = Depends(require("predict")), db: Session = Depends(get_db)):
    user = ctx.user
    if body.calculator not in CALCULATORS:
        raise HTTPException(422, f"Unknown calculator '{body.calculator}'")
    p = body.patient
    errors = validate_input(p)
    if errors:
        raise HTTPException(422, {"errors": errors})
    calc = CALCULATORS[body.calculator]
    b = get_bundle(request)
    warnings: list[str] = []
    equations = run_equations(body.calculator, p)

    result: dict[str, Any] = {
        "calculator": body.calculator, "calculator_name": calc["name"], "equations": equations,
        "applicable": True, "endpoints": [], "primary": None, "contributions": [], "warnings": warnings,
        "model": None,
    }
    excl = calc.get("exclude_if")
    if excl and str(p.get(excl)).lower() in {"1", "true", "on", "yes"}:
        result["applicable"] = False
        result["not_applicable_reason"] = calc["exclude_reason"]
    elif b is None:
        warnings.append("No AI model bundle is loaded on the server; showing published equations only.")
    elif b.is_demo and not settings.allow_demo_models:
        warnings.append("Only demo models are installed and demo models are disabled on this server.")
    else:
        pred = b.predict(body.calculator, p)
        result["model"] = {"bundle_id": b.manifest.get("bundle_id"), "provenance": b.provenance, "is_demo": b.is_demo}
        if b.is_demo:
            warnings.append("DEMO MODELS trained on synthetic data — numbers are illustrative and NOT for clinical use.")
        for ep, label in calc["endpoints"].items():
            hz = {}
            for h, risk in pred["probabilities"].get(ep, {}).items():
                mt = b.metrics(ep, h) or {}
                hz[h] = {"risk": risk, "auc": mt.get("auc"), "auc_ci": mt.get("auc_ci"), "meets_target": mt.get("meets_target"),
                         "calibration_slope": (mt.get("calibration") or {}).get("slope")}
            result["endpoints"].append({"id": ep, "label": label, "horizons": hz})
        pe = calc["primary_endpoint"]
        prim_h = next((h for h in ("5y", "1y", "30d") if h in pred["probabilities"].get(pe, {})), None)
        if prim_h:
            risk = pred["probabilities"][pe][prim_h]
            cat, cls = recommendations.category(risk)
            result["primary"] = {"endpoint": pe, "label": calc["endpoints"][pe], "horizon": prim_h, "risk": risk,
                                 "category": cat, "category_class": cls}
            result["contributions"] = b.explain(pe, prim_h, pred["features"])
        else:
            warnings.append("No trained model is available for this calculator's primary endpoint.")
        missing = [f["label"] for f in FIELDS if f["section"] in {"labs", "imaging"} and p.get(f["key"]) in (None, "")]
        if missing:
            warnings.append("Not provided (model handled as missing): " + ", ".join(missing) + ".")

    prim = result["primary"]["risk"] if result["primary"] else None
    result["recommendations"] = recommendations.build(body.calculator, p, prim, equations)
    result = nan_to_none(result)

    if settings.store_assessments:
        a = Assessment(user_id=user.id, calculator=body.calculator, inputs=p, results=result,
                       model_bundle=(b.manifest.get("bundle_id") if b else "none"))
        db.add(a)
        db.commit()
        result["assessment_id"] = a.id
    audit.record("phi.assessment_created", request=request, actor=user, target_type="assessment",
                 target_id=result.get("assessment_id", ""), calculator=body.calculator, stored=settings.store_assessments)
    return result


@app.get("/api/assessments")
def assessments(request: Request, limit: int = 20, ctx: Ctx = Depends(require("assessments:own")), db: Session = Depends(get_db)):
    q = select(Assessment).where(Assessment.user_id == ctx.user.id).order_by(Assessment.created_at.desc()).limit(max(1, min(limit, 100)))
    rows = list(db.scalars(q))
    audit.record("phi.assessments_viewed", request=request, actor=ctx.user, target_type="assessment",
                 target_id=",".join(str(a.id) for a in rows)[:64], count=len(rows))
    return [{"id": a.id, "calculator": a.calculator, "created_at": a.created_at.isoformat(),
             "primary": (a.results or {}).get("primary"), "inputs": a.inputs} for a in rows]


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    detail = exc.detail
    body = detail if isinstance(detail, dict) else {"detail": detail}
    return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_exc(request: Request, exc: RequestValidationError):
    # Don't echo submitted values back (they may be passwords or clinical data)
    errs = [f"{'.'.join(str(x) for x in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors()]
    return JSONResponse({"detail": "Invalid request", "errors": errs}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_exc(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal error"}, status_code=500)


# ------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
