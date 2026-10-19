"""Account creation, sign-in (password + MFA), session and self-service account routes."""
from __future__ import annotations

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cardioai_core.security import DUMMY_HASH, make_token, needs_rehash, read_token

from . import audit, mfa, rbac
from .auth import (EMAIL_RE, Ctx, any_session, end_session_cookie, full_session, hash_password, is_locked,
                   normalize_email, password_problems, register_failure, revoke_user_sessions, start_session,
                   throttle, verify_password)
from .config import settings
from .db import AuthSession, User, aware, get_db, utcnow

router = APIRouter(prefix="/api")
GENERIC_FAIL = "Incorrect email or password."
TERMS_VERSION = "2026-10"


# ------------------------------------------------------------------ helpers
def setup_steps(u: User) -> list[str]:
    steps = []
    if u.must_change_password:
        steps.append("change_password")
    if settings.require_mfa and not u.mfa_enabled:
        steps.append("enroll_mfa")
    return steps


def user_payload(u: User, ctx: Ctx | None = None) -> dict:
    return {
        "id": u.id, "email": u.username, "full_name": u.full_name, "organization": u.organization,
        "role": u.role, "role_label": rbac.ROLES.get(u.role, {}).get("label", u.role),
        "permissions": rbac.permissions_for(u.role), "mfa_enabled": u.mfa_enabled,
        "last_login_at": aware(u.last_login_at).isoformat() if u.last_login_at else None,
        "steps": setup_steps(u), "scope": ctx.session.scope if ctx else None,
    }


def session_payload(u: User, csrf: str, scope: str) -> dict:
    return {"status": "ok" if scope == "full" else "setup_required", "csrf": csrf, "user": user_payload(u),
            "steps": setup_steps(u), "idle_minutes": settings.session_idle_minutes}


def finish_login(db: Session, u: User, request: Request, response: Response, method: str) -> dict:
    u.failed_logins, u.locked_until, u.last_login_at = 0, None, utcnow()
    db.commit()
    scope = "setup" if setup_steps(u) else "full"
    csrf = start_session(db, u, request, response, scope)
    audit.record("auth.login", request=request, actor=u, target_type="user", target_id=u.id, method=method, scope=scope)
    return session_payload(u, csrf, scope)


def rotate_to_full_if_ready(db: Session, ctx: Ctx, request: Request, response: Response) -> dict:
    """Leaving the restricted setup session: revoke it and issue a fresh full session (session fixation defence)."""
    if setup_steps(ctx.user):
        return session_payload(ctx.user, ctx.csrf, ctx.session.scope)
    ctx.session.revoked_at = utcnow()
    db.commit()
    csrf = start_session(db, ctx.user, request, response, "full")
    return session_payload(ctx.user, csrf, "full")


# ------------------------------------------------------------------ public configuration for the sign-in page
@router.get("/auth/config")
def auth_config(db: Session = Depends(get_db)):
    has_admin = db.scalar(select(User.id).where(User.role == "admin", User.status == "active").limit(1)) is not None
    return {
        "admin_configured": has_admin,
        "signup_mode": settings.signup_mode, "allowed_domains": settings.signup_allowed_domains,
        "password_min_length": settings.password_min_length, "require_mfa": settings.require_mfa,
        "idle_minutes": settings.session_idle_minutes, "terms_version": TERMS_VERSION,
        "roles": {k: {"label": v["label"], "description": v["description"]} for k, v in rbac.ROLES.items()},
    }


# ------------------------------------------------------------------ account request (sign-up)
class SignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    full_name: str = Field(min_length=2, max_length=120)
    organization: str = Field(default="", max_length=160)
    password: str = Field(min_length=1, max_length=200)
    accept_terms: bool = False


