"""Security controls: account requests, approval, MFA, sessions, CSRF, lockout, RBAC, audit trail, headers."""
import datetime as dt

import pytest

from conftest import ADMIN, CLOCK, new_browser, next_code, sign_in

PATIENT = dict(age=62, sex="male", race="white", height_cm=178, weight_kg=92, sbp=142, dbp=86, smoking="current",
               diabetes="type2", total_chol=230, hdl=38, ldl=150, trig=210, egfr=70, hypertension=True)
GOOD_PW = "Correct-Horse-Battery-71"


@pytest.fixture(scope="module")
def app(app_started):
    return app_started[0]


def request_account(app, email, password=GOOD_PW, **kw):
    c = new_browser(app)
    return c.post("/api/auth/signup", json={"email": email, "full_name": kw.get("full_name", "Dana Rivera"),
                                            "organization": "Test Clinic", "password": password,
                                            "accept_terms": kw.get("accept_terms", True)})


def find_user(admin, email):
    return next(u for u in admin.get("/api/admin/users").json() if u["email"] == email)


def approve(admin, email, role):
    uid = find_user(admin, email)["id"]
    r = admin.post(f"/api/admin/users/{uid}/approve", json={"role": role})
    assert r.status_code == 200, r.text
    return uid


# ------------------------------------------------------------------ gate
def test_everything_behind_sign_in(app_started):
    c = app_started[1]
    for path in ("/api/meta", "/api/models", "/api/assessments", "/api/me", "/api/admin/users", "/api/audit"):
        assert c.get(path).status_code == 401, path
    assert c.get("/healthz").status_code == 200 and c.get("/api/auth/config").status_code == 200


def test_security_headers(app_started):
    r = app_started[1].get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert app_started[1].get("/api/auth/config").headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ sign-up and approval
def test_signup_validates_password_and_terms(app):
    r = request_account(app, "weak@clinic.org", password="password1234")
    assert r.status_code == 422 and any("common" in e for e in r.json()["errors"])
    r = request_account(app, "short@clinic.org", password="Abc-12")
    assert r.status_code == 422 and any("at least 12" in e for e in r.json()["errors"])
    r = request_account(app, "terms@clinic.org", accept_terms=False)
    assert r.status_code == 422 and any("terms" in e for e in r.json()["errors"])
    assert request_account(app, "not-an-email").status_code == 422


def test_signup_is_pending_until_approved(app, admin):
    assert request_account(app, "Dana.Rivera@Clinic.org").status_code == 202
    c = new_browser(app)
    r = c.post("/api/auth/login", json={"email": "dana.rivera@clinic.org", "password": GOOD_PW})
    assert r.status_code == 403 and "approval" in r.json()["detail"]
    u = find_user(admin, "dana.rivera@clinic.org")
    assert u["status"] == "pending" and u["role"] == "clinician"
    approve(admin, "dana.rivera@clinic.org", "clinician")
    clin = sign_in(app, "dana.rivera@clinic.org", GOOD_PW)       # forced MFA enrolment on first sign-in
    assert clin.user["role"] == "clinician" and clin.user["mfa_enabled"] and len(clin.recovery_codes) == 8


def test_duplicate_signup_gives_same_answer(app, admin):
    a = request_account(app, "dup@clinic.org")
    b = request_account(app, "dup@clinic.org", password="Another-Long-Passphrase-9")
    assert a.status_code == b.status_code == 202 and a.json() == b.json()


def test_reject_request(app, admin):
    request_account(app, "nope@clinic.org")
    uid = find_user(admin, "nope@clinic.org")["id"]
    assert admin.post(f"/api/admin/users/{uid}/reject").json()["status"] == "rejected"
    r = new_browser(app).post("/api/auth/login", json={"email": "nope@clinic.org", "password": GOOD_PW})
    assert r.status_code == 403


