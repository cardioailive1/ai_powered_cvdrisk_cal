"""Administration (users:read / users:manage) and audit log (audit:read) routes."""
from __future__ import annotations

import csv
import io
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, rbac
from .auth import EMAIL_RE, Ctx, hash_password, normalize_email, require, revoke_user_sessions
from .db import AuditEvent, AuthSession, User, aware, get_db, utcnow

router = APIRouter(prefix="/api")


def _iso(t):
    return aware(t).isoformat() if t else None


def user_row(u: User, active_sessions: int = 0) -> dict:
    return {"id": u.id, "email": u.username, "full_name": u.full_name, "organization": u.organization, "role": u.role,
            "status": u.status, "mfa_enabled": u.mfa_enabled, "locked": bool(u.locked_until and aware(u.locked_until) > utcnow()),
            "must_change_password": u.must_change_password, "created_at": _iso(u.created_at),
            "last_login_at": _iso(u.last_login_at), "approved_at": _iso(u.approved_at), "active_sessions": active_sessions}


def _get(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "User not found")
    return u


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.status == "active"))


def _temp_password() -> str:
    return secrets.token_urlsafe(12) + "-" + secrets.token_hex(2)


# ------------------------------------------------------------------ users
@router.get("/admin/roles")
def roles(ctx: Ctx = Depends(require("users:read"))):
    return {"roles": rbac.ROLES, "permissions": rbac.PERMISSIONS, "statuses": rbac.STATUSES}


@router.get("/admin/users")
def list_users(request: Request, status: str | None = None, ctx: Ctx = Depends(require("users:read")),
               db: Session = Depends(get_db)):
    q = select(User).order_by(User.status != "pending", User.created_at.desc())
    if status:
        q = q.where(User.status == status)
    now = utcnow()
    counts = dict(db.execute(select(AuthSession.user_id, func.count()).where(
        AuthSession.revoked_at.is_(None), AuthSession.expires_at > now).group_by(AuthSession.user_id)).all())
    audit.record("users.list", request=request, actor=ctx.user, status_filter=status or "all")
    return [user_row(u, counts.get(u.id, 0)) for u in db.scalars(q)]


class CreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    full_name: str = Field(min_length=2, max_length=120)
    organization: str = Field(default="", max_length=160)
    role: str = Field(default="clinician")


@router.post("/admin/users", status_code=201)
def create_user(body: CreateIn, request: Request, ctx: Ctx = Depends(require("users:manage")), db: Session = Depends(get_db)):
    email = normalize_email(body.email)
    if not EMAIL_RE.match(email):
        raise HTTPException(422, {"errors": ["Enter a valid email address."]})
    if body.role not in rbac.ROLES:
        raise HTTPException(422, {"errors": ["Unknown role."]})
    if db.scalar(select(User).where(User.username == email)):
        raise HTTPException(409, "An account with this email already exists")
    temp = _temp_password()
    u = User(username=email, full_name=body.full_name.strip(), organization=body.organization.strip(),
             password_hash=hash_password(temp), role=body.role, status="active", must_change_password=True,
             approved_by_id=ctx.user.id, approved_at=utcnow())
    db.add(u)
    db.commit()
    audit.record("users.created", request=request, actor=ctx.user, target_type="user", target_id=u.id, role=u.role)
    return {"user": user_row(u), "temporary_password": temp,
            "note": "Give this one-time password to the user through a secure channel. They must change it and set up MFA at first sign-in."}


class ApproveIn(BaseModel):
    role: str = "clinician"


@router.post("/admin/users/{user_id}/approve")
def approve(user_id: int, body: ApproveIn, request: Request, ctx: Ctx = Depends(require("users:manage")),
            db: Session = Depends(get_db)):
    u = _get(db, user_id)
    if u.status != "pending":
        raise HTTPException(409, "Only pending requests can be approved")
    if body.role not in rbac.ROLES:
        raise HTTPException(422, {"errors": ["Unknown role."]})
    u.status, u.role, u.approved_by_id, u.approved_at = "active", body.role, ctx.user.id, utcnow()
    db.commit()
    audit.record("users.approved", request=request, actor=ctx.user, target_type="user", target_id=u.id, role=u.role)
    return user_row(u)


@router.post("/admin/users/{user_id}/reject")
def reject(user_id: int, request: Request, ctx: Ctx = Depends(require("users:manage")), db: Session = Depends(get_db)):
    u = _get(db, user_id)
    if u.status != "pending":
        raise HTTPException(409, "Only pending requests can be rejected")
    u.status = "rejected"
    db.commit()
    audit.record("users.rejected", request=request, actor=ctx.user, target_type="user", target_id=u.id)
    return user_row(u)


class UpdateIn(BaseModel):
    role: str | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