@router.post("/auth/signup", status_code=202)
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)):
    if settings.signup_mode == "closed":
        raise HTTPException(403, "Self-service account requests are turned off. Ask your administrator for an account.")
    throttle(db, "signup:" + audit.client_ip(request), settings.ip_signup_limit, 3600)
    email = normalize_email(body.email)
    errors = []
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    domain = email.rsplit("@", 1)[-1]
    if settings.signup_allowed_domains and domain not in settings.signup_allowed_domains:
        errors.append("Accounts can only be requested with an email address at: "
                      + ", ".join("@" + d for d in settings.signup_allowed_domains) + ".")
    if not body.accept_terms:
        errors.append("You must accept the acceptable-use and confidentiality terms.")
    errors += password_problems(body.password, email, body.full_name)
    if errors:
        raise HTTPException(422, {"errors": errors})
    msg = {"status": "pending", "detail": "Thanks — your account request was received. An administrator will review it; "
                                          "you can sign in once it is approved."}
    if db.scalar(select(User).where(User.username == email)):
        # Same answer as a new request so the form cannot be used to discover who has an account.
        audit.record("account.signup_duplicate", "failure", request=request, actor_label=email)
        return msg
    u = User(username=email, full_name=body.full_name.strip(), organization=body.organization.strip(),
             password_hash=hash_password(body.password), role="clinician", status="pending",
             terms_accepted_at=utcnow())
    db.add(u)
    db.commit()
    audit.record("account.signup_requested", request=request, actor=u, target_type="user", target_id=u.id,
                 organization=u.organization, terms_version=TERMS_VERSION)
    return msg


# ------------------------------------------------------------------ sign-in
class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    throttle(db, "login:" + audit.client_ip(request), settings.ip_login_limit, 900)
    name = normalize_email(body.email)
    u = db.scalar(select(User).where(User.username == name))
    if u is None:
        verify_password(body.password, DUMMY_HASH)  # equalise timing
        audit.record("auth.login", "failure", request=request, actor_label=name[:254], reason="unknown_account")
        raise HTTPException(401, GENERIC_FAIL)
    if is_locked(u):
        audit.record("auth.login", "denied", request=request, actor=u, reason="locked")
        raise HTTPException(423, f"Too many failed attempts — the account is locked for up to {settings.lockout_minutes} minutes.")
    if not verify_password(body.password, u.password_hash):
        register_failure(db, u, request)
        audit.record("auth.login", "failure", request=request, actor=u, reason="bad_password")
        raise HTTPException(401, GENERIC_FAIL)
    if u.status != "active":
        audit.record("auth.login", "denied", request=request, actor=u, reason="status_" + u.status)
        raise HTTPException(403, "Your account request is awaiting administrator approval." if u.status == "pending"
                            else "This account is not active. Contact your administrator.")
    if needs_rehash(u.password_hash):
        u.password_hash = hash_password(body.password)
        db.commit()
    if u.mfa_enabled:
        challenge = make_token(settings.secret_key, u.id, u.username, u.role, 5, purpose="mfa",
                               pwc=aware(u.password_changed_at).timestamp())
        return {"status": "mfa_required", "challenge": challenge}
    return finish_login(db, u, request, response, "password")


class MfaVerifyIn(BaseModel):
    challenge: str = Field(max_length=2000)
    code: str = Field(min_length=6, max_length=20)


@router.post("/auth/mfa/verify")
def mfa_verify(body: MfaVerifyIn, request: Request, response: Response, db: Session = Depends(get_db)):
    throttle(db, "login:" + audit.client_ip(request), settings.ip_login_limit, 900)
    try:
        c = read_token(settings.secret_key, body.challenge)
        assert c.get("purpose") == "mfa"
    except (jwt.PyJWTError, AssertionError):
        raise HTTPException(401, "Sign-in step expired — enter your password again.")
    u = db.get(User, int(c["sub"]))
    if u is None or u.status != "active" or not u.mfa_enabled or abs(aware(u.password_changed_at).timestamp() - c["pwc"]) > 1e-3:
        raise HTTPException(401, "Sign-in step expired — enter your password again.")
    if is_locked(u):
        raise HTTPException(423, f"Too many failed attempts — the account is locked for up to {settings.lockout_minutes} minutes.")
    code = body.code.strip()
    secret = mfa.decrypt(u.mfa_secret or "")
    step = mfa.verify_totp(secret, code, u.mfa_last_step) if secret and len(code.replace(" ", "")) == 6 else None
    if step is not None:
        u.mfa_last_step = step
        return finish_login(db, u, request, response, "password+totp")
    h = mfa.hash_recovery_code(code)
    if h in (u.recovery_codes or []):
        u.recovery_codes = [x for x in u.recovery_codes if x != h]
        db.commit()
        audit.record("auth.recovery_code_used", request=request, actor=u, remaining=len(u.recovery_codes))
        return finish_login(db, u, request, response, "password+recovery_code")
    register_failure(db, u, request)
    audit.record("auth.mfa", "failure", request=request, actor=u)
    raise HTTPException(401, "That code is not valid. Check your authenticator app and try again.")


