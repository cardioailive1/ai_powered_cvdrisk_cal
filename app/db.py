from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings

_kwargs = {"connect_args": {"check_same_thread": False}} if settings.database_url.startswith("sqlite") else {"pool_pre_ping": True}
engine = create_engine(settings.database_url, **_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def aware(t: dt.datetime | None) -> dt.datetime | None:
    """SQLite returns naive datetimes; everything is stored in UTC."""
    return None if t is None else (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc))


class Base(DeclarativeBase):
    pass


# Column definitions mirror prisma/schema.prisma (Prisma owns the Postgres schema on Render);
# tests/test_postgres.py checks the two stay in step.
JSONType = JSON().with_variant(JSONB(), "postgresql")
TS = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # sign-in name: lower-case email
    full_name: Mapped[str] = mapped_column(String(120), default="")
    organization: Mapped[str] = mapped_column(String(160), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="clinician")          # app.rbac.ROLES
    status: Mapped[str] = mapped_column(String(20), default="pending")          # pending|active|disabled|rejected
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Fernet-encrypted TOTP secret
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_last_step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # last TOTP step used (replay block)
    recovery_codes: Mapped[list] = mapped_column(JSONType, default=list)          # HMAC hashes, single use
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[dt.datetime]] = mapped_column(TS, nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(TS, nullable=True)
    approved_by_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approved_at: Mapped[Optional[dt.datetime]] = mapped_column(TS, nullable=True)
    terms_accepted_at: Mapped[Optional[dt.datetime]] = mapped_column(TS, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class AuthSession(Base):
    """Server-side session. The browser holds only an opaque random token in an HttpOnly cookie;
    the database stores its SHA-256, so a database leak does not leak usable sessions."""
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scope: Mapped[str] = mapped_column(String(20), default="full")   # full | setup (MFA enrolment / password change only)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(TS)
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(TS, nullable=True)


class AuditEvent(Base):
    """Append-only, hash-chained audit trail (HIPAA §164.312(b); SOC 2 CC7.2). Postgres rejects UPDATE/DELETE
    on this table (trigger in the Prisma migration). Never holds clinical values."""
    __tablename__ = "audit_events"
    __table_args__ = (Index("audit_events_created_at_idx", "created_at"), Index("audit_events_actor_id_idx", "actor_id"),
                      Index("audit_events_action_idx", "action"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(254), default="")
    action: Mapped[str] = mapped_column(String(60))
    outcome: Mapped[str] = mapped_column(String(20))                  # success | failure | denied
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class AuthThrottle(Base):
    """One row per sign-in / sign-up attempt, keyed by IP; shared by all app instances."""
    __tablename__ = "auth_throttle"
    __table_args__ = (Index("auth_throttle_key_created_at_idx", "key", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Assessment(Base):
    """Calculator run. Stores clinical inputs WITHOUT patient identifiers."""
    __tablename__ = "assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    calculator: Mapped[str] = mapped_column(String(20))
    inputs: Mapped[dict] = mapped_column(JSONType)
    results: Mapped[dict] = mapped_column(JSONType)
    model_bundle: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, index=True)


class SchemaNotMigrated(RuntimeError):
    pass


def init_db() -> None:
    if settings.schema_manager == "prisma":
        insp = inspect(engine)
        missing = sorted(set(Base.metadata.tables) - set(insp.get_table_names()))
        stale = [] if missing else sorted(
            f"{t.name}.{c.name}" for t in Base.metadata.sorted_tables for c in t.columns
            if c.name not in {col["name"] for col in insp.get_columns(t.name)})
        if missing or stale:
            raise SchemaNotMigrated(
                f"Database schema is behind the app (missing: {', '.join(missing or stale)}). The schema is managed by "
                "Prisma: run `npx prisma migrate deploy` (Render runs it as the preDeployCommand).")
        return
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
