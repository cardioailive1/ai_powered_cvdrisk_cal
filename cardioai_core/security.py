"""Password hashing and signed session tokens, shared by the Render app and the
curation workbench."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os

import jwt

_ITER = 390_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def make_token(secret: str, user_id: int, username: str, role: str, ttl_minutes: int) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return jwt.encode({"sub": str(user_id), "usr": username, "role": role, "iat": now,
                       "exp": now + dt.timedelta(minutes=ttl_minutes)}, secret, algorithm="HS256")


def read_token(secret: str, token: str) -> dict:
    """Raises jwt.PyJWTError when invalid or expired."""
    return jwt.decode(token, secret, algorithms=["HS256"])


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
