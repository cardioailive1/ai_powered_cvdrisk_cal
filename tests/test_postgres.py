"""Postgres tests for the Render app with Prisma-managed schema.

Run against a database that `npx prisma migrate deploy` has migrated (CI does this; locally apply
prisma/migrations/*/migration.sql). Skipped unless TEST_DATABASE_URL is set, e.g.
    TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/cardioai pytest tests/test_postgres.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def _sa_url(url: str) -> str:
    return "postgresql+psycopg://" + url.split("://", 1)[1]


def _run(code: str, **env) -> subprocess.CompletedProcess:
    e = {**os.environ, "PYTHONPATH": str(ROOT), "DATABASE_URL": URL, "DB_SCHEMA_MANAGER": "prisma",
         "MODEL_DIR": str(ROOT / "models" / "synthetic-demo"), "SECRET_KEY": "x" * 48, **env}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e, cwd=ROOT, timeout=300)


def test_sqlalchemy_models_match_prisma_schema():
    """Every SQLAlchemy column exists in the Prisma-created table with a compatible type and nullability."""
    from sqlalchemy import create_engine, inspect

    sys.path.insert(0, str(ROOT))
    insp = inspect(create_engine(_sa_url(URL)))
    code = "import json; from app.db import Base; print(json.dumps({t.name: {c.name: [str(c.type), c.nullable] for c in t.columns} for t in Base.metadata.sorted_tables}))"
    r = _run(code)
    assert r.returncode == 0, r.stderr
    models = json.loads(r.stdout.strip().splitlines()[-1])
    for table, cols in models.items():
        db_cols = {c["name"]: c for c in insp.get_columns(table)}
        assert set(cols) == set(db_cols), f"{table}: model {sorted(cols)} vs db {sorted(db_cols)}"
        for name, (sa_type, nullable) in cols.items():
            db = db_cols[name]
            assert db["nullable"] is bool(nullable), f"{table}.{name}: nullable model={nullable} db={db['nullable']}"
            dbt = str(db["type"]).upper()
            sat = sa_type.upper()
            if sat.startswith("VARCHAR"):
                assert dbt == sat, f"{table}.{name}: {sat} vs {dbt}"
            elif "JSON" in sat:
                assert dbt == "JSONB", f"{table}.{name}: expected JSONB, got {dbt}"
            elif sat.startswith("DATETIME") or sat.startswith("TIMESTAMP"):
                assert dbt.startswith("TIMESTAMP"), f"{table}.{name}: {dbt}"
            elif sat == "INTEGER":
                assert dbt == "INTEGER", f"{table}.{name}: {dbt}"
            elif sat == "BOOLEAN":
                assert dbt == "BOOLEAN", f"{table}.{name}: {dbt}"
    assert any(ix["column_names"] == ["user_id"] for ix in insp.get_indexes("assessments"))
    assert insp.get_foreign_keys("assessments")[0]["referred_table"] == "users"
    fk = insp.get_foreign_keys("auth_sessions")[0]
    assert fk["referred_table"] == "users" and fk["options"].get("ondelete") == "CASCADE"
    assert any(ix["column_names"] == ["key", "created_at"] for ix in insp.get_indexes("auth_throttle"))


def test_app_end_to_end_on_postgres():
    """Sign-up -> approval -> MFA enrolment -> calculation -> audit chain, all on the Prisma-managed schema."""
    code = """
from fastapi.testclient import TestClient
from app.main import app
from app import mfa
with TestClient(app) as c:
    r = c.post('/api/auth/login', json={'email': 'pgadmin@cardioailive.com', 'password': 'Pg-Admin-Passphrase-77'}).json()
    assert r['steps'] == ['enroll_mfa'], r
    h = {'X-CSRF-Token': r['csrf']}
    sec = c.post('/api/auth/mfa/setup', headers=h).json()['secret']
    r = c.post('/api/auth/mfa/enable', json={'code': mfa.totp_now(sec)}, headers=h).json()
    assert r['status'] == 'ok', r
    h = {'X-CSRF-Token': r['csrf']}
    assert TestClient(app).post('/api/auth/signup', json={'email': 'pg.clin@clinic.org', 'full_name': 'Pg Clin',
        'password': 'Another-Strong-Phrase-19', 'accept_terms': True}).status_code == 202
    uid = next(u['id'] for u in c.get('/api/admin/users').json() if u['email'] == 'pg.clin@clinic.org')
    assert c.post(f'/api/admin/users/{uid}/approve', json={'role': 'clinician'}, headers=h).status_code == 200
    p = dict(age=62, sex='male', race='white', height_cm=178, weight_kg=92, sbp=142, dbp=86, smoking='current',
             diabetes='type2', total_chol=230, hdl=38, ldl=150, trig=210, egfr=70, hypertension=True)
    r = c.post('/api/predict', json={'calculator': 'ami', 'patient': p}, headers=h)
    assert r.status_code == 200, r.text
    rows = c.get('/api/assessments').json()
    assert rows and rows[0]['inputs']['sbp'] == 142 and rows[0]['primary']['risk'] > 0
    v = c.get('/api/audit/verify').json()
    assert v['ok'] and v['events_checked'] >= 8, v
print('OK')
"""
    r = _run(code, ADMIN_USERNAME="pgadmin@cardioailive.com", ADMIN_PASSWORD="Pg-Admin-Passphrase-77")
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_audit_log_is_append_only_in_postgres():
    import psycopg

    with psycopg.connect(URL, autocommit=True) as con:
        if not con.execute("SELECT count(*) FROM audit_events").fetchone()[0]:
            pytest.skip("no audit events yet")
        for sql in ("UPDATE audit_events SET outcome = 'success'", "DELETE FROM audit_events", "TRUNCATE audit_events"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                con.execute(sql)


def test_app_refuses_to_start_on_unmigrated_database():
    import psycopg

    admin = URL.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute("DROP DATABASE IF EXISTS cardioai_unmigrated")
        con.execute("CREATE DATABASE cardioai_unmigrated")
    empty = URL.rsplit("/", 1)[0] + "/cardioai_unmigrated"
    code = "from fastapi.testclient import TestClient\nfrom app.main import app\nTestClient(app).__enter__()"
    r = _run(code, DATABASE_URL=empty)
    assert r.returncode != 0 and "prisma migrate deploy" in r.stderr


def test_concurrent_startup_creates_one_admin():
    """Render starts several workers at once; all of them bootstrap the admin. None may crash (IntegrityError)."""
    code = "from app import main; main._bootstrap_admin(); print('OK')"
    env = {"ADMIN_USERNAME": "race.admin@cardioailive.com", "ADMIN_PASSWORD": "Race-Admin-Passphrase-5"}
    e = {**os.environ, "PYTHONPATH": str(ROOT), "DATABASE_URL": URL, "DB_SCHEMA_MANAGER": "prisma",
         "MODEL_DIR": str(ROOT / "models" / "synthetic-demo"), "SECRET_KEY": "x" * 48, **env}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=e, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(6)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 and "OK" in o for p, (o, _) in zip(procs, outs)), [x[1][-800:] for x in outs]
    assert not any("IntegrityError" in err for _, err in outs)
    import psycopg
    with psycopg.connect(URL) as con:
        assert con.execute("SELECT count(*) FROM users WHERE username = 'race.admin@cardioailive.com'").fetchone()[0] == 1