# ------------------------------------------------------------------ MFA and sessions
def test_setup_session_cannot_reach_clinical_data(app, admin):
    request_account(app, "setup@clinic.org")
    approve(admin, "setup@clinic.org", "clinician")
    c = new_browser(app)
    body = c.post("/api/auth/login", json={"email": "setup@clinic.org", "password": GOOD_PW}).json()
    assert body["status"] == "setup_required" and body["steps"] == ["enroll_mfa"]
    assert c.get("/api/meta").status_code == 403
    assert c.post("/api/predict", json={"calculator": "ami", "patient": PATIENT}, headers={"X-CSRF-Token": body["csrf"]}).status_code == 403


def test_mfa_code_required_and_replay_blocked(app, admin):
    request_account(app, "mfa@clinic.org")
    approve(admin, "mfa@clinic.org", "clinician")
    first = sign_in(app, "mfa@clinic.org", GOOD_PW)
    c = new_browser(app)
    ch = c.post("/api/auth/login", json={"email": "mfa@clinic.org", "password": GOOD_PW}).json()
    assert ch["status"] == "mfa_required" and "csrf" not in ch
    assert c.post("/api/auth/mfa/verify", json={"challenge": ch["challenge"], "code": "000000"}).status_code == 401
    code = next_code(first.secret)
    assert c.post("/api/auth/mfa/verify", json={"challenge": ch["challenge"], "code": code}).status_code == 200
    c2 = new_browser(app)
    ch2 = c2.post("/api/auth/login", json={"email": "mfa@clinic.org", "password": GOOD_PW}).json()
    assert c2.post("/api/auth/mfa/verify", json={"challenge": ch2["challenge"], "code": code}).status_code == 401  # replay
    # a recovery code works exactly once
    rc = first.recovery_codes[0]
    ch3 = c2.post("/api/auth/login", json={"email": "mfa@clinic.org", "password": GOOD_PW}).json()
    assert c2.post("/api/auth/mfa/verify", json={"challenge": ch3["challenge"], "code": rc}).status_code == 200
    c3 = new_browser(app)
    ch4 = c3.post("/api/auth/login", json={"email": "mfa@clinic.org", "password": GOOD_PW}).json()
    assert c3.post("/api/auth/mfa/verify", json={"challenge": ch4["challenge"], "code": rc}).status_code == 401


