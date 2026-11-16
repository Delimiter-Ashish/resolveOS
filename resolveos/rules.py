"""Custom policy rules: a tiny, auditable DSL + natural-language compiler + backtesting.

Rule JSON:
  {"action": "refund.issue" | "connector.crm.*" | "*",
   "when":   [{"field": "amount", "op": ">", "value": 500}],   # all must match (AND)
   "effect": "ALLOW" | "REQUIRE_APPROVAL" | "DENY",
   "reason": "human readable"}

Rules can only TIGHTEN built-in policy for built-in actions (they can never grant what the
built-in safety rules deny). For connector actions they also decide the default (ALLOW a
safe write, for example).
"""
from __future__ import annotations

import fnmatch
import json
import re
import time
from typing import Any

from .common import HASH_FIELDS

OPS = {">", ">=", "<", "<=", "==", "!=", "contains", "in"}
EFFECTS = ("ALLOW", "REQUIRE_APPROVAL", "DENY")
RANK = {"ALLOW": 0, "REQUIRE_APPROVAL": 1, "DENY": 2}

ACTION_FIELDS: dict[str, list[str]] = {
    "refund.issue": ["order_id", "amount", "reason"],
    "credit.issue": ["customer_id", "amount", "reason"],
    "order.cancel": ["order_id"],
    "order.update_address": ["order_id", "new_address"],
    "inventory.purchase_order": ["sku", "quantity"],
    "account.unlock": ["account_id"],
    "account.reset_credentials": ["account_id"],
    "shipment.reship": ["order_id"],
}


class RuleError(ValueError):
    pass


