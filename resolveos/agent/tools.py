"""Tool registry. Each tool = validated args -> REST call. Write tools map to a policy action."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field


class CustomerArgs(BaseModel):
    customer_id: str = Field(description="Customer id, e.g. C012")


class OrderArgs(BaseModel):
    order_id: str = Field(description="Order id, e.g. O012A")


class SkuArgs(BaseModel):
    sku: str = Field(description="SKU, e.g. SKU-012")


class AccountArgs(BaseModel):
    account_id: str = Field(description="Account id, e.g. ACC-012")


class RefundArgs(BaseModel):
    order_id: str
    amount: float = Field(gt=0, description="Refund amount in USD")
    reason: str = Field(default="", description="Short reason")


class CancelArgs(BaseModel):
    order_id: str
    reason: str = ""


class AddressArgs(BaseModel):
    order_id: str
    new_address: str = Field(description="Full new shipping address")


class POArgs(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    reason: str = ""


class CreditArgs(BaseModel):
    customer_id: str
    amount: float = Field(gt=0, description="Store credit in USD")
    reason: str = ""


class ReshipArgs(BaseModel):
    order_id: str
    reason: str = ""


class KnowledgeArgs(BaseModel):
    query: str = Field(description="What to look up in the company knowledge base (policies, FAQs, procedures)")


class CloseArgs(BaseModel):
    summary: str = Field(description="Customer-facing summary of what was done or why not")


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel] | None
    kind: str  # read | write | terminal
    method: str
    path: Callable[[dict], str]
    action: str | None = None  # policy action
    query: Callable[[dict], dict] | None = None
    body: Callable[[dict], dict] | None = None
    terminal_outcome: str | None = None
    json_schema: dict | None = None  # for connector tools (no pydantic model)

    def validate(self, raw: dict) -> dict:
        if self.args is not None:
            return self.args(**raw).model_dump()
        sch = self.json_schema or {}
        props = sch.get("properties", {})
        missing = [r for r in sch.get("required", []) if raw.get(r) in (None, "")]
        if missing:
            raise ValueError("missing required: " + ", ".join(missing))
        return {k: v for k, v in raw.items() if k in props}

    def schema(self) -> dict[str, Any]:
        if self.args is None:
            return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                     "parameters": self.json_schema}}
        s = self.args.model_json_schema()
        s.pop("title", None)
        for p in s.get("properties", {}).values():
            p.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": s}}


def _q(*keys):
    return lambda a: {k: a[k] for k in keys}


def _b(*keys):
    return lambda a: {k: a[k] for k in keys if k in a}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_customer", "Look up a customer (tier, fraud flag, credit balance).", CustomerArgs, "read", "GET",
         lambda a: f"/v1/customers/{a['customer_id']}"),
    Tool("get_order", "Look up an order: status, total, amount already refunded, refundable_remaining, address, dates.",
         OrderArgs, "read", "GET", lambda a: f"/v1/orders/{a['order_id']}"),
    Tool("list_customer_orders", "List all orders for a customer.", CustomerArgs, "read", "GET",
         lambda a: "/v1/orders", query=_q("customer_id")),
    Tool("get_shipment", "Get shipment/tracking status for an order.", OrderArgs, "read", "GET",
         lambda a: f"/v1/shipments/{a['order_id']}"),
    Tool("get_inventory", "Get stock level, reorder point, unit cost and discontinued flag for a SKU.", SkuArgs, "read", "GET",
         lambda a: f"/v1/inventory/{a['sku']}"),
    Tool("get_account", "Get account state: locked, role, identity_verified.", AccountArgs, "read", "GET",
         lambda a: f"/v1/accounts/{a['account_id']}"),
    Tool("list_refunds", "List refunds already issued for an order.", OrderArgs, "read", "GET",
         lambda a: "/v1/refunds", query=_q("order_id")),
    Tool("search_knowledge", "Search the company knowledge base (policies, FAQs, procedures). Cite results as [kb:<id>].",
         KnowledgeArgs, "read", "GET", lambda a: "/v1/knowledge/search", query=_q("query")),

    Tool("issue_refund", "Refund money for a delivered order.", RefundArgs, "write", "POST",
         lambda a: "/v1/refunds", action="refund.issue", body=_b("order_id", "amount", "reason")),
    Tool("cancel_order", "Cancel an order that has not shipped yet.", CancelArgs, "write", "POST",
         lambda a: f"/v1/orders/{a['order_id']}/cancel", action="order.cancel", body=_b("reason")),
    Tool("update_shipping_address", "Change the shipping address of an order that has not shipped.", AddressArgs,
         "write", "POST", lambda a: f"/v1/orders/{a['order_id']}/address", action="order.update_address",
         body=_b("new_address")),
    Tool("create_purchase_order", "Create a supplier purchase order to restock a SKU.", POArgs, "write", "POST",
         lambda a: "/v1/purchase-orders", action="inventory.purchase_order", body=_b("sku", "quantity", "reason")),
    Tool("unlock_account", "Unlock a locked user account.", AccountArgs, "write", "POST",
         lambda a: f"/v1/accounts/{a['account_id']}/unlock", action="account.unlock", body=lambda a: {}),
    Tool("reset_credentials", "Reset credentials for an account whose identity is verified.", AccountArgs, "write", "POST",
         lambda a: f"/v1/accounts/{a['account_id']}/reset-credentials", action="account.reset_credentials",
         body=lambda a: {}),
    Tool("issue_credit", "Issue store credit to a customer (goodwill / delay compensation).", CreditArgs, "write", "POST",
         lambda a: "/v1/credits", action="credit.issue", body=_b("customer_id", "amount", "reason")),
    Tool("reship_order", "Send a replacement shipment for a lost or badly delayed order.", ReshipArgs, "write", "POST",
         lambda a: f"/v1/shipments/{a['order_id']}/reship", action="shipment.reship", body=_b("reason")),

    Tool("resolve_ticket", "FINISH: close the ticket as handled (including requests that policy forbids, with an explanation).",
         CloseArgs, "terminal", "POST", lambda a: "/v1/tickets/{ticket}/resolve", action="ticket.resolve",
         body=_b("summary"), terminal_outcome="resolved"),
    Tool("escalate_ticket", "FINISH: hand the ticket to a human because it needs judgment you cannot exercise.",
         CloseArgs, "terminal", "POST", lambda a: "/v1/tickets/{ticket}/escalate", action="ticket.escalate",
         body=_b("summary"), terminal_outcome="escalated"),
]}

TOOL_SCHEMAS = [t.schema() for t in TOOLS.values()]

# ------------------------------------------------------------- connector tools
_conn_cache: dict[str, tuple[float, dict[str, Tool]]] = {}


def connector_tools(org_id: str) -> dict[str, Tool]:
    import time

    hit = _conn_cache.get(org_id)
    if hit and time.time() - hit[0] < 2.0:
        return hit[1]
    from ..db import conn

    out: dict[str, Tool] = {}
    with conn() as c:
        rows = c.execute("SELECT name, operations FROM connectors WHERE org_id=%s AND enabled", (org_id,)).fetchall()
    for r in rows:
        cname = r["name"]
        for op_id, op in (r["operations"] or {}).items():
            tname = f"{cname}__{op_id}"[:64]
            kind = "read" if op["method"] == "GET" else "write"
            out[tname] = Tool(
                tname, f"[{cname}] {op['summary']}", None, kind, "POST",
                (lambda a, n=cname, k=kind: f"/v1/connectors/{n}/" + ("read" if k == "read" else "invoke")),
                action=f"connector.{cname}.{op_id}",
                body=(lambda a, o=op_id: {"operation": o, "args": a}),
                json_schema=op["schema"],
            )
    _conn_cache[org_id] = (time.time(), out)
    return out


def tools_for(org_id: str) -> dict[str, Tool]:
    return {**TOOLS, **connector_tools(org_id)}


def schemas_for(org_id: str) -> list[dict]:
    return [t.schema() for t in tools_for(org_id).values()]
