"""Authentication, sessions and authorization.

* Server-side sessions: an opaque 256-bit token in an HttpOnly, Secure, SameSite=Strict cookie. The database
  keeps only its SHA-256. Sessions end after SESSION_IDLE_MINUTES of inactivity or SESSION_MAX_HOURS in total,
  and on sign-out, password change, role change or account disable.
* CSRF: state-changing requests must echo the per-session CSRF token in the X-CSRF-Token header.
* Session scope "setup" can only enrol MFA or change the password (forced on first sign-in).
* Authorization: every route depends on require("<permission>") (see app.rbac).
"""
from __future__ import annotations

import datetime as dt
import hmac
import re
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from cardioai_core.security import hash_password, sha256_hex, verify_password  # noqa: F401

from . import audit, rbac
from .config import settings
from .db import AuthSession, AuthThrottle, User, aware, get_db, utcnow

COOKIE = "__Host-cardioai_session" if settings.cookie_secure else "cardioai_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Ctx:
    user: User
    session: AuthSession
    csrf: str

    def can(self, perm: str) -> bool:
        return rbac.has(self.user.role, perm)


# ------------------------------------------------------------------ sessions
def csrf_for(token: str) -> str:
    """CSRF token bound to the session token (synchroniser token without storing it)."""
    return hmac.new(settings.secret_key.encode(), ("csrf:" + token).encode(), "sha256").hexdigest()


def start_session(db: Session, user: User, request: Request, response: Response, scope: str = "full") -> str:
    """Create a session, set the cookie, return the CSRF token (kept by the page in memory only)."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    s = AuthSession(user_id=user.id, token_hash=sha256_hex(token), scope=scope,
                    ip=audit.client_ip(request), user_agent=request.headers.get("user-agent", "")[:255],
                    created_at=now, last_seen_at=now, expires_at=now + dt.timedelta(hours=settings.session_max_hours))
    db.add(s)
    db.commit()
    response.set_cookie(COOKIE, token, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/",
                        max_age=settings.session_max_hours * 3600)
    return csrf_for(token)


def end_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict")


def revoke_user_sessions(db: Session, user_id: int, except_id: int | None = None) -> int:
    q = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if except_id is not None:
        q = q.where(AuthSession.id != except_id)
    n = db.execute(q.values(revoked_at=utcnow())).rowcount
    db.commit()
    return n


def _load(request: Request, db: Session) -> Ctx:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    s = db.scalar(select(AuthSession).where(AuthSession.token_hash == sha256_hex(token)))
    now = utcnow()
    if s is None or s.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session ended — please sign in again")
    if aware(s.expires_at) <= now or aware(s.last_seen_at) + dt.timedelta(minutes=settings.session_idle_minutes) <= now:
        s.revoked_at = now
        db.commit()
        audit.record("auth.session_timeout", request=request, actor_label="", target_type="user", target_id=s.user_id)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Signed out after inactivity — please sign in again")
    user = db.get(User, s.user_id)
    if user is None or user.status != "active":
        s.revoked_at = now
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not hmac.compare_digest(sent, csrf_for(token)):
            audit.record("auth.csrf_rejected", "denied", request=request, actor=user, path=request.url.path)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Security token missing or invalid — reload the page")
    # sliding idle timeout; the page's own idle timer signs out after the same interval
    if (now - aware(s.last_seen_at)).total_seconds() > 30:
        s.last_seen_at = now
        db.commit()
    return Ctx(user, s, csrf_for(token))


def any_session(request: Request, db: Session = Depends(get_db)) -> Ctx:
    """Signed in, including restricted 'setup' sessions (MFA enrolment, forced password change)."""
    return _load(request, db)


def full_session(request: Request, db: Session = Depends(get_db)) -> Ctx:
    ctx = _load(request, db)
    if ctx.session.scope != "full":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Finish account setup (MFA / password) first")
    return ctx


def require(perm: str):
    def dep(request: Request, ctx: Ctx = Depends(full_session)) -> Ctx:
        if not ctx.can(perm):
            audit.record("access.denied", "denied", request=request, actor=ctx.user, permission=perm, path=request.url.path)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role does not allow this action")
        return ctx
    return dep


# ------------------------------------------------------------------ throttling and lockout
def throttle(db: Session, key: str, limit: int, window_s: int) -> None:
    now = utcnow()
    since = now - dt.timedelta(seconds=window_s)
    n = db.scalar(select(func.count()).select_from(AuthThrottle).where(AuthThrottle.key == key, AuthThrottle.created_at >= since))
    if n >= limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please wait a few minutes and try again.")
    db.add(AuthThrottle(key=key[:128], created_at=now))
    db.execute(delete(AuthThrottle).where(AuthThrottle.created_at < now - dt.timedelta(days=1)))
    db.commit()


def is_locked(user: User) -> bool:
    return user.locked_until is not None and aware(user.locked_until) > utcnow()


def register_failure(db: Session, user: User, request: Request) -> None:
    user.failed_logins += 1
    locked = user.failed_logins >= settings.lockout_threshold
    if locked:
        user.locked_until = utcnow() + dt.timedelta(minutes=settings.lockout_minutes)
        user.failed_logins = 0
    db.commit()
    if locked:
        audit.record("auth.account_locked", "denied", request=request, actor=user, target_type="user", target_id=user.id,
                     minutes=settings.lockout_minutes)


# ------------------------------------------------------------------ password policy (NIST SP 800-63B)
_COMMON = {
    "password", "passw0rd", "p@ssw0rd", "letmein", "welcome", "qwerty", "iloveyou", "admin", "administrator",
    "12345678", "123456789", "1234567890", "abc123", "monkey", "dragon", "football", "baseball", "sunshine",
    "princess", "trustno1", "changeme", "cardio", "heart", "doctor", "medical", "clinician", "hospital",
    "mayo", "cardioai", "heart123", "cardio2024", "medical2024",
}


_SEQUENCES = ("0123456789" * 3, "abcdefghijklmnopqrstuvwxyz" * 2, "qwertyuiopasdfghjklzxcvbnm" * 2)


def password_problems(pw: str, username: str = "", full_name: str = "") -> list[str]:
    errs = []
    if len(pw) < settings.password_min_length:
        errs.append(f"Use at least {settings.password_min_length} characters.")
    if len(pw) > 128:
        errs.append("Use at most 128 characters.")
    low = pw.lower()
    if len(set(pw)) < 5:
        errs.append("Use more varied characters.")
    if re.fullmatch(r"(.)\1+", low) or any(low in seq for seq in _SEQUENCES):
        errs.append("Avoid repeated or sequential characters.")
    stripped = re.sub(r"[^a-z]", "", low)
    if low in _COMMON or stripped in _COMMON or any(stripped == w + w for w in _COMMON):
        errs.append("This password is too common.")
    local = username.split("@")[0].lower()
    if len(local) >= 4 and local in low:
        errs.append("Do not include your email name in the password.")
    for part in full_name.lower().split():
        if len(part) >= 4 and part in low:
            errs.append("Do not include your name in the password.")
            break
    return errs


EMAIL_RE = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def normalize_email(s: str) -> str:
    return s.strip().lower()
