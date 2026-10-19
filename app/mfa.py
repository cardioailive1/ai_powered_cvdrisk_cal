"""Multi-factor authentication: RFC 6238 TOTP (authenticator apps), one-time recovery codes, and
encryption at rest for TOTP secrets."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
import urllib.parse

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

STEP_S, DIGITS, WINDOW = 30, 6, 1  # accept the current code and one step either side (clock drift)


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS).zfill(DIGITS)


def totp_now(secret: str, at: float | None = None) -> str:
    return _code(secret, int((at or time.time()) // STEP_S))


def verify_totp(secret: str, code: str, last_step: int | None) -> int | None:
    """Return the matched time step (store it to block replay) or None. Codes at or before last_step are refused."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return None
    now = int(time.time() // STEP_S)
    for step in range(now - WINDOW, now + WINDOW + 1):
        if (last_step is None or step > last_step) and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str) -> str:
    issuer = settings.mfa_issuer
    label = urllib.parse.quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={urllib.parse.quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_S}"


def qr_svg_data_uri(uri: str) -> str:
    import segno

    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=2, dark="#0f172a", light="#ffffff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ recovery codes
def new_recovery_codes(n: int = 8) -> list[str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)) for _ in range(n)]


def hash_recovery_code(code: str) -> str:
    norm = code.strip().upper().replace(" ", "")
    return hmac.new(settings.secret_key.encode(), norm.encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ secret encryption at rest
def _fernet() -> Fernet:
    key = hashlib.sha256(("cardioai-mfa:" + settings.mfa_encryption_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
