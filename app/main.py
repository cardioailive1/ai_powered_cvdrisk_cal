"""CardioAI CVD Risk API + frontend (FastAPI). Start: uvicorn app.main:app"""
from __future__ import annotations

import logging
import tarfile
import tempfile
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cardioai_core.bundle import ModelBundle, load_bundle, nan_to_none
from cardioai_core.equations import run_equations
from cardioai_core.schema import CALCULATORS, FIELDS, HORIZONS, SECTIONS, validate_input

from . import recommendations
from .auth import create_token, current_user, hash_password, require_admin, throttle_login, verify_password
from .config import ROOT, settings
from .db import Assessment, SessionLocal, User, get_db, init_db

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
    if not (settings.admin_username and settings.admin_password):
        return
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == settings.admin_username)) is None:
            db.add(User(username=settings.admin_username, password_hash=hash_password(settings.admin_password), role="admin"))
            db.commit()
            log.info("Created admin user '%s'", settings.admin_username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _bootstrap_admin()
    model_dir = Path(settings.model_dir)
    if settings.model_bundle_url and not (model_dir / "manifest.json").exists():
        try:
            _fetch_bundle(settings.model_bundle_url, model_dir)
        except Exception:
            log.exception("Could not download model bundle")
    app.state.bundle = load_bundle(settings.model_dir)
    yield


app = FastAPI(title="CardioAI CVD Risk API", version="2.0.0", lifespan=lifespan)
if settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store") if request.url.path.startswith("/api") else resp.headers.get("Cache-Control", "no-cache")
    return resp


def get_bundle(request: Request) -> ModelBundle | None:
    return request.app.state.bundle


# ------------------------------------------------------------------ schemas
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class PredictIn(BaseModel):
    calculator: str
    patient: dict[str, Any]


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=10, max_length=200)
    role: str = Field(default="clinician", pattern="^(clinician|admin)$")


# ------------------------------------------------------------------ routes
@app.get("/healthz")
def healthz(request: Request):
    b = get_bundle(request)
    return {"status": "ok", "models_loaded": bool(b), "bundle": b.manifest.get("bundle_id") if b else None}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    throttle_login(request)
    user = db.scalar(select(User).where(User.username == body.username.strip()))
    if not user or not user.active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password. Please try again.")
    return {"token": create_token(user), "username": user.username, "role": user.role}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role}


@app.get("/api/meta")
def meta(request: Request, user: User = Depends(current_user)):
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
def model_cards(request: Request, user: User = Depends(current_user)):
    b = get_bundle(request)
    if not b:
        raise HTTPException(503, "No model bundle loaded")
    m = {k: {kk: vv for kk, vv in v.items() if kk != "file"} for k, v in b.manifest["models"].items()}
    return nan_to_none({"bundle_id": b.manifest.get("bundle_id"), "provenance": b.provenance, "models": m,
                        "skipped": b.manifest.get("skipped", [])})


@app.post("/api/predict")
def predict(body: PredictIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
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
    return result


@app.get("/api/assessments")
def assessments(limit: int = 20, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Assessment).where(Assessment.user_id == user.id).order_by(Assessment.created_at.desc()).limit(min(limit, 100))
    return [{"id": a.id, "calculator": a.calculator, "created_at": a.created_at.isoformat(),
             "primary": (a.results or {}).get("primary"), "inputs": a.inputs} for a in db.scalars(q)]


@app.get("/api/users")
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [{"id": u.id, "username": u.username, "role": u.role, "active": u.active} for u in db.scalars(select(User))]


@app.post("/api/users", status_code=201)
def create_user(body: UserIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "Username already exists")
    u = User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    db.add(u)
    db.commit()
    return {"id": u.id, "username": u.username, "role": u.role}


@app.patch("/api/users/{user_id}")
def set_active(user_id: int, active: bool, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Not found")
    u.active = active
    db.commit()
    return {"id": u.id, "active": u.active}


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    detail = exc.detail
    body = detail if isinstance(detail, dict) else {"detail": detail}
    return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))


# ------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
