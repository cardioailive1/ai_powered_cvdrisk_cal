from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings

_kwargs = {"connect_args": {"check_same_thread": False}} if settings.database_url.startswith("sqlite") else {"pool_pre_ping": True}
engine = create_engine(settings.database_url, **_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


# Column definitions mirror prisma/schema.prisma (Prisma owns the Postgres schema on Render).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="clinician")  # clinician | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Assessment(Base):
    """Calculator run. Stores clinical inputs WITHOUT patient identifiers."""
    __tablename__ = "assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    calculator: Mapped[str] = mapped_column(String(20))
    inputs: Mapped[dict] = mapped_column(JSONType)
    results: Mapped[dict] = mapped_column(JSONType)
    model_bundle: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SchemaNotMigrated(RuntimeError):
    pass


def init_db() -> None:
    if settings.schema_manager == "prisma":
        missing = sorted(set(Base.metadata.tables) - set(inspect(engine).get_table_names()))
        if missing:
            raise SchemaNotMigrated(
                f"Database tables missing: {', '.join(missing)}. The schema is managed by Prisma: run "
                "`npx prisma migrate deploy` (Render runs it as the preDeployCommand).")
        return
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
