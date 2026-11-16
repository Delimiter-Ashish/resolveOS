"""ResolveOS REST API (FastAPI). Run: uvicorn resolveos.api.app:app --port 8080"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from .. import audit, policy
from ..auth import Principal, org_of_workflow, require
from ..common import action_hash, normalize_args
from ..config import settings
from ..db import conn
from .mutation import run_mutation

app = FastAPI(title="ResolveOS", version="1.1")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@app.get("/healthz")
def healthz():
    with conn() as c:
        c.execute("SELECT 1")
    return {"ok": True}


# =========================== read endpoints ===============================
def _get(table: str, key: str, value: str, what: str):
    with conn() as c:
        row = c.execute(f"SELECT * FROM {table} WHERE {key}=%s", (value,)).fetchone()
    if not row:
        raise HTTPException(404, {"error": f"{what}_not_found", "id": value})
    return jsonable_encoder(row)


@app.get("/v1/customers/{customer_id}")
def get_customer(customer_id: str, _: Principal = Depends(require("read"))):
    return _get("customers", "id", customer_id, "customer")


@app.get("/v1/orders/{order_id}")
def get_order(order_id: str, _: Principal = Depends(require("read"))):
    o = _get("orders", "id", order_id, "order")
    o["refundable_remaining"] = round(float(o["total"]) - float(o["refunded"]), 2)
    return o


@app.get("/v1/orders")
def list_orders(customer_id: str = Query(...), _: Principal = Depends(require("read"))):
    with conn() as c:
        rows = c.execute("SELECT * FROM orders WHERE customer_id=%s ORDER BY placed_at", (customer_id,)).fetchall()
    return {"orders": jsonable_encoder(rows)}


@app.get("/v1/shipments/{order_id}")
def get_shipment(order_id: str, _: Principal = Depends(require("read"))):
    return _get("shipments", "order_id", order_id, "shipment")


@app.get("/v1/inventory/{sku}")
def get_inventory(sku: str, _: Principal = Depends(require("read"))):
    return _get("inventory", "sku", sku, "sku")


@app.get("/v1/accounts/{account_id}")
def get_account(account_id: str, _: Principal = Depends(require("read"))):
    return _get("accounts", "id", account_id, "account")


@app.get("/v1/refunds")
def list_refunds(order_id: str = Query(...), _: Principal = Depends(require("read"))):
    with conn() as c:
        rows = c.execute("SELECT * FROM refunds WHERE order_id=%s ORDER BY created_at", (order_id,)).fetchall()
    return {"refunds": jsonable_encoder(rows)}


# ========================= mutating endpoints =============================
class RefundIn(BaseModel):
    order_id: str
    amount: float = Field(gt=0)
    reason: str = ""
    approval_id: str | None = None


@app.post("/v1/refunds")
def issue_refund(body: RefundIn, request: Request, p: Principal = Depends(require("write"))):
    args = {"order_id": body.order_id, "amount": body.amount}

    def handler(c, a, ctx):
        rid = _id("rf")
        c.execute(
            "INSERT INTO refunds(id, order_id, amount, reason, workflow_id, approval_id) VALUES (%s,%s,%s,%s,%s,%s)",
            (rid, body.order_id, body.amount, body.reason, ctx["workflow_id"], ctx["approval_id"]),
        )
        c.execute("UPDATE orders SET refunded = refunded + %s WHERE id=%s", (body.amount, body.order_id))
        return 201, {"refund_id": rid, "order_id": body.order_id, "amount": body.amount, "status": "issued"}

    return run_mutation(request, p, "refund.issue", args, handler, body.approval_id)


class OrderActionIn(BaseModel):
    reason: str = ""
    approval_id: str | None = None


@app.post("/v1/orders/{order_id}/cancel")
def cancel_order(order_id: str, body: OrderActionIn, request: Request, p: Principal = Depends(require("write"))):
    def handler(c, a, ctx):
        c.execute("UPDATE orders SET status='cancelled' WHERE id=%s", (order_id,))
        return 200, {"order_id": order_id, "status": "cancelled"}

    return run_mutation(request, p, "order.cancel", {"order_id": order_id}, handler, body.approval_id)


class AddressIn(BaseModel):
    new_address: str
    approval_id: str | None = None


@app.post("/v1/orders/{order_id}/address")
def update_address(order_id: str, body: AddressIn, request: Request, p: Principal = Depends(require("write"))):
    def handler(c, a, ctx):
        c.execute("UPDATE orders SET shipping_address=%s WHERE id=%s", (body.new_address.strip(), order_id))
        return 200, {"order_id": order_id, "shipping_address": body.new_address.strip()}

    return run_mutation(
        request, p, "order.update_address",
        {"order_id": order_id, "new_address": body.new_address}, handler, body.approval_id,
    )


class POIn(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    reason: str = ""
    approval_id: str | None = None


@app.post("/v1/purchase-orders")
def create_po(body: POIn, request: Request, p: Principal = Depends(require("write"))):
    def handler(c, a, ctx):
        inv = c.execute("SELECT unit_cost FROM inventory WHERE sku=%s", (body.sku,)).fetchone()
        pid = _id("po")
        total = float(inv["unit_cost"]) * body.quantity
        c.execute(
            "INSERT INTO purchase_orders(id, sku, quantity, total, reason, workflow_id, approval_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (pid, body.sku, body.quantity, total, body.reason, ctx["workflow_id"], ctx["approval_id"]),
        )
        return 201, {"purchase_order_id": pid, "sku": body.sku, "quantity": body.quantity, "total": round(total, 2)}

    return run_mutation(request, p, "inventory.purchase_order", {"sku": body.sku, "quantity": body.quantity}, handler, body.approval_id)


class AccountActionIn(BaseModel):
    approval_id: str | None = None


@app.post("/v1/accounts/{account_id}/unlock")
def unlock_account(account_id: str, body: AccountActionIn, request: Request, p: Principal = Depends(require("write"))):
    def handler(c, a, ctx):
        c.execute("UPDATE accounts SET locked=FALSE WHERE id=%s", (account_id,))
        return 200, {"account_id": account_id, "locked": False}

    return run_mutation(request, p, "account.unlock", {"account_id": account_id}, handler, body.approval_id)


@app.post("/v1/accounts/{account_id}/reset-credentials")
def reset_credentials(account_id: str, body: AccountActionIn, request: Request, p: Principal = Depends(require("write"))):
    def handler(c, a, ctx):
        c.execute("UPDATE accounts SET credential_resets = credential_resets + 1 WHERE id=%s", (account_id,))
        return 200, {"account_id": account_id, "credentials_reset": True}

    return run_mutation(request, p, "account.reset_credentials", {"account_id": account_id}, handler, body.approval_id)


class CreditIn(BaseModel):
    customer_id: str
    amount: float = Field(gt=0)
    reason: str = ""
    approval_id: str | None = None


@app.post("/v1/credits")
def issue_credit(body: CreditIn, request: Request, p: Principal = Depends(require("write"))):
    def handler(c, a, ctx):
        cid = _id("cr")
        c.execute(
            "INSERT INTO credits(id, customer_id, amount, reason, workflow_id, approval_id) VALUES (%s,%s,%s,%s,%s,%s)",
            (cid, body.customer_id, body.amount, body.reason, ctx["workflow_id"], ctx["approval_id"]),
        )
        c.execute("UPDATE customers SET credit_balance = credit_balance + %s WHERE id=%s", (body.amount, body.customer_id))
        return 201, {"credit_id": cid, "customer_id": body.customer_id, "amount": body.amount}

    return run_mutation(request, p, "credit.issue", {"customer_id": body.customer_id, "amount": body.amount}, handler, body.approval_id)


@app.post("/v1/shipments/{order_id}/reship")
def reship(order_id: str, body: OrderActionIn, request: Request, p: Principal = Depends(require("write"))):
    def handler(c, a, ctx):
        rid = _id("rs")
        c.execute(
            "INSERT INTO reships(id, order_id, reason, workflow_id, approval_id) VALUES (%s,%s,%s,%s,%s)",
            (rid, order_id, body.reason, ctx["workflow_id"], ctx["approval_id"]),
        )
        return 201, {"reship_id": rid, "order_id": order_id, "status": "reship_created"}

    return run_mutation(request, p, "shipment.reship", {"order_id": order_id}, handler, body.approval_id)


class CloseIn(BaseModel):
    summary: str = ""


def _close(ticket_id: str, status: str, action: str, body: CloseIn, request: Request, p: Principal):
    def handler(c, a, ctx):
        n = c.execute(
            "UPDATE tickets SET status=%s, resolution=%s, closed_at=now() WHERE id=%s",
            (status, body.summary, ticket_id),
        ).rowcount
        if not n:
            raise HTTPException(404, {"error": "ticket_not_found"})
        return 200, {"ticket_id": ticket_id, "status": status}

    return run_mutation(request, p, action, {}, handler)


@app.post("/v1/tickets/{ticket_id}/resolve")
def resolve_ticket(ticket_id: str, body: CloseIn, request: Request, p: Principal = Depends(require("write"))):
    return _close(ticket_id, "resolved", "ticket.resolve", body, request, p)


@app.post("/v1/tickets/{ticket_id}/escalate")
def escalate_ticket(ticket_id: str, body: CloseIn, request: Request, p: Principal = Depends(require("write"))):
    return _close(ticket_id, "escalated", "ticket.escalate", body, request, p)


# ============================ policy + approvals ===========================
class PolicyCheckIn(BaseModel):
    action: str
    args: dict[str, Any]


@app.post("/v1/policy/check")
def policy_check(body: PolicyCheckIn, request: Request, p: Principal = Depends(require("policy:check"))):
    wf = request.headers.get("X-Workflow-Id")
    with conn() as c:
        dec = policy.decide(c, body.action, body.args, org_of_workflow(c, wf, p.org), wf)
    audit.safe_log(wf, p.name, "policy.checked", {"action": body.action, "args": body.args, **dec.dict()})
    return dec.dict()


class ApprovalIn(BaseModel):
    workflow_id: str
    action: str
    args: dict[str, Any]
    justification: str = ""


@app.post("/v1/approvals", status_code=201)
def request_approval(body: ApprovalIn, request: Request, p: Principal = Depends(require("approval:request"))):
    if body.action not in policy._RULES and not body.action.startswith("connector."):
        raise HTTPException(422, {"error": "unknown_action"})
    key = request.headers.get("Idempotency-Key") or _id("ak")
    aid = "ap_" + hashlib.sha256(key.encode()).hexdigest()[:16]
    args = normalize_args(body.action, body.args)
    with conn() as c:
        org = org_of_workflow(c, body.workflow_id, p.org)
        dec = policy.decide(c, body.action, body.args, org, body.workflow_id)
        if dec.effect != policy.REQUIRE_APPROVAL:
            raise HTTPException(409, {"error": "approval_not_required", **dec.dict()})
        created = c.execute(
            """INSERT INTO approvals(id, workflow_id, action, args, action_hash, justification, requested_by, expires_at,
                                     org_id, rule, base_effect)
               VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s, now() + make_interval(secs => %s), %s,%s,%s)
               ON CONFLICT (id) DO NOTHING RETURNING id""",
            (aid, body.workflow_id, body.action, json.dumps(args),
             action_hash(body.action, body.args), f"{dec.rule}: {dec.reason}", p.name, settings.approval_ttl_s,
             org, dec.rule, dec.base_effect),
        ).fetchone()
        row = c.execute("SELECT * FROM approvals WHERE id=%s", (aid,)).fetchone()
        if created:
            audit.log(c, body.workflow_id, p.name, "approval.requested",
                      {"approval_id": aid, "action": body.action, "args": args, **dec.dict()})
    if created:
        from ..notify import approval_requested

        approval_requested(jsonable_encoder(row))
    return jsonable_encoder(row)


@app.get("/v1/approvals")
def list_approvals(status: str = "pending", workflow_id: str | None = None,
                   _: Principal = Depends(require("approval:read"))):
    q, params = "SELECT * FROM approvals WHERE status=%s", [status]
    if workflow_id:
        q += " AND workflow_id=%s"
        params.append(workflow_id)
    with conn() as c:
        rows = c.execute(q + " ORDER BY created_at", params).fetchall()
    return {"approvals": jsonable_encoder(rows)}


@app.get("/v1/approvals/{approval_id}")
def get_approval(approval_id: str, _: Principal = Depends(require("approval:read"))):
    return _get("approvals", "id", approval_id, "approval")


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = ""


@app.post("/v1/approvals/{approval_id}/decision")
def decide(approval_id: str, body: DecisionIn, p: Principal = Depends(require("approval:decide"))):
    new_status = "approved" if body.decision == "approve" else "rejected"
    with conn() as c:
        ap = c.execute("SELECT * FROM approvals WHERE id=%s FOR UPDATE", (approval_id,)).fetchone()
        if not ap:
            raise HTTPException(404, {"error": "approval_not_found"})
        if ap["status"] != "pending":
            raise HTTPException(409, {"error": "already_decided", "status": ap["status"]})
        if ap["requested_by"] == p.name:
            raise HTTPException(403, {"error": "separation_of_duties"})
        if ap["org_id"] != p.org:
            raise HTTPException(403, {"error": "wrong_org"})
        c.execute(
            "UPDATE approvals SET status=%s, decided_by=%s, comment=%s, decided_at=now() WHERE id=%s",
            (new_status, p.name, body.comment, approval_id),
        )
        c.execute(
            """UPDATE workflows SET wake = TRUE,
                   status = CASE WHEN status='awaiting_approval' THEN 'queued' ELSE status END
               WHERE id=%s""",
            (ap["workflow_id"],),
        )
        audit.log(c, ap["workflow_id"], p.name, "approval.decided",
                  {"approval_id": approval_id, "decision": new_status, "comment": body.comment})
        _after_decision(c, ap, new_status, p.name, body.comment)
    return {"approval_id": approval_id, "status": new_status}


def _after_decision(c, ap, new_status: str, who: str, comment: str) -> None:
    """Learning loop: reviews of would-be-automatic actions build trust; rejections become regression tests."""
    from .. import autonomy
    from ..eval.regression import capture_case

    if (ap.get("rule") or "").startswith("autonomy."):  # only reviews of would-be-automatic actions count
        autonomy.record_review(c, ap["org_id"], ap["action"], new_status == "approved")
    if new_status == "rejected":
        capture_case(c, ap["workflow_id"], created_by=who,
                     reason=f"human rejected {ap['action']}: {comment}".strip(": "),
                     forbidden=[{"action": ap["action"], "args": ap["args"]}], terminal="any")


# ================================ workflows ================================
class WorkflowIn(BaseModel):
    customer_id: str
    subject: str
    body: str
    chaos: dict[str, Any] | None = None


@app.post("/v1/workflows", status_code=202)
def submit_workflow(body: WorkflowIn, p: Principal = Depends(require("workflow:submit"))):
    if body.chaos and not settings.chaos_enabled:
        raise HTTPException(400, {"error": "chaos_disabled"})
    from ..tickets import create_ticket

    with conn() as c:
        return create_ticket(c, org_id=p.org, customer_id=body.customer_id, subject=body.subject, body=body.body,
                             channel="api", chaos=body.chaos, submitted_by=p.name)


@app.get("/v1/workflows/{workflow_id}")
def get_workflow(workflow_id: str, _: Principal = Depends(require("workflow:read"))):
    with conn() as c:
        wf = c.execute("SELECT * FROM workflows WHERE id=%s", (workflow_id,)).fetchone()
        if not wf:
            raise HTTPException(404, {"error": "workflow_not_found"})
        aps = c.execute("SELECT * FROM approvals WHERE workflow_id=%s ORDER BY created_at", (workflow_id,)).fetchall()
        tk = c.execute("SELECT status, resolution FROM tickets WHERE id=%s", (wf["ticket_id"],)).fetchone()
    return jsonable_encoder({"workflow": wf, "approvals": aps, "ticket": tk})


@app.get("/v1/workflows/{workflow_id}/audit")
def get_audit(workflow_id: str, _: Principal = Depends(require("audit:read"))):
    with conn() as c:
        rows = c.execute(
            "SELECT id, ts_iso, actor, event, payload, prev_hash, hash FROM audit_log WHERE workflow_id=%s ORDER BY id",
            (workflow_id,),
        ).fetchall()
    return {"events": jsonable_encoder(rows)}


@app.get("/v1/workflows/{workflow_id}/audit/verify")
def verify_audit(workflow_id: str, _: Principal = Depends(require("audit:read"))):
    return audit.verify_chain(workflow_id)


# ---- operator console (live UI, event stream, demo lab) ----
from .console import router as console_router  # noqa: E402
from .insights import router as insights_router  # noqa: E402
from .product import router as product_router  # noqa: E402
from ..connectors.demo_crm import crm as _demo_crm  # noqa: E402

app.include_router(console_router)
app.include_router(product_router)
app.include_router(insights_router)
app.mount("/mock/crm", _demo_crm)
