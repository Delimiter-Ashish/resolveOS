"""The transactional core every mutating endpoint goes through:

    authenticate -> idempotency lookup -> policy enforcement -> approval consumption
    -> business handler -> store response -> audit   (all in ONE database transaction)

so a mutation and its idempotency record commit atomically: a retried request after a
lost response (or a worker crash between "side-effect done" and "checkpoint saved")
replays the stored response instead of executing twice.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .. import audit, policy
from ..auth import Principal, org_of_workflow
from ..common import action_hash, canonical, sha
from ..config import settings
from ..db import conn


class PolicyBlocked(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        self.status, self.body = status, body


@dataclass
class Chaos:
    mode: str = ""
    n: int = 1
    secs: float = 0.0


def parse_chaos(header: str | None) -> Chaos:
    if not header or not settings.chaos_enabled:
        return Chaos()
    mode, _, arg = header.partition(":")
    if mode == "slow":
        return Chaos("slow", 1, float(arg or 2))
    return Chaos(mode, int(arg or 1), settings.tool_http_timeout_s + 1.5)


def _chaos_should_fire(key: str, n: int) -> bool:
    with conn() as c:
        row = c.execute(
            """INSERT INTO chaos_hits(key, hits) VALUES (%s,1)
               ON CONFLICT (key) DO UPDATE SET hits = chaos_hits.hits + 1 RETURNING hits""",
            (key,),
        ).fetchone()
    return row["hits"] <= n


def _enforce(c, principal: Principal, action: str, args: dict, approval_id: str | None, wf: str | None) -> dict:
    """Raise PolicyBlocked unless the action may proceed. Returns audit info."""
    org = org_of_workflow(c, wf, principal.org)
    dec = policy.decide(c, action, args, org, wf)
    if dec.effect == policy.DENY:
        raise PolicyBlocked(403, {"error": "policy_denied", **dec.dict()})
    if dec.effect == policy.ALLOW:
        return {"policy": dec.dict(), "approval_id": None}

    # REQUIRE_APPROVAL: a valid, unconsumed, matching approval must be presented.
    if not approval_id:
        raise PolicyBlocked(403, {"error": "approval_required", **dec.dict()})
    ap = c.execute(
        "SELECT * FROM approvals WHERE id=%s FOR UPDATE", (approval_id,)
    ).fetchone()
    problem = None
    if not ap:
        problem = "approval_not_found"
    elif ap["status"] != "approved":
        problem = f"approval_{ap['status']}"
    elif ap["consumed_at"] is not None:
        problem = "approval_already_used"
    elif ap["workflow_id"] != wf:
        problem = "approval_workflow_mismatch"
    elif ap["action_hash"] != action_hash(action, args):
        problem = "approval_action_mismatch"
    if problem:
        raise PolicyBlocked(403, {"error": problem, **dec.dict()})
    c.execute("UPDATE approvals SET consumed_at = now() WHERE id=%s", (approval_id,))
    return {"policy": dec.dict(), "approval_id": approval_id}


def run_mutation(
    request: Request,
    principal: Principal,
    action: str,
    args: dict[str, Any],
    handler: Callable[[Any, dict[str, Any], dict], tuple[int, dict[str, Any]]],
    approval_id: str | None = None,
) -> JSONResponse:
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise HTTPException(400, {"error": "idempotency_key_required"})
    wf = request.headers.get("X-Workflow-Id")
    chaos = parse_chaos(request.headers.get("X-Chaos"))
    req_hash = sha(canonical({"m": request.method, "p": request.url.path, "a": args, "ap": approval_id}))

    if chaos.mode == "fail_before" and _chaos_should_fire(f"{principal.name}:{key}:fb", chaos.n):
        audit.safe_log(wf, "chaos", "chaos.fail_before", {"path": request.url.path})
        raise HTTPException(503, {"error": "injected_fault", "mode": "fail_before"})
    if chaos.mode == "slow" and _chaos_should_fire(f"{principal.name}:{key}:sl", 1):
        time.sleep(chaos.secs)

    blocked: PolicyBlocked | None = None
    replayed = False
    try:
        with conn() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"idem:{principal.name}:{key}",))
            row = c.execute(
                "SELECT request_hash, status_code, response FROM idempotency_keys WHERE principal=%s AND key=%s",
                (principal.name, key),
            ).fetchone()
            if row:
                if row["request_hash"] != req_hash:
                    raise HTTPException(
                        422, {"error": "idempotency_key_reuse", "detail": "same key, different request"}
                    )
                status, body, replayed = row["status_code"], row["response"], True
            else:
                info = _enforce(c, principal, action, args, approval_id, wf)
                status, body = handler(c, args, {"workflow_id": wf, "approval_id": info["approval_id"], "principal": principal.name})
                body = jsonable_encoder(body)
                c.execute(
                    """INSERT INTO idempotency_keys(principal, key, request_hash, status_code, response)
                       VALUES (%s,%s,%s,%s,%s::jsonb)""",
                    (principal.name, key, req_hash, status, canonical(body)),
                )
                audit.log(
                    c, wf, principal.name, f"{action}.committed",
                    {"args": args, "result": body, "policy": info["policy"], "approval_id": info["approval_id"]},
                )
    except PolicyBlocked as e:
        blocked = e
    if blocked:
        audit.safe_log(
            wf, principal.name, f"{action}.blocked",
            {"args": args, "status": blocked.status, **blocked.body},
        )
        raise HTTPException(blocked.status, blocked.body)

    if replayed:
        audit.safe_log(wf, principal.name, f"{action}.replayed", {"key": key})

    if chaos.mode == "fail_after" and _chaos_should_fire(f"{principal.name}:{key}:fa", chaos.n):
        audit.safe_log(wf, "chaos", "chaos.fail_after_commit", {"path": request.url.path})
        raise HTTPException(503, {"error": "injected_fault", "mode": "fail_after"})
    if chaos.mode == "timeout_after" and _chaos_should_fire(f"{principal.name}:{key}:ta", chaos.n):
        audit.safe_log(wf, "chaos", "chaos.timeout_after_commit", {"path": request.url.path})
        time.sleep(chaos.secs)

    return JSONResponse(body, status_code=status, headers={"Idempotent-Replay": str(replayed).lower()})
