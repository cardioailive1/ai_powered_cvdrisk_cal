"""Runtime configuration — all from environment variables (set in Render)."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    env = os.getenv("APP_ENV", "development")
    production = env == "production"
    secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)  # set in prod or sessions reset on restart

    _db = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'cardioai.db'}")
    # Render gives postgres://...; SQLAlchemy 2 + psycopg 3 wants postgresql+psycopg://
    if _db.startswith("postgres://"):
        _db = "postgresql+psycopg://" + _db[len("postgres://"):]
    elif _db.startswith("postgresql://"):
        _db = "postgresql+psycopg://" + _db[len("postgresql://"):]
    database_url = _db
    # "prisma": the schema is owned by Prisma migrations (Render runs `npx prisma migrate deploy` before each
    # deploy); the app only checks the tables exist. "sqlalchemy" (default): create tables on start (local/dev).
    schema_manager = os.getenv("DB_SCHEMA_MANAGER", "sqlalchemy").strip().lower()

    model_dir = os.getenv("MODEL_DIR", str(ROOT / "models" / "synthetic-demo"))
    model_bundle_url = os.getenv("MODEL_BUNDLE_URL", "")          # optional .tar.gz fetched at startup
    model_bundle_token = os.getenv("MODEL_BUNDLE_TOKEN", "")      # optional bearer token for that URL
    allow_demo_models = _bool("ALLOW_DEMO_MODELS", True)          # set false in clinical deployments

    # --- first administrator (created once if no account with this sign-in name exists)
    admin_username = os.getenv("ADMIN_USERNAME", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "")

    # --- account creation
    # approval: anyone may request an account; an administrator approves it and assigns the role (default)
    # closed:   only administrators create accounts
    signup_mode = os.getenv("SIGNUP_MODE", "approval").strip().lower()
    signup_allowed_domains = [d.strip().lower().lstrip("@") for d in os.getenv("SIGNUP_ALLOWED_DOMAINS", "").split(",") if d.strip()]

    # --- sessions (HIPAA §164.312(a)(2)(iii) automatic logoff)
    session_idle_minutes = int(os.getenv("SESSION_IDLE_MINUTES", "15"))
    session_max_hours = int(os.getenv("SESSION_MAX_HOURS", "12"))
    cookie_secure = _bool("COOKIE_SECURE", production)

    # --- MFA (required for every account by default)
    require_mfa = _bool("REQUIRE_MFA", True)
    mfa_issuer = os.getenv("MFA_ISSUER", "CardioAI CVD Risk")
    mfa_encryption_key = os.getenv("MFA_ENCRYPTION_KEY") or secret_key

    # --- brute-force protection (stored in the database, so it holds across autoscaled instances)
    lockout_threshold = int(os.getenv("LOCKOUT_THRESHOLD", "5"))
    lockout_minutes = int(os.getenv("LOCKOUT_MINUTES", "15"))
    ip_login_limit = int(os.getenv("IP_LOGIN_LIMIT", "100"))      # sign-in attempts per IP per 15 min (hospitals share egress IPs)
    ip_signup_limit = int(os.getenv("IP_SIGNUP_LIMIT", "5"))      # account requests per IP per hour

    password_min_length = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))
    store_assessments = _bool("STORE_ASSESSMENTS", True)
    assessment_retention_days = int(os.getenv("ASSESSMENT_RETENTION_DAYS", "365"))   # 0 = keep forever
    cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]


settings = Settings()