def test_csrf_required_for_state_changes(admin):
    r = admin.c.post("/api/predict", json={"calculator": "ami", "patient": PATIENT})  # no header
    assert r.status_code == 403
    r = admin.c.post("/api/predict", json={"calculator": "ami", "patient": PATIENT}, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    assert admin.post("/api/predict", json={"calculator": "ami", "patient": PATIENT}).status_code == 200


def test_session_cookie_flags(app):
    request_account(app, "cookie@clinic.org")
    c = new_browser(app)
    r = c.post("/api/auth/login", json={"email": "cookie@clinic.org", "password": GOOD_PW})
    assert r.status_code == 403  # pending: no cookie handed out
    assert "set-cookie" not in r.headers
    r = c.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.json()["status"] == "mfa_required" and "set-cookie" not in r.headers  # no session before the second factor


def test_idle_timeout(app, admin):
    from app.db import AuthSession, SessionLocal
    request_account(app, "idle@clinic.org")
    approve(admin, "idle@clinic.org", "viewer")
    b = sign_in(app, "idle@clinic.org", GOOD_PW)
    assert b.get("/api/meta").status_code == 200
    with SessionLocal() as db:
        s = db.query(AuthSession).filter(AuthSession.user_id == b.user["id"], AuthSession.revoked_at.is_(None)).one()
        s.last_seen_at = s.last_seen_at - dt.timedelta(minutes=16)
        db.commit()
    r = b.get("/api/meta")
    assert r.status_code == 401 and "inactivity" in r.json()["detail"]


def test_logout_revokes_session(app, admin):
    request_account(app, "logout@clinic.org")
    approve(admin, "logout@clinic.org", "viewer")
    b = sign_in(app, "logout@clinic.org", GOOD_PW)
    cookie = dict(b.c.cookies)
    assert b.post("/api/auth/logout").status_code == 200
    replay = new_browser(app)
    replay.cookies.update(cookie)
    assert replay.get("/api/me").status_code == 401


def test_lockout_after_repeated_failures(app, admin):
    request_account(app, "lock@clinic.org")
    approve(admin, "lock@clinic.org", "viewer")
    c = new_browser(app)
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "lock@clinic.org", "password": "wrong-password-x"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": "lock@clinic.org", "password": GOOD_PW})
    assert r.status_code == 423
    uid = find_user(admin, "lock@clinic.org")["id"]
    assert find_user(admin, "lock@clinic.org")["locked"]
    admin.post(f"/api/admin/users/{uid}/unlock")
    assert c.post("/api/auth/login", json={"email": "lock@clinic.org", "password": GOOD_PW}).status_code == 200


def test_unknown_and_wrong_password_look_the_same(app):
    c = new_browser(app)
    a = c.post("/api/auth/login", json={"email": "ghost@clinic.org", "password": "whatever-123456"})
    b = c.post("/api/auth/login", json={"email": ADMIN[0], "password": "whatever-123456"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


# ------------------------------------------------------------------ RBAC
@pytest.fixture(scope="module")
def roles(app, admin):
    out = {}
    for role in ("viewer", "clinician", "auditor"):
        email = f"{role}.rbac@clinic.org"
        request_account(app, email)
        approve(admin, email, role)
        out[role] = sign_in(app, email, GOOD_PW)
    out["admin"] = admin
    return out


@pytest.mark.parametrize("role,method,path,allowed", [
    ("viewer", "get", "/api/meta", True), ("viewer", "post", "/api/predict", False),
    ("viewer", "get", "/api/assessments", False), ("viewer", "get", "/api/admin/users", False),
    ("clinician", "post", "/api/predict", True), ("clinician", "get", "/api/assessments", True),
    ("clinician", "get", "/api/audit", False), ("clinician", "get", "/api/admin/users", False),
    ("auditor", "get", "/api/audit", True), ("auditor", "get", "/api/admin/users", True),
    ("auditor", "post", "/api/predict", False), ("auditor", "get", "/api/assessments", False),
    ("auditor", "post", "/api/admin/users", False),
    ("admin", "get", "/api/audit", True), ("admin", "post", "/api/predict", True),
])
def test_role_permissions(roles, role, method, path, allowed):
    b = roles[role]
    kw = {"json": {"calculator": "ami", "patient": PATIENT}} if path == "/api/predict" else (
        {"json": {"email": "x@clinic.org", "full_name": "X Y"}} if method == "post" else {})
    r = getattr(b, method)(path, **kw)
    assert (r.status_code < 400) == allowed, (role, path, r.status_code, r.text)
    if not allowed:
        assert r.status_code == 403


def test_clinicians_only_see_their_own_assessments(app, admin, roles):
    request_account(app, "other.clin@clinic.org")
    approve(admin, "other.clin@clinic.org", "clinician")
    other = sign_in(app, "other.clin@clinic.org", GOOD_PW)
    assert roles["clinician"].post("/api/predict", json={"calculator": "ami", "patient": PATIENT}).status_code == 200
    assert other.get("/api/assessments").json() == []


def test_role_change_signs_user_out_and_last_admin_protected(app, admin):
    request_account(app, "promote@clinic.org")
    uid = approve(admin, "promote@clinic.org", "viewer")
    b = sign_in(app, "promote@clinic.org", GOOD_PW)
    assert admin.patch(f"/api/admin/users/{uid}", json={"role": "clinician"}).status_code == 200
    assert b.get("/api/meta").status_code == 401                    # old privileges' session revoked
    assert admin.patch(f"/api/admin/users/{admin.user['id']}", json={"status": "disabled"}).status_code == 409
    assert admin.patch(f"/api/admin/users/{uid}", json={"status": "disabled"}).status_code == 200
    assert new_browser(app).post("/api/auth/login", json={"email": "promote@clinic.org", "password": GOOD_PW}).status_code == 403


def test_admin_created_account_must_change_password(app, admin):
    r = admin.post("/api/admin/users", json={"email": "new.hire@clinic.org", "full_name": "New Hire", "role": "clinician"})
    assert r.status_code == 201
    temp = r.json()["temporary_password"]
    c = new_browser(app)
    body = c.post("/api/auth/login", json={"email": "new.hire@clinic.org", "password": temp}).json()
    assert body["steps"] == ["change_password", "enroll_mfa"]
    b = sign_in(app, "new.hire@clinic.org", temp, new_password="Fresh-Unique-Passphrase-33")
    assert b.user["role"] == "clinician" and not b.user["steps"]


def test_admin_mfa_reset(app, admin):
    request_account(app, "lostphone@clinic.org")
    uid = approve(admin, "lostphone@clinic.org", "viewer")
    sign_in(app, "lostphone@clinic.org", GOOD_PW)
    admin.post(f"/api/admin/users/{uid}/reset-mfa")
    body = new_browser(app).post("/api/auth/login", json={"email": "lostphone@clinic.org", "password": GOOD_PW}).json()
    assert body["status"] == "setup_required" and body["steps"] == ["enroll_mfa"]


# ------------------------------------------------------------------ audit trail
def test_audit_trail_records_and_verifies(roles):
    aud = roles["auditor"]
    events = aud.get("/api/audit?limit=500").json()
    actions = {e["action"] for e in events}
    assert {"auth.login", "account.signup_requested", "users.approved", "phi.assessment_created",
            "access.denied", "auth.mfa_enabled", "auth.account_locked"} <= actions
    created = next(e for e in events if e["action"] == "phi.assessment_created")
    assert "sbp" not in str(created["details"]) and "142" not in str(created["details"])  # no clinical values in the log
    assert aud.get("/api/audit/verify").json()["ok"] is True
    csv = aud.get("/api/audit/export.csv")
    assert csv.status_code == 200 and csv.text.startswith("id,created_at")


def test_audit_chain_detects_tampering(roles):
    from app import audit
    from app.db import AuditEvent, SessionLocal
    with SessionLocal() as db:
        e = db.query(AuditEvent).order_by(AuditEvent.id).first()
        original = e.outcome
        e.outcome = "failure" if original != "failure" else "success"
        db.commit()
        bad = audit.verify_chain(db)
        e.outcome = original
        db.commit()
        assert bad["ok"] is False and bad["broken_at_id"] == e.id
        assert audit.verify_chain(db)["ok"] is True


def test_csv_export_neutralises_formulas(app, admin, roles):
    request_account(app, "csv@clinic.org", full_name="=HYPERLINK(1) Name")
    c = new_browser(app)
    c.post("/api/auth/login", json={"email": "=cmd@clinic.org", "password": "x" * 12})
    text = roles["auditor"].get("/api/audit/export.csv").text
    assert ",=cmd@clinic.org" not in text and ",'=cmd@clinic.org" in text


def test_totp_matches_rfc6238_vector():
    import base64

    from app import mfa
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert mfa._code(secret, 59 // 30) == "287082"          # RFC 6238 appendix B (SHA-1, 6 digits)
    assert mfa._code(secret, 1111111109 // 30) == "081804"


def test_mfa_secret_encrypted_at_rest(roles):
    from app.db import SessionLocal, User
    with SessionLocal() as db:
        u = db.get(User, roles["clinician"].user["id"])
        assert u.mfa_secret and roles["clinician"].secret not in u.mfa_secret
    assert CLOCK.t  # clock fixture active
