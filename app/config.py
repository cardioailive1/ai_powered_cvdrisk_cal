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
    secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)  # set in prod or tokens reset on restart
    token_ttl_minutes = int(os.getenv("TOKEN_TTL_MINUTES", "480"))

    _db = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'cardioai.db'}")
    # Render gives postgres://...; SQLAlchemy 2 + psycopg 3 wants postgresql+psycopg://
    if _db.startswith("postgres://"):
        _db = "postgresql+psycopg://" + _db[len("postgres://"):]
    elif _db.startswith("postgresql://"):
        _db = "postgresql+psycopg://" + _db[len("postgresql://"):]
    database_url = _db

    model_dir = os.getenv("MODEL_DIR", str(ROOT / "models" / "synthetic-demo"))
    model_bundle_url = os.getenv("MODEL_BUNDLE_URL", "")          # optional .tar.gz fetched at startup
    model_bundle_token = os.getenv("MODEL_BUNDLE_TOKEN", "")      # optional bearer token for that URL
    allow_demo_models = _bool("ALLOW_DEMO_MODELS", True)          # set false in clinical deployments

    admin_username = os.getenv("ADMIN_USERNAME", "")
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    store_assessments = _bool("STORE_ASSESSMENTS", True)
    cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]


settings = Settings()
