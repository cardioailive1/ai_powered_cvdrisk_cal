"""Tamper-evident audit trail. Each event's hash covers its content and the previous event's hash, so any
edit, deletion or re-ordering breaks the chain (GET /api/audit/verify). Events are written in their own
transaction so that denied or failed actions are recorded even when the request itself fails.

Never put clinical values (inputs, results) in an event: record who touched which record, not its content."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

from fastapi import Request
from sqlalchemy import select, text

from .db import AuditEvent, SessionLocal, aware, engine, utcnow

log = logging.getLogger("cardioai.audit")
GENESIS = "0" * 64
_PG = engine.dialect.name == "postgresql"


def client_ip(request: Request | None) -> str:
    if request is None:
        return ""
    # Render's load balancer sets X-Forwarded-For; uvicorn --proxy-headers already applies it to request.client
    return (request.client.host if request.client else "")[:64]


def _canon_ts(t: dt.datetime) -> str:
    return aware(t).astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def compute_hash(prev: str, e: AuditEvent | dict) -> str:
    g = (lambda k: e[k]) if isinstance(e, dict) else (lambda k: getattr(e, k))
    payload = json.dumps({
        "prev": prev, "ts": _canon_ts(g("created_at")), "actor_id": g("actor_id"), "actor": g("actor"),
        "action": g("action"), "outcome": g("outcome"), "target_type": g("target_type"), "target_id": g("target_id"),
        "ip": g("ip"), "user_agent": g("user_agent"), "details": g("details") or {},
    }, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def record(action: str, outcome: str = "success", *, request: Request | None = None, actor=None,
           actor_label: str | None = None, target_type: str = "", target_id: str | int = "", **details) -> None:
    """Append an audit event. Raises if it cannot be written: an action that cannot be audited must not proceed."""
    ev = dict(
        created_at=utcnow(), actor_id=getattr(actor, "id", None),
        actor=(actor_label if actor_label is not None else getattr(actor, "username", ""))[:254],
        action=action, outcome=outcome, target_type=target_type, target_id=str(target_id)[:64],
        ip=client_ip(request), user_agent=((request.headers.get("user-agent", "") if request else "")[:255]),
        details=details,
    )
    with SessionLocal() as db, db.begin():
        if _PG:  # serialise writers across all instances so the chain stays linear
            db.execute(text("SELECT pg_advisory_xact_lock(7428301)"))
        prev = db.scalar(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1)) or GENESIS
        db.add(AuditEvent(**ev, prev_hash=prev, hash=compute_hash(prev, ev)))
    log.info("audit action=%s outcome=%s actor_id=%s target=%s:%s", action, outcome, ev["actor_id"], target_type, ev["target_id"])


def verify_chain(db) -> dict:
    prev, n = GENESIS, 0
    for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id).execution_options(yield_per=1000)):
        if e.prev_hash != prev or compute_hash(prev, e) != e.hash:
            return {"ok": False, "events_checked": n, "broken_at_id": e.id}
        prev, n = e.hash, n + 1
    return {"ok": True, "events_checked": n, "head": prev}


def to_dict(e: AuditEvent) -> dict:
    return {"id": e.id, "created_at": _canon_ts(e.created_at), "actor_id": e.actor_id, "actor": e.actor,
            "action": e.action, "outcome": e.outcome, "target_type": e.target_type, "target_id": e.target_id,
            "ip": e.ip, "user_agent": e.user_agent, "details": e.details, "hash": e.hash}
