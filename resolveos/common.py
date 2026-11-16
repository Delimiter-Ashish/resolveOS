"""Shared helpers used by both the API (server) and the agent (client)."""
from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# Policy-relevant fields per action: an approval is bound to exactly these.
HASH_FIELDS: dict[str, tuple[str, ...]] = {
    "refund.issue": ("order_id", "amount"),
    "order.cancel": ("order_id",),
    "order.update_address": ("order_id", "new_address"),
    "inventory.purchase_order": ("sku", "quantity"),
    "account.unlock": ("account_id",),
    "account.reset_credentials": ("account_id",),
    "credit.issue": ("customer_id", "amount"),
    "shipment.reship": ("order_id",),
    "ticket.resolve": (),
    "ticket.escalate": (),
}

MONEY_FIELDS = {"amount"}


def D(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_args(action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action not in HASH_FIELDS:  # connector actions: bind the approval to every argument
        return {k: (" ".join(v.split()) if isinstance(v, str) else v) for k, v in sorted((args or {}).items())}
    out: dict[str, Any] = {}
    for f in HASH_FIELDS[action]:
        v = args.get(f)
        if f in MONEY_FIELDS and v is not None:
            v = str(D(v))
        elif isinstance(v, str):
            v = " ".join(v.split())
        out[f] = v
    return out


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def action_hash(action: str, args: dict[str, Any]) -> str:
    return sha(canonical({"action": action, "args": normalize_args(action, args)}))