@router.post("/auth/logout")
def logout(request: Request, response: Response, ctx: Ctx = Depends(any_session), db: Session = Depends(get_db)):
    ctx.session.revoked_at = utcnow()
    db.commit()
    end_session_cookie(response)
    audit.record("auth.logout", request=request, actor=ctx.user)
    return {"status": "signed_out"}


# ------------------------------------------------------------------ MFA enrolment
@router.post("/auth/mfa/setup")
def mfa_setup(request: Request, ctx: Ctx = Depends(any_session), db: Session = Depends(get_db)):
    u = ctx.user
    if u.mfa_enabled:
        raise HTTPException(409, "MFA is already set up. An administrator can reset it if you lost your device.")
    secret = mfa.new_secret()
    u.mfa_secret = mfa.encrypt(secret)
    db.commit()
    uri = mfa.provisioning_uri(secret, u.username)
    audit.record("auth.mfa_setup_started", request=request, actor=u)
    return {"secret": secret, "otpauth_uri": uri, "qr": mfa.qr_svg_data_uri(uri)}


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


@router.post("/auth/mfa/enable")
def mfa_enable(body: CodeIn, request: Request, response: Response, ctx: Ctx = Depends(any_session), db: Session = Depends(get_db)):
    u = ctx.user
    secret = mfa.decrypt(u.mfa_secret or "")
    step = mfa.verify_totp(secret, body.code, None) if secret and not u.mfa_enabled else None
    if step is None:
        audit.record("auth.mfa_enable", "failure", request=request, actor=u)
        raise HTTPException(422, "That code did not match. Make sure the time on your phone is correct and try again.")
    codes = mfa.new_recovery_codes()
    u.mfa_enabled, u.mfa_last_step, u.recovery_codes = True, step, [mfa.hash_recovery_code(c) for c in codes]
    db.commit()
    audit.record("auth.mfa_enabled", request=request, actor=u, target_type="user", target_id=u.id)
    revoke_user_sessions(db, u.id, except_id=ctx.session.id)
    out = rotate_to_full_if_ready(db, ctx, request, response)
    return {**out, "recovery_codes": codes}


# ------------------------------------------------------------------ password change
class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


@router.post("/auth/password")
def change_password(body: PasswordIn, request: Request, response: Response, ctx: Ctx = Depends(any_session),
                    db: Session = Depends(get_db)):
    u = ctx.user
    if not verify_password(body.current_password, u.password_hash):
        register_failure(db, u, request)
        audit.record("auth.password_change", "failure", request=request, actor=u)
        raise HTTPException(422, {"errors": ["Your current password is not correct."]})
    errors = password_problems(body.new_password, u.username, u.full_name)
    if verify_password(body.new_password, u.password_hash):
        errors.append("Choose a password different from your current one.")
    if errors:
        raise HTTPException(422, {"errors": errors})
    u.password_hash, u.must_change_password, u.password_changed_at = hash_password(body.new_password), False, utcnow()
    db.commit()
    revoke_user_sessions(db, u.id, except_id=ctx.session.id)
    audit.record("auth.password_changed", request=request, actor=u, target_type="user", target_id=u.id)
    return rotate_to_full_if_ready(db, ctx, request, response)


# ------------------------------------------------------------------ who am I / my sessions
@router.get("/me")
def me(ctx: Ctx = Depends(any_session)):
    return {**user_payload(ctx.user, ctx), "csrf": ctx.csrf, "idle_minutes": settings.session_idle_minutes}


@router.get("/me/sessions")
def my_sessions(ctx: Ctx = Depends(full_session), db: Session = Depends(get_db)):
    now = utcnow()
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == ctx.user.id, AuthSession.revoked_at.is_(None),
                                                AuthSession.expires_at > now).order_by(AuthSession.last_seen_at.desc()))
    return [{"id": s.id, "current": s.id == ctx.session.id, "ip": s.ip, "user_agent": s.user_agent,
             "created_at": aware(s.created_at).isoformat(), "last_seen_at": aware(s.last_seen_at).isoformat()} for s in rows]


@router.delete("/me/sessions/{session_id}")
def revoke_my_session(session_id: int, request: Request, ctx: Ctx = Depends(full_session), db: Session = Depends(get_db)):
    s = db.get(AuthSession, session_id)
    if s is None or s.user_id != ctx.user.id:
        raise HTTPException(404, "Not found")
    s.revoked_at = utcnow()
    db.commit()
    audit.record("auth.session_revoked", request=request, actor=ctx.user, target_type="session", target_id=s.id)
    return {"status": "revoked"}
