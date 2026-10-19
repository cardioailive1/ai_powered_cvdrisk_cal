"""Server-side authentication: PBKDF2 password hashes + signed JWT bearer tokens."""
from __future__ import annotations

import time
from collections import defaultdict, deque

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from cardioai_core.security import hash_password, make_token, read_token, verify_password  # noqa: F401

from .config import settings
from .db import User, get_db

_bearer = HTTPBearer(auto_error=False)


def create_token(user: User) -> str:
    return make_token(settings.secret_key, user.id, user.username, user.role, settings.token_ttl_minutes)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = read_token(settings.secret_key, creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired — please sign in again")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


# --- simple in-memory login throttle (per client IP) -----------------------
_attempts: dict[str, deque] = defaultdict(deque)
MAX_ATTEMPTS, WINDOW_S = 10, 300


def throttle_login(request: Request) -> None:
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    q = _attempts[ip]
    now = time.time()
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    if len(q) >= MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many sign-in attempts. Try again in a few minutes.")
    q.append(now)
