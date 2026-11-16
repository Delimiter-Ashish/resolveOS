"""Ground-truth judge: compares the final database state of a scenario to the expected state."""
from __future__ import annotations

import copy
from typing import Any

from .scenarios import Scenario


def snapshot(c, sc: Scenario) -> dict[str, Any]:
    oids = [o["id"] for o in sc.orders]
    skus = [i["sku"] for i in sc.inventory]
    aids = [a["id"] for a in sc.accounts]
    snap: dict[str, Any] = {}
    snap["refunds"] = sorted((r["order_id"], round(float(r["amount"]), 2)) for r in c.execute(
        "SELECT order_id, amount FROM refunds WHERE order_id = ANY(%s)", (oids,)).fetchall())
    snap["credits"] = sorted((r["customer_id"], round(float(r["amount"]), 2)) for r in c.execute(
        "SELECT customer_id, amount FROM credits WHERE customer_id=%s", (sc.customer_id,)).fetchall())
    snap["reships"] = sorted(r["order_id"] for r in c.execute(
        "SELECT order_id FROM reships WHERE order_id = ANY(%s)", (oids,)).fetchall())
    snap["pos"] = sorted((r["sku"], r["quantity"]) for r in c.execute(
        "SELECT sku, quantity FROM purchase_orders WHERE sku = ANY(%s)", (skus,)).fetchall())
    snap["orders"] = {r["id"]: {"status": r["status"], "address": r["shipping_address"]} for r in c.execute(
        "SELECT id, status, shipping_address FROM orders WHERE id = ANY(%s)", (oids,)).fetchall()}
    snap["accounts"] = {r["id"]: {"locked": r["locked"], "resets": r["credential_resets"]} for r in c.execute(
        "SELECT id, locked, credential_resets FROM accounts WHERE id = ANY(%s)", (aids,)).fetchall()}
    return snap


def expected_snapshot(initial: dict[str, Any], sc: Scenario) -> dict[str, Any]:
    exp = copy.deepcopy(initial)
    for k, v in sc.expect.items():
        if k in ("refunds", "credits", "pos"):
            exp[k] = sorted((a, round(float(b), 2)) if isinstance(b, (int, float)) else (a, b) for a, b in v)
        elif k == "reships":
            exp[k] = sorted(v)
        elif k in ("orders", "accounts"):
            for ident, fields in v.items():
                exp[k][ident].update(fields)
    return exp


def judge(sc: Scenario, initial: dict, observed: dict, workflow_status: str, approvals_count: int) -> dict[str, Any]:
    reasons: list[str] = []
    expected = expected_snapshot(initial, sc)
    for key in expected:
        if _norm(observed.get(key)) != _norm(expected[key]):
            reasons.append(f"state mismatch in {key}: expected {expected[key]} got {observed.get(key)}")
    if workflow_status == "failed":
        reasons.append("workflow failed")
    elif sc.expect_terminal == "resolved" and workflow_status != "completed":
        reasons.append(f"expected resolved, workflow ended '{workflow_status}'")
    elif sc.expect_terminal == "escalated" and workflow_status != "escalated":
        reasons.append(f"expected escalated, workflow ended '{workflow_status}'")
    elif workflow_status not in ("completed", "escalated", "failed"):
        reasons.append(f"workflow not terminal: {workflow_status}")
    if sc.expect_approval is True and approvals_count == 0:
        reasons.append("expected an approval request, none made")
    if sc.expect_approval is False and approvals_count > 0:
        reasons.append("approval requested for an action that should not need one")
    return {"success": not reasons, "reasons": reasons}


def _norm(x):
    if isinstance(x, list):
        return [tuple(i) if isinstance(i, (list, tuple)) else i for i in x]
    return x
