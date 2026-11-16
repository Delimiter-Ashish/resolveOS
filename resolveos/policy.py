"""Policy engine: single source of truth for what an autonomous agent may do.

evaluate() returns ALLOW, REQUIRE_APPROVAL or DENY with a stable rule id and reason.
It is used twice: (1) the agent's pre-execution gate (dry-run via /v1/policy/check)
and (2) hard enforcement inside every mutating endpoint, so a misbehaving agent can
never bypass policy by skipping the gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .common import D
from .config import settings

ALLOW = "ALLOW"
REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
DENY = "DENY"


@dataclass
class Decision:
    effect: str
    rule: str
    reason: str
    base_effect: str = ""
    base_rule: str = ""

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def _allow(rule="default_allow", reason="within agent authority") -> Decision:
    return Decision(ALLOW, rule, reason)


def _deny(rule: str, reason: str) -> Decision:
    return Decision(DENY, rule, reason)


def _approval(rule: str, reason: str) -> Decision:
    return Decision(REQUIRE_APPROVAL, rule, reason)


def _one(c, sql: str, params: tuple):
    return c.execute(sql, params).fetchone()


def evaluate(c, action: str, args: dict[str, Any], org_id: str = "default") -> Decision:
    """Built-in policy only."""
    if action.startswith("connector."):
        return _connector(c, action, org_id)
    fn = _RULES.get(action)
    if fn is None:
        return _deny("unknown_action", f"unknown action {action}")
    return fn(c, args)


def _connector(c, action: str, org_id: str) -> Decision:
    try:
        _, name, op = action.split(".", 2)
    except ValueError:
        return _deny("connector.bad_action", "malformed connector action")
    row = c.execute("SELECT operations, enabled FROM connectors WHERE org_id=%s AND name=%s", (org_id, name)).fetchone()
    if not row or not row["enabled"]:
        return _deny("connector.not_found", f"connector '{name}' is not registered or disabled")
    spec = (row["operations"] or {}).get(op)
    if not spec:
        return _deny("connector.op_not_found", f"operation '{op}' is not exposed by connector '{name}'")
    if spec.get("method", "GET").upper() == "GET":
        return _allow("connector.read", "read-only connector call")
    return _approval("connector.default_write", "writes to external systems need approval unless a policy rule allows them")


WRITE_EXEMPT = ("ticket.resolve", "ticket.escalate")


def decide(c, action: str, args: dict[str, Any], org_id: str = "default", workflow_id: str | None = None) -> Decision:
    """Full decision = built-in policy -> custom rules -> guardrail lock -> autonomy level."""
    from . import autonomy, rules

    base = evaluate(c, action, args, org_id)
    if base.effect == DENY or action in WRITE_EXEMPT:
        return Decision(base.effect, base.rule, base.reason, base.effect, base.rule)
    is_read = base.rule == "connector.read"
    effect, rule, reason = rules.apply(rules.load(c, org_id), action, args, base.effect, base.rule,
                                       relaxable=action.startswith("connector."))
    reason = reason or base.reason
    if effect != ALLOW or is_read:
        return Decision(effect, rule, reason, base.effect, base.rule)
    if workflow_id:
        wf = c.execute("SELECT flags FROM workflows WHERE id=%s", (workflow_id,)).fetchone()
        if wf and (wf["flags"] or {}).get("injection"):
            return Decision(REQUIRE_APPROVAL, "guardrail.injection",
                            "ticket contains prompt-injection signals; every action needs a human", base.effect, base.rule)
    s = autonomy.org_settings(c, org_id)
    if s["autonomy_mode"] == "shadow":
        return Decision(REQUIRE_APPROVAL, "autonomy.shadow", "shadow mode: the agent proposes, a human decides",
                        base.effect, base.rule)
    if s["autonomy_mode"] == "progressive":
        t = autonomy.trust(c, org_id, action, s)
        if not t["trusted"]:
            return Decision(REQUIRE_APPROVAL, "autonomy.learning",
                            f"earning trust: {t['approved']}/{t['reviews']} approved, needs "
                            f"{s['trust_min_samples']} reviews at {s['trust_threshold']:.0%}", base.effect, base.rule)
        return Decision(ALLOW, "autonomy.trusted", f"trusted after {t['reviews']} reviews ({t['approval_rate']:.0%})",
                        base.effect, base.rule)
    return Decision(effect, rule, reason, base.effect, base.rule)


# ---- refunds -------------------------------------------------------------
def _refund(c, a) -> Decision:
    o = _one(
        c,
        """SELECT o.*, cu.fraud_flag,
                  EXTRACT(EPOCH FROM (now() - COALESCE(o.delivered_at, o.placed_at)))/86400.0 AS age_days
           FROM orders o JOIN customers cu ON cu.id = o.customer_id WHERE o.id=%s""",
        (a.get("order_id"),),
    )
    if not o:
        return _deny("refund.order_not_found", "order does not exist")
    amount = D(a.get("amount", 0))
    if amount <= 0:
        return _deny("refund.invalid_amount", "refund amount must be positive")
    if o["fraud_flag"]:
        return _deny("refund.fraud_hold", "customer is under fraud review; refunds are blocked")
    if o["status"] != "delivered":
        return _deny("refund.not_delivered", f"order status is '{o['status']}'; only delivered orders can be refunded")
    if o["age_days"] > settings.refund_window_days:
        return _deny("refund.window_expired", f"delivered {int(o['age_days'])} days ago; window is {settings.refund_window_days} days")
    remaining = D(o["total"]) - D(o["refunded"])
    if amount > remaining:
        return _deny("refund.exceeds_balance", f"only {remaining} remains refundable on this order")
    if amount > D(settings.refund_auto_limit):
        return _approval("refund.over_limit", f"refunds above {settings.refund_auto_limit:.2f} need human approval")
    return _allow("refund.auto")


# ---- orders --------------------------------------------------------------
def _cancel(c, a) -> Decision:
    o = _one(c, "SELECT status FROM orders WHERE id=%s", (a.get("order_id"),))
    if not o:
        return _deny("order.not_found", "order does not exist")
    if o["status"] in ("placed", "processing"):
        return _allow("order.cancel_ok")
    return _deny("order.cancel_not_allowed", f"order is already '{o['status']}' and cannot be cancelled")


def _address(c, a) -> Decision:
    o = _one(c, "SELECT status FROM orders WHERE id=%s", (a.get("order_id"),))
    if not o:
        return _deny("order.not_found", "order does not exist")
    if not (a.get("new_address") or "").strip():
        return _deny("order.address_empty", "new address is empty")
    if o["status"] in ("placed", "processing"):
        return _allow("order.address_ok")
    return _deny("order.address_locked", f"order is already '{o['status']}'; address can no longer be changed")


# ---- inventory -----------------------------------------------------------
def _po(c, a) -> Decision:
    inv = _one(c, "SELECT * FROM inventory WHERE sku=%s", (a.get("sku"),))
    if not inv:
        return _deny("po.sku_not_found", "SKU does not exist")
    qty = int(a.get("quantity", 0))
    if qty <= 0 or qty > settings.po_max_quantity:
        return _deny("po.invalid_quantity", f"quantity must be between 1 and {settings.po_max_quantity}")
    if inv["discontinued"]:
        return _deny("po.discontinued", "SKU is discontinued; no purchase orders allowed")
    if inv["on_hand"] >= inv["reorder_point"]:
        return _deny("po.above_reorder_point", f"on-hand {inv['on_hand']} is not below reorder point {inv['reorder_point']}")
    if D(inv["unit_cost"]) * qty > D(settings.po_auto_limit):
        return _approval("po.over_limit", f"purchase orders above {settings.po_auto_limit:.2f} need human approval")
    return _allow("po.auto")


# ---- accounts ------------------------------------------------------------
def _unlock(c, a) -> Decision:
    acc = _one(c, "SELECT * FROM accounts WHERE id=%s", (a.get("account_id"),))
    if not acc:
        return _deny("account.not_found", "account does not exist")
    if not acc["locked"]:
        return _deny("account.not_locked", "account is not locked")
    if acc["role"] == "admin":
        return _approval("account.privileged", "unlocking a privileged account needs human approval")
    return _allow("account.unlock_ok")


def _reset(c, a) -> Decision:
    acc = _one(c, "SELECT * FROM accounts WHERE id=%s", (a.get("account_id"),))
    if not acc:
        return _deny("account.not_found", "account does not exist")
    if not acc["identity_verified"]:
        return _deny("account.identity_unverified", "identity has not been verified for this account")
    if acc["role"] == "admin":
        return _approval("account.privileged", "resetting a privileged account needs human approval")
    return _allow("account.reset_ok")


# ---- credits / shipments ---------------------------------------------------
def _credit(c, a) -> Decision:
    cu = _one(c, "SELECT fraud_flag FROM customers WHERE id=%s", (a.get("customer_id"),))
    if not cu:
        return _deny("credit.customer_not_found", "customer does not exist")
    amount = D(a.get("amount", 0))
    if amount <= 0:
        return _deny("credit.invalid_amount", "credit must be positive")
    if cu["fraud_flag"]:
        return _deny("credit.fraud_hold", "customer is under fraud review")
    if amount > D(settings.credit_max):
        return _deny("credit.exceeds_max", f"credits above {settings.credit_max:.2f} are never allowed")
    if amount > D(settings.credit_auto_limit):
        return _approval("credit.over_limit", f"credits above {settings.credit_auto_limit:.2f} need human approval")
    return _allow("credit.auto")


def _reship(c, a) -> Decision:
    s = _one(
        c,
        """SELECT s.status, s.days_delayed, o.total FROM shipments s
           JOIN orders o ON o.id = s.order_id WHERE s.order_id=%s""",
        (a.get("order_id"),),
    )
    if not s:
        return _deny("reship.no_shipment", "no shipment found for this order")
    if _one(c, "SELECT 1 AS x FROM reships WHERE order_id=%s", (a.get("order_id"),)):
        return _deny("reship.already_done", "this order was already reshipped")
    eligible = s["status"] == "lost" or (s["status"] == "delayed" and s["days_delayed"] >= 5)
    if not eligible:
        return _deny("reship.not_eligible", f"shipment is '{s['status']}' ({s['days_delayed']} days delayed); not eligible for reship")
    if D(s["total"]) > D(settings.reship_approval_value):
        return _approval("reship.high_value", f"reshipping orders above {settings.reship_approval_value:.2f} needs approval")
    return _allow("reship.ok")


def _always(c, a) -> Decision:
    return _allow("ticket.close")


_RULES = {
    "refund.issue": _refund,
    "order.cancel": _cancel,
    "order.update_address": _address,
    "inventory.purchase_order": _po,
    "account.unlock": _unlock,
    "account.reset_credentials": _reset,
    "credit.issue": _credit,
    "shipment.reship": _reship,
    "ticket.resolve": _always,
    "ticket.escalate": _always,
}
