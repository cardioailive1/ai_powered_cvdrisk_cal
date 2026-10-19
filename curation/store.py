"""Curation database (SQLite in the workspace data dir) + tamper-evident audit trail.

Every state change is appended to `audit_log`; each entry's hash covers the
previous entry's hash, so any edit or deletion breaks the chain and is caught
by `verify_log()` (shown in the workbench and checked before every release).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


ROLES = ("admin", "data_scientist", "cardiologist", "lead_cardiologist")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(30))
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def can_read_cases(self) -> bool:
        return self.role in ("cardiologist", "lead_cardiologist")


class LabelRun(Base):
    __tablename__ = "label_runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="active")       # active | superseded
    label_model_version: Mapped[str] = mapped_column(String(80))
    label_model_sha: Mapped[str] = mapped_column(String(64))
    inputs_sha: Mapped[str] = mapped_column(String(64))
    governance_sha: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(400))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditReport(Base):
    __tablename__ = "audit_reports"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    label_run_id: Mapped[str] = mapped_column(ForeignKey("label_runs.id"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    overall: Mapped[str] = mapped_column(String(10))
    sha: Mapped[str] = mapped_column(String(64))
    report: Mapped[dict] = mapped_column(JSON)


class Review(Base):
    """Data-scientist acknowledgement/waiver of an audit check, or a waiver of a release gate."""
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label_run_id: Mapped[str] = mapped_column(ForeignKey("label_runs.id"), index=True)
    item_id: Mapped[str] = mapped_column(String(80))                       # audit:<check> | gate:<gate>
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))                         # acknowledge | waive | revoke
    justification: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label_run_id: Mapped[str] = mapped_column(ForeignKey("label_runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))                          # ds_dataset_review | ds_release | cardiology_release
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(20))                       # approve | reject
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)          # blinded id, e.g. ADJ-7F3A91C2
    label_run_id: Mapped[str] = mapped_column(ForeignKey("label_runs.id"), index=True)
    case_type: Mapped[str] = mapped_column(String(20))                      # event | annotation
    ref_id: Mapped[str] = mapped_column(String(40), index=True)             # candidate_id or note_id
    family: Mapped[str] = mapped_column(String(20))
    stratum: Mapped[str] = mapped_column(String(20))                        # uncertain | auto_confirmed | auto_rejected | annotation
    sampling_prob: Mapped[float] = mapped_column(Float, default=1.0)
    reads_required: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[str] = mapped_column(String(20), default="open")         # open | disagreement | final
    final: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Read(Base):
    __tablename__ = "reads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    reader_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20))                           # independent | lead
    decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Release(Base):
    __tablename__ = "releases"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    label_run_id: Mapped[str] = mapped_column(ForeignKey("label_runs.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(80))
    path: Mapped[str] = mapped_column(String(400))
    dataset_sha: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON)


class LogEntry(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[str] = mapped_column(String(40))
    username: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(120))
    details: Mapped[dict] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


GENESIS = "0" * 64


def _entry_hash(prev: str, ts: str, user: str, action: str, entity: str, details: dict) -> str:
    payload = json.dumps({"prev": prev, "ts": ts, "user": user, "action": action, "entity": entity, "details": details},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def log_event(db: Session, user: str, action: str, entity: str, details: dict[str, Any] | None = None) -> LogEntry:
    details = json.loads(json.dumps(details or {}, default=str))
    last = db.scalar(select(LogEntry).order_by(LogEntry.id.desc()).limit(1))
    prev = last.hash if last else GENESIS
    ts = utcnow().isoformat(timespec="microseconds")
    e = LogEntry(ts=ts, username=user, action=action, entity=entity, details=details, prev_hash=prev,
                 hash=_entry_hash(prev, ts, user, action, entity, details))
    db.add(e)
    db.flush()
    return e


def verify_log(db: Session) -> dict:
    prev, n = GENESIS, 0
    for e in db.scalars(select(LogEntry).order_by(LogEntry.id)):
        if e.prev_hash != prev or e.hash != _entry_hash(e.prev_hash, e.ts, e.username, e.action, e.entity, e.details):
            return {"ok": False, "entries": n, "first_bad_id": e.id}
        prev, n = e.hash, n + 1
    return {"ok": True, "entries": n, "head": prev}


def open_store(data_dir: str | Path) -> sessionmaker:
    p = Path(data_dir) / "curation.db"
    engine = create_engine(f"sqlite:///{p}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with engine.begin() as con:   # add columns introduced after a database was created
        cols = {r[1] for r in con.exec_driver_sql("PRAGMA table_info(users)")}
        if "email" not in cols:
            con.exec_driver_sql("ALTER TABLE users ADD COLUMN email VARCHAR(200) DEFAULT ''")
    return sessionmaker(bind=engine, expire_on_commit=False)


def check_reader_policy(db: Session, gov: dict, role: str, email: str) -> None:
    """Raise ValueError if creating a user with this role/e-mail breaks the platform's reader rules."""
    pol = gov.get("reader_policy", {})
    dom = pol.get("email_domain")
    if dom and role in pol.get("domain_roles", []):
        if not email or not email.lower().endswith("@" + dom.lower()):
            raise ValueError(f"{role} accounts need an @{dom} e-mail (Mayo Clinic Platform workspace seats require it)")
    if role in ("cardiologist", "lead_cardiologist"):
        n = len(list(db.scalars(select(User).where(User.active, User.role.in_(["cardiologist", "lead_cardiologist"])))))
        if n >= pol.get("max_reader_seats", 10**6):
            raise ValueError(f"All {pol['max_reader_seats']} annotator seats are in use; deactivate a reader or request more seats")


def active_run(db: Session) -> LabelRun | None:
    return db.scalar(select(LabelRun).where(LabelRun.status == "active").order_by(LabelRun.created_at.desc()).limit(1))