@router.patch("/admin/users/{user_id}")
def update_user(user_id: int, body: UpdateIn, request: Request, ctx: Ctx = Depends(require("users:manage")),
                db: Session = Depends(get_db)):
    u = _get(db, user_id)
    if u.id == ctx.user.id:
        raise HTTPException(409, "You cannot change your own role or status — ask another administrator")
    if u.status in ("pending", "rejected") and body.status:
        raise HTTPException(409, "Approve or reject the request first")
    changes = {}
    if body.role is not None and body.role != u.role:
        if body.role not in rbac.ROLES:
            raise HTTPException(422, {"errors": ["Unknown role."]})
        changes["role"] = [u.role, body.role]
    if body.status is not None and body.status != u.status:
        changes["status"] = [u.status, body.status]
    losing_admin = u.role == "admin" and u.status == "active" and (
        changes.get("role", [None, "admin"])[1] != "admin" or changes.get("status", [None, "active"])[1] != "active")
    if losing_admin and _active_admins(db) <= 1:
        raise HTTPException(409, "At least one active administrator must remain")
    if not changes:
        return user_row(u)
    if "role" in changes:
        u.role = body.role
    if "status" in changes:
        u.status = body.status
    db.commit()
    revoke_user_sessions(db, u.id)  # new privileges take effect at next sign-in; disabled users are signed out now
    audit.record("users.updated", request=request, actor=ctx.user, target_type="user", target_id=u.id, changes=changes)
    return user_row(u)


@router.post("/admin/users/{user_id}/reset-password")
def reset_password(user_id: int, request: Request, ctx: Ctx = Depends(require("users:manage")), db: Session = Depends(get_db)):
    u = _get(db, user_id)
    temp = _temp_password()
    u.password_hash, u.must_change_password, u.password_changed_at = hash_password(temp), True, utcnow()
    u.failed_logins, u.locked_until = 0, None
    db.commit()
    revoke_user_sessions(db, u.id)
    audit.record("users.password_reset", request=request, actor=ctx.user, target_type="user", target_id=u.id)
    return {"temporary_password": temp, "note": "One-time password: share it securely. The user must change it at sign-in."}


@router.post("/admin/users/{user_id}/reset-mfa")
def reset_mfa(user_id: int, request: Request, ctx: Ctx = Depends(require("users:manage")), db: Session = Depends(get_db)):
    u = _get(db, user_id)
    u.mfa_enabled, u.mfa_secret, u.mfa_last_step, u.recovery_codes = False, None, None, []
    db.commit()
    revoke_user_sessions(db, u.id)
    audit.record("users.mfa_reset", request=request, actor=ctx.user, target_type="user", target_id=u.id)
    return user_row(u)


@router.post("/admin/users/{user_id}/unlock")
def unlock(user_id: int, request: Request, ctx: Ctx = Depends(require("users:manage")), db: Session = Depends(get_db)):
    u = _get(db, user_id)
    u.failed_logins, u.locked_until = 0, None
    db.commit()
    audit.record("users.unlocked", request=request, actor=ctx.user, target_type="user", target_id=u.id)
    return user_row(u)


@router.post("/admin/users/{user_id}/revoke-sessions")
def revoke_sessions(user_id: int, request: Request, ctx: Ctx = Depends(require("users:manage")), db: Session = Depends(get_db)):
    u = _get(db, user_id)
    n = revoke_user_sessions(db, u.id, except_id=ctx.session.id if u.id == ctx.user.id else None)
    audit.record("users.sessions_revoked", request=request, actor=ctx.user, target_type="user", target_id=u.id, count=n)
    return {"revoked": n}


# ------------------------------------------------------------------ audit log
def _audit_query(action: str | None, actor: str | None, outcome: str | None):
    q = select(AuditEvent).order_by(AuditEvent.id.desc())
    if action:
        q = q.where(AuditEvent.action.like(action.replace("*", "%")))
    if actor:
        q = q.where(AuditEvent.actor.ilike(f"%{actor}%"))
    if outcome:
        q = q.where(AuditEvent.outcome == outcome)
    return q


@router.get("/audit")
def audit_log(request: Request, limit: int = Query(100, le=500), before_id: int | None = None, action: str | None = None,
              actor: str | None = None, outcome: str | None = None, ctx: Ctx = Depends(require("audit:read")),
              db: Session = Depends(get_db)):
    q = _audit_query(action, actor, outcome)
    if before_id:
        q = q.where(AuditEvent.id < before_id)
    rows = [audit.to_dict(e) for e in db.scalars(q.limit(limit))]
    audit.record("audit.read", request=request, actor=ctx.user, filters={"action": action, "actor": actor, "outcome": outcome})
    return rows


@router.get("/audit/verify")
def audit_verify(request: Request, ctx: Ctx = Depends(require("audit:read")), db: Session = Depends(get_db)):
    res = audit.verify_chain(db)
    audit.record("audit.verified", "success" if res["ok"] else "failure", request=request, actor=ctx.user, **res)
    return res


def _cell(v):
    """Neutralise spreadsheet formulas (CSV injection): sign-up names and emails are user-controlled."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/audit/export.csv")
def audit_export(request: Request, action: str | None = None, actor: str | None = None, outcome: str | None = None,
                 ctx: Ctx = Depends(require("audit:read")), db: Session = Depends(get_db)):
    rows = [audit.to_dict(e) for e in db.scalars(_audit_query(action, actor, outcome))]
    audit.record("audit.exported", request=request, actor=ctx.user, rows=len(rows))
    buf = io.StringIO()
    cols = ["id", "created_at", "actor_id", "actor", "action", "outcome", "target_type", "target_id", "ip", "user_agent", "details", "hash"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({k: _cell(v) for k, v in {**r, "details": json.dumps(r["details"], sort_keys=True)}.items()})
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="cardioai-audit-log.csv"'})
