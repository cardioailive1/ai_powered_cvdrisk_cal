"""Shared setup for the web-app tests: a throwaway SQLite database, a first admin, a controllable clock for
TOTP codes, and helpers that sign a user in through the real flow (password -> MFA enrolment / code)."""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_DB = ROOT / "test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(TEST_DB))
os.environ.setdefault("ADMIN_USERNAME", "admin@cardioailive.com")
os.environ.setdefault("ADMIN_PASSWORD", "Initial-Admin-Passphrase-42")
os.environ.setdefault("MODEL_DIR", str(ROOT / "models" / "synthetic-demo"))
os.environ.setdefault("IP_LOGIN_LIMIT", "1000")
os.environ.setdefault("IP_SIGNUP_LIMIT", "1000")

import pytest  # noqa: E402

ADMIN = (os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"])


class Clock:
    """Stands in for time.time() inside app.mfa so tests never wait for a new 30-second TOTP step."""
    def __init__(self):
        self.t = time.time()

    def time(self):
        return self.t


CLOCK = Clock()


def next_code(secret: str) -> str:
    from app import mfa
    CLOCK.t += 31
    return mfa.totp_now(secret, CLOCK.t)


@pytest.fixture(scope="session", autouse=True)
def _patch_clock():
    from app import mfa
    mfa.time = CLOCK
    yield


@pytest.fixture(scope="session")
def app_started():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:   # runs startup (tables, first admin, model bundle) once
        yield app, c
    if TEST_DB.exists():
        TEST_DB.unlink()


class Signed:
    """A browser: its own cookie jar plus the CSRF token the page keeps in memory."""
    def __init__(self, client, csrf, secret=None, user=None, recovery_codes=None):
        self.c, self.csrf, self.secret, self.user, self.recovery_codes = client, csrf, secret, user, recovery_codes

    def h(self):
        return {"X-CSRF-Token": self.csrf}

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, **kw):
        return self.c.post(url, headers={**self.h(), **kw.pop("headers", {})}, **kw)

    def patch(self, url, **kw):
        return self.c.patch(url, headers={**self.h(), **kw.pop("headers", {})}, **kw)

    def delete(self, url, **kw):
        return self.c.delete(url, headers={**self.h(), **kw.pop("headers", {})}, **kw)


def new_browser(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def sign_in(app, email, password, secret=None, new_password=None):
    """Full sign-in. Enrols MFA if needed (returns the secret) and changes the password if forced."""
    c = new_browser(app)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    if body["status"] == "mfa_required":
        assert secret, "account has MFA: pass its secret"
        r = c.post("/api/auth/mfa/verify", json={"challenge": body["challenge"], "code": next_code(secret)})
        assert r.status_code == 200, r.text
        body = r.json()
    b = Signed(c, body["csrf"], secret, body["user"])
    if "change_password" in body["steps"]:
        assert new_password, "password change is required: pass new_password"
        r = b.post("/api/auth/password", json={"current_password": password, "new_password": new_password})
        assert r.status_code == 200, r.text
        body = r.json()
        b.csrf = body["csrf"]
    if "enroll_mfa" in body["steps"]:
        r = b.post("/api/auth/mfa/setup")
        assert r.status_code == 200, r.text
        b.secret = r.json()["secret"]
        r = b.post("/api/auth/mfa/enable", json={"code": next_code(b.secret)})
        assert r.status_code == 200, r.text
        body = r.json()
        b.csrf, b.recovery_codes = body["csrf"], body["recovery_codes"]
    assert body["status"] == "ok", body
    b.user = body["user"]
    return b


@pytest.fixture(scope="session")
def admin(app_started):
    app, _ = app_started
    return sign_in(app, *ADMIN)
