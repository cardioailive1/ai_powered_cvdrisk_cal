"""Curation workbench web server — run INSIDE the Mayo Clinic Platform workspace.

    python -m curation.server --data-dir data --host 127.0.0.1 --port 8090

Open it through the workspace's own browser / port forwarding (e.g. a
JupyterLab server proxy at .../proxy/8090/). All URLs are relative so it works
behind such a proxy. Do not expose it outside the workspace.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import jwt
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from cardioai_core.bundle import nan_to_none
from cardioai_core.security import hash_password, make_token, read_token, verify_password

from . import actions, gates as gates_mod, governance
from .adjudication import AdjudicationError, queue_for, reader_view, submit_read
from .pipeline import plan_workload
from .release import ReleaseError, build_release
from .runs import current, get_run_data, latest_audit
from .store import ROLES, Case, LogEntry, Release, User, check_reader_policy, log_event, open_store, verify_log

STATIC = Path(__file__).parent / "static"
APP_CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "styles.css"


class LoginIn(BaseModel):
    username: str
    password: str


class ReviewIn(BaseModel):
    check_id: str
    action: str
    justification: str = ""


class ApproveIn(BaseModel):
    stage: str
    decision: str
    comment: str = ""


class WaiveIn(BaseModel):
    gate_id: str
    justification: str


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    full_name: str = ""
    email: str = ""
    role: str
    password: str = Field(min_length=12, max_length=200)


def create_app(data_dir: str) -> FastAPI:
    data_dir = str(Path(data_dir).resolve())
    Session = open_store(data_dir)
    sec = Path(data_dir) / ".secret"
    secret = sec.read_text().strip() if sec.exists() else os.environ.get("CURATION_SECRET", "")
    if not secret:
        raise SystemExit(f"No secret found — run `python -m curation.pipeline init --data-dir {data_dir}` first")
    app = FastAPI(title="CardioAI Curation Workbench", docs_url=None, redoc_url=None)
    bearer = HTTPBearer(auto_error=False)

    def db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def user(creds: HTTPAuthorizationCredentials | None = Depends(bearer), s=Depends(db)) -> User:
        if creds is None:
            raise HTTPException(401, "Not authenticated")
        try:
            p = read_token(secret, creds.credentials)
        except jwt.PyJWTError:
            raise HTTPException(401, "Session expired")
        u = s.get(User, int(p["sub"]))
        if not u or not u.active:
            raise HTTPException(401, "Account disabled")
        return u

    def run_or_404(s):
        r = current(s)
        if not r:
            raise HTTPException(404, "No active label run. Run `python -m curation.pipeline label` in the workspace.")
        return r

    def need(u: User, *roles):
        if u.role not in roles:
            raise HTTPException(403, f"Requires role: {', '.join(roles)}")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
                             "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
        return resp

    @app.exception_handler(actions.ActionError)
    async def _ae(_, e):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(AdjudicationError)
    async def _adj(_, e):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(ReleaseError)
    async def _re(_, e):
        return JSONResponse({"detail": str(e)}, status_code=400)

    # ------------------------------------------------------------ auth
    @app.post("/api/auth/login")
    def login(body: LoginIn, s=Depends(db)):
        u = s.scalar(select(User).where(User.username == body.username.strip()))
        if not u or not u.active or not verify_password(body.password, u.password_hash):
            log_event(s, body.username[:80], "auth.failed", "login", {})
            s.commit()
            raise HTTPException(401, "Invalid username or password")
        log_event(s, u.username, "auth.login", "login", {})
        s.commit()
        return {"token": make_token(secret, u.id, u.username, u.role, 600), "username": u.username, "role": u.role,
                "full_name": u.full_name}

    @app.get("/api/me")
    def me(u: User = Depends(user)):
        return {"username": u.username, "role": u.role, "full_name": u.full_name}

    # ------------------------------------------------------------ status / pipeline
    @app.get("/api/status")
    def status(u: User = Depends(user), s=Depends(db)):
        run = current(s)
        if not run:
            return {"run": None}
        ar = latest_audit(s, run.id)
        m = actions.run_metrics(s, data_dir, run)
        g = gates_mod.evaluate(s, data_dir, run, ar.report if ar else None, m)
        rels = [{"id": r.id, "created_at": r.created_at.isoformat(), "created_by": r.created_by, "dataset_sha": r.dataset_sha,
                 "path": r.path, **r.summary} for r in s.scalars(select(Release).where(Release.label_run_id == run.id))]
        approvals = {st: (lambda a: None if not a else {"decision": a.decision, "comment": a.comment,
                                                         "user": s.get(User, a.user_id).username, "at": a.created_at.isoformat()})(
            gates_mod.approval(s, run.id, st)) for st in ("ds_dataset_review", "ds_release", "cardiology_release")}
        gov = governance.load(data_dir)
        return nan_to_none({
            "run": {"id": run.id, "created_at": run.created_at.isoformat(), "created_by": run.created_by,
                    "label_model": run.label_model_version, "summary": run.summary},
            "audit": None if not ar else {"id": ar.id, "overall": ar.overall, "counts": ar.report["counts"], "created_at": ar.created_at.isoformat()},
            "approvals": approvals, "metrics": m, "gates": g, "releasable": gates_mod.releasable(g),
            "ready_for_signoff": gates_mod.ready_for_signoff(g), "releases": rels,
            "governance": {k: gov[k] for k in ("gates", "sampling", "definitions", "label_thresholds", "provenance", "reader_policy")},
            "workload": plan_workload(run.summary, gov, max(1, len([u for u in s.scalars(select(User)) if u.active and u.role == "cardiologist"]))),
        })

    @app.get("/api/audit")
    def audit(u: User = Depends(user), s=Depends(db)):
        run = run_or_404(s)
        ar = latest_audit(s, run.id)
        if not ar:
            return {"report": None}
        import json
        lf = json.loads((Path(run.path) / "lf_stats.json").read_text())
        return nan_to_none({"report": {**ar.report, "id": ar.id}, "items": gates_mod.audit_items(s, run.id, ar.report),
                            "lf_stats": lf, "locked": gates_mod.approval(s, run.id, "ds_dataset_review") is not None})

    @app.post("/api/audit/review")
    def audit_review(body: ReviewIn, u: User = Depends(user), s=Depends(db)):
        actions.review_audit_item(s, run_or_404(s), u, body.check_id, body.action, body.justification)
        return {"ok": True}

    @app.post("/api/approve")
    def approve(body: ApproveIn, u: User = Depends(user), s=Depends(db)):
        return actions.approve(s, data_dir, run_or_404(s), u, body.stage, body.decision, body.comment)

    @app.post("/api/gates/waive")
    def waive(body: WaiveIn, u: User = Depends(user), s=Depends(db)):
        actions.waive_gate(s, data_dir, run_or_404(s), u, body.gate_id, body.justification)
        return {"ok": True}

    @app.post("/api/release")
    def release(u: User = Depends(user), s=Depends(db)):
        need(u, "data_scientist")
        rel = build_release(s, data_dir, run_or_404(s), u)
        return {"id": rel.id, "path": rel.path, "dataset_sha": rel.dataset_sha}

    # ------------------------------------------------------------ adjudication
    @app.get("/api/queue")
    def queue(u: User = Depends(user), s=Depends(db)):
        if not u.can_read_cases:
            return {"open": [], "completed": 0, "disagreements": []}
        return queue_for(s, run_or_404(s).id, u)

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str, u: User = Depends(user), s=Depends(db)):
        need(u, "cardiologist", "lead_cardiologist")
        run = run_or_404(s)
        c = s.get(Case, case_id)
        if not c or c.label_run_id != run.id:
            raise HTTPException(404, "Case not found")
        view = reader_view(s, c, u)
        rd = get_run_data(data_dir, run)
        view["packet"] = rd.annotation_packet(c.ref_id) if c.case_type == "annotation" else rd.event_packet(c.ref_id)
        log_event(s, u.username, "case.viewed", c.id, {})
        s.commit()
        return nan_to_none(view)

    @app.post("/api/cases/{case_id}/read")
    def post_read(case_id: str, decision: dict[str, Any], u: User = Depends(user), s=Depends(db)):
        need(u, "cardiologist", "lead_cardiologist")
        run = run_or_404(s)
        c = s.get(Case, case_id)
        if not c or c.label_run_id != run.id:
            raise HTTPException(404, "Case not found")
        c = submit_read(s, c, u, decision, governance.load(data_dir))
        return {"status": c.status}

    # ------------------------------------------------------------ audit trail & users
    @app.get("/api/log")
    def log(limit: int = 200, u: User = Depends(user), s=Depends(db)):
        need(u, "admin", "data_scientist", "lead_cardiologist")
        rows = s.scalars(select(LogEntry).order_by(desc(LogEntry.id)).limit(min(limit, 1000)))
        return {"verify": verify_log(s), "entries": [{"id": e.id, "ts": e.ts, "user": e.username, "action": e.action, "entity": e.entity,
                                                      "details": e.details, "hash": e.hash[:12]} for e in rows]}

    @app.get("/api/users")
    def users(u: User = Depends(user), s=Depends(db)):
        need(u, "admin")
        pol = governance.load(data_dir)["reader_policy"]
        users = [{"id": x.id, "username": x.username, "full_name": x.full_name, "email": x.email, "role": x.role, "active": x.active}
                 for x in s.scalars(select(User))]
        used = sum(1 for x in users if x["active"] and x["role"] in ("cardiologist", "lead_cardiologist"))
        return {"users": users, "seats_used": used, "seats_available": pol["max_reader_seats"], "email_domain": pol["email_domain"]}

    @app.post("/api/users", status_code=201)
    def add_user(body: UserIn, u: User = Depends(user), s=Depends(db)):
        need(u, "admin")
        if body.role not in ROLES:
            raise HTTPException(422, f"role must be one of {ROLES}")
        if s.scalar(select(User).where(User.username == body.username)):
            raise HTTPException(409, "Username exists")
        try:
            check_reader_policy(s, governance.load(data_dir), body.role, body.email)
        except ValueError as e:
            raise HTTPException(400, str(e))
        s.add(User(username=body.username, full_name=body.full_name, email=body.email, role=body.role,
                   password_hash=hash_password(body.password)))
        log_event(s, u.username, "user.created", body.username, {"role": body.role})
        s.commit()
        return {"ok": True}

    # ------------------------------------------------------------ frontend (relative URLs)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/static-base.css", include_in_schema=False)
    def base_css():
        return FileResponse(APP_CSS, media_type="text/css")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("CURATION_DATA_DIR", "data"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    import uvicorn
    uvicorn.run(create_app(a.data_dir), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