def validate(rule: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise RuleError("rule must be an object")
    action = str(rule.get("action", "")).strip()
    if not action:
        raise RuleError("rule.action is required")
    if action not in ACTION_FIELDS and not action.startswith("connector.") and "*" not in action:
        raise RuleError(f"unknown action '{action}'")
    effect = str(rule.get("effect", "")).upper()
    if effect not in EFFECTS:
        raise RuleError("effect must be ALLOW, REQUIRE_APPROVAL or DENY")
    conds = []
    for c in rule.get("when") or []:
        op = str(c.get("op", "")).strip()
        if op not in OPS:
            raise RuleError(f"unsupported operator '{op}'")
        if not c.get("field"):
            raise RuleError("condition.field is required")
        conds.append({"field": str(c["field"]), "op": op, "value": c.get("value")})
    return {"action": action, "when": conds, "effect": effect,
            "reason": str(rule.get("reason") or "custom rule")[:200]}


def _cmp(actual: Any, op: str, value: Any) -> bool:
    if actual is None:
        return False
    try:
        if op in (">", ">=", "<", "<="):
            a, v = float(actual), float(value)
            return {">": a > v, ">=": a >= v, "<": a < v, "<=": a <= v}[op]
        if op == "==":
            return str(actual).lower() == str(value).lower()
        if op == "!=":
            return str(actual).lower() != str(value).lower()
        if op == "contains":
            return str(value).lower() in str(actual).lower()
        if op == "in":
            vals = value if isinstance(value, list) else [x.strip() for x in str(value).split(",")]
            return str(actual).lower() in [str(x).lower() for x in vals]
    except (TypeError, ValueError):
        return False
    return False


def matches(rule: dict, action: str, args: dict) -> bool:
    if not fnmatch.fnmatchcase(action, rule["action"]):
        return False
    return all(_cmp(args.get(c["field"]), c["op"], c["value"]) for c in rule.get("when", []))


_cache: dict[str, tuple[float, list[dict]]] = {}


def load(c, org_id: str) -> list[dict]:
    hit = _cache.get(org_id)
    if hit and time.time() - hit[0] < 2.0:
        return hit[1]
    rows = c.execute(
        "SELECT id, name, rule FROM policy_rules WHERE org_id=%s AND enabled ORDER BY created_at", (org_id,)
    ).fetchall()
    out = [{**r["rule"], "_id": r["id"], "_name": r["name"]} for r in rows]
    _cache[org_id] = (time.time(), out)
    return out


def invalidate(org_id: str) -> None:
    _cache.pop(org_id, None)


def apply(rules: list[dict], action: str, args: dict, base_effect: str, base_rule: str,
          relaxable: bool) -> tuple[str, str, str]:
    """Combine built-in decision with custom rules. Returns (effect, rule_id, reason)."""
    effect, rule_id, reason = base_effect, base_rule, ""
    for r in rules:
        if not matches(r, action, args):
            continue
        if RANK[r["effect"]] > RANK[effect]:
            effect, rule_id, reason = r["effect"], f"custom:{r['_name']}", r["reason"]
        elif relaxable and r["effect"] == "ALLOW" and effect == "REQUIRE_APPROVAL":
            effect, rule_id, reason = "ALLOW", f"custom:{r['_name']}", r["reason"]
    return effect, rule_id, reason


# ------------------------------------------------------------- NL compiler
COMPILE_PROMPT = """Convert the business policy sentence into ONE JSON rule. Output JSON only, no prose.
Schema: {"name": short_snake_case, "action": ACTION, "when": [{"field": F, "op": OP, "value": V}], "effect": EFFECT, "reason": text}
ACTION is one of: %s, or a connector action pattern like "connector.<name>.*".
Fields per action: %s
OP is one of > >= < <= == != contains in. EFFECT is ALLOW, REQUIRE_APPROVAL or DENY.
"needs approval / ask me / check with a manager" => REQUIRE_APPROVAL. "never / block / not allowed" => DENY.
Money values are numbers without currency symbols.
Policy: %s"""


def _heuristic(text: str) -> dict:
    t = text.lower()
    action = "refund.issue"
    for key, act in [("credit", "credit.issue"), ("cancel", "order.cancel"), ("address", "order.update_address"),
                     ("purchase order", "inventory.purchase_order"), ("restock", "inventory.purchase_order"),
                     ("unlock", "account.unlock"), ("reset", "account.reset_credentials"),
                     ("reship", "shipment.reship"), ("replacement", "shipment.reship"), ("refund", "refund.issue")]:
        if key in t:
            action = act
            break
    effect = "REQUIRE_APPROVAL"
    if re.search(r"\b(never|block|deny|not allowed|forbid|prohibit)\b", t):
        effect = "DENY"
    elif re.search(r"\b(auto(matically)?|always allow|no approval)\b", t):
        effect = "ALLOW"
    when = []
    m = re.search(r"(over|above|more than|greater than|exceeds?|>)\s*\$?\s*([\d,]+(?:\.\d+)?)", t)
    if m:
        when.append({"field": "quantity" if action == "inventory.purchase_order" else "amount", "op": ">",
                     "value": float(m.group(2).replace(",", ""))})
    m = re.search(r"(under|below|less than|<)\s*\$?\s*([\d,]+(?:\.\d+)?)", t)
    if m:
        when.append({"field": "amount", "op": "<", "value": float(m.group(2).replace(",", ""))})
    name = re.sub(r"[^a-z0-9]+", "_", t)[:40].strip("_") or "custom_rule"
    return {"name": name, "action": action, "when": when, "effect": effect, "reason": text.strip()[:200]}


def compile_nl(text: str) -> tuple[dict, str]:
    """Returns (rule, method). Uses the configured LLM; falls back to a deterministic parser."""
    from .agent.llm import complete_text

    prompt = COMPILE_PROMPT % (", ".join(ACTION_FIELDS), json.dumps(ACTION_FIELDS), text)
    try:
        out, _ = complete_text(prompt, purpose="policy_compile", json_mode=True)
        m = re.search(r"\{.*\}", out or "", re.S)
        if m:
            raw = json.loads(m.group(0))
            rule = validate(raw)
            return {**rule, "name": str(raw.get("name") or "custom_rule")[:60]}, "llm"
    except Exception:
        pass
    raw = _heuristic(text)
    return {**validate(raw), "name": raw["name"]}, "heuristic"


# ------------------------------------------------------------- backtest / what-if
def historical_actions(c, org_id: str, days: float) -> list[dict]:
    """Every write the agent proposed in the window (from the audit log), with its built-in verdict."""
    rows = c.execute(
        """SELECT a.workflow_id, a.payload FROM audit_log a
           JOIN workflows w ON w.id = a.workflow_id
           WHERE w.org_id=%s AND a.ts > now() - make_interval(days => %s) AND a.event = 'policy.checked'
           ORDER BY a.id""",
        (org_id, int(max(days, 1))),
    ).fetchall()
    out = []
    for r in rows:
        p = r["payload"]
        out.append({"workflow_id": r["workflow_id"], "action": p.get("action"), "args": p.get("args") or {},
                    "effect": p.get("effect"), "rule": p.get("rule"),
                    "base_effect": p.get("base_effect") or p.get("effect"), "base_rule": p.get("base_rule") or p.get("rule")})
    return out


def simulate(c, org_id: str, proposed: list[dict], days: float = 30, replace_existing: bool = False) -> dict:
    """What-if: re-decide every historical write under the current custom rules vs. current + proposed.
    Built-in denials can never be relaxed; runtime overlays (shadow mode, injection lock) are excluded."""
    current = [] if replace_existing else load(c, org_id)
    proposed = [{**validate(r), "_id": "proposed", "_name": r.get("name", "proposed")} for r in proposed]
    hist = historical_actions(c, org_id, days)
    changes, matrix = [], {}
    money = {"newly_gated": 0.0, "newly_denied": 0.0, "newly_allowed": 0.0}
    for h in hist:
        if not h["action"] or h["action"].startswith("ticket.") or h["base_effect"] not in RANK:
            continue
        relax = h["action"].startswith("connector.")
        before, _, _ = apply(current, h["action"], h["args"], h["base_effect"], h["base_rule"], relax)
        after, rid, reason = apply(current + proposed, h["action"], h["args"], h["base_effect"], h["base_rule"], relax)
        key = f"{before}->{after}"
        matrix[key] = matrix.get(key, 0) + 1
        if before != after:
            amt = float(h["args"].get("amount") or 0)
            bucket = {"DENY": "newly_denied", "REQUIRE_APPROVAL": "newly_gated", "ALLOW": "newly_allowed"}[after]
            money[bucket] += amt
            changes.append({"workflow_id": h["workflow_id"], "action": h["action"], "args": h["args"],
                            "before": before, "after": after, "rule": rid, "reason": reason})
    return {"window_days": days, "actions_considered": sum(matrix.values()), "matrix": matrix,
            "changed": len(changes), "money": {k: round(v, 2) for k, v in money.items()},
            "examples": changes[:25]}


def hash_fields_for(action: str) -> tuple[str, ...] | None:
    return HASH_FIELDS.get(action)
