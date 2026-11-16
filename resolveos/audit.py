"""Tamper-evident audit log: every row stores sha256(prev_hash || canonical body),
forming one hash chain per workflow. The table is also append-only via trigger."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder

from .common import sha
from .db import conn

GENESIS = "GENESIS"


def _body(workflow_id: str, actor: str, event: str, payload_text: str, ts_iso: str) -> str:
    return json.dumps(
        {"w": workflow_id, "a": actor, "e": event, "p": payload_text, "t": ts_iso}, sort_keys=True
    )


def log(c, workflow_id: str | None, actor: str, event: str, payload: dict[str, Any] | None = None) -> None:
    """Append an event using an existing transaction `c` (the chain lock is held until commit)."""
    wf = workflow_id or "_system"
    payload_text = json.dumps(jsonable_encoder(payload or {}), sort_keys=True, default=str)
    c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("audit:" + wf,))
    row = c.execute(
        "SELECT hash FROM audit_log WHERE workflow_id=%s ORDER BY id DESC LIMIT 1", (wf,)
    ).fetchone()
    prev = row["hash"] if row else GENESIS
    ts_iso = datetime.now(timezone.utc).isoformat()
    h = sha(prev + _body(wf, actor, event, payload_text, ts_iso))
    c.execute(
        """INSERT INTO audit_log(ts_iso, workflow_id, actor, event, payload, payload_text, prev_hash, hash)
           VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
        (ts_iso, wf, actor, event, payload_text, payload_text, prev, h),
    )


def log_standalone(workflow_id: str | None, actor: str, event: str, payload: dict[str, Any] | None = None) -> None:
    with conn() as c:
        log(c, workflow_id, actor, event, payload)


def safe_log(workflow_id: str | None, actor: str, event: str, payload: dict[str, Any] | None = None) -> None:
    """Audit must never break the business path."""
    try:
        log_standalone(workflow_id, actor, event, payload)
    except Exception:  # pragma: no cover
        pass


def verify_chain(workflow_id: str) -> dict[str, Any]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM audit_log WHERE workflow_id=%s ORDER BY id", (workflow_id,)
        ).fetchall()
    prev = GENESIS
    for i, r in enumerate(rows):
        if r["prev_hash"] != prev:
            return {"valid": False, "broken_at": r["id"], "events": len(rows)}
        expect = sha(prev + _body(r["workflow_id"], r["actor"], r["event"], r["payload_text"], r["ts_iso"]))
        if expect != r["hash"]:
            return {"valid": False, "broken_at": r["id"], "events": len(rows)}
        prev = r["hash"]
    return {"valid": True, "events": len(rows), "head": prev}
