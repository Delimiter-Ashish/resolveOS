"""Single entry point for creating tickets + workflows from any channel (API, email, Slack, chat, webhook).
Guardrails run here, so nothing un-redacted is ever stored and injection flags follow the workflow."""
from __future__ import annotations

import json
import uuid
from typing import Any

from . import audit, guardrails


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def create_ticket(c, *, org_id: str, customer_id: str, subject: str, body: str, channel: str = "api",
                  reply_to: str | None = None, chaos: dict | None = None, submitted_by: str = "system") -> dict[str, Any]:
    g = guardrails.scan(subject, body)
    meta = {k: g[k] for k in ("pii", "injection", "injection_score", "signals")}
    flags = {"injection": g["injection"], "pii": g["pii"]}
    wid, tid = new_id("wf"), new_id("tk")
    c.execute(
        """INSERT INTO tickets(id, customer_id, subject, body, workflow_id, org_id, channel, reply_to, guardrails)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (tid, customer_id, g["subject"], g["body"], wid, org_id, channel, reply_to, json.dumps(meta)),
    )
    c.execute(
        "INSERT INTO workflows(id, ticket_id, submitted_by, chaos, org_id, flags) VALUES (%s,%s,%s,%s::jsonb,%s,%s::jsonb)",
        (wid, tid, submitted_by, json.dumps(chaos or {}), org_id, json.dumps(flags)),
    )
    audit.log(c, wid, submitted_by, "workflow.submitted",
              {"ticket_id": tid, "customer_id": customer_id, "subject": g["subject"], "channel": channel})
    if g["pii"]:
        audit.log(c, wid, "guardrails", "guardrail.pii_redacted", {"types": g["pii"]})
    if g["injection"]:
        audit.log(c, wid, "guardrails", "guardrail.injection_detected",
                  {"score": g["injection_score"], "signals": g["signals"]})
    return {"workflow_id": wid, "ticket_id": tid, "status": "queued", "guardrails": meta}
