"""Pure unit tests (no DB / LLM needed):  pytest -q"""
import math

from resolveos.agent.tools import TOOLS, TOOL_SCHEMAS
from resolveos.common import action_hash, normalize_args
from resolveos.eval.judge import expected_snapshot
from resolveos.eval.runner import pct
from resolveos.eval.scenarios import CATEGORIES, generate


def test_120_scenarios_balanced_and_unique():
    s = generate(7)
    assert len(s) == 120
    assert len({x.sid for x in s}) == 120
    for c in CATEGORIES:
        assert sum(1 for x in s if x.category == c) == 10


def test_generation_is_deterministic():
    a, b = generate(7), generate(7)
    assert [x.body for x in a] == [x.body for x in b]


def test_action_hash_binds_policy_fields_only():
    h1 = action_hash("refund.issue", {"order_id": "O1", "amount": 120, "reason": "a"})
    h2 = action_hash("refund.issue", {"order_id": "O1", "amount": "120.00", "reason": "different"})
    h3 = action_hash("refund.issue", {"order_id": "O1", "amount": 121})
    assert h1 == h2 and h1 != h3
    assert normalize_args("refund.issue", {"order_id": "O1", "amount": 5})["amount"] == "5.00"


def test_every_write_tool_maps_to_policy_action():
    from resolveos.policy import _RULES
    for t in TOOLS.values():
        if t.kind != "read":
            assert t.action in _RULES
    assert len(TOOL_SCHEMAS) == len(TOOLS)


def test_percentile():
    xs = list(range(1, 101))
    assert math.isclose(pct(xs, 0.95), 95.05)


def test_expected_snapshot_applies_overrides():
    sc = next(x for x in generate(7) if x.category == "cancel_order" and x.expect)
    oid = sc.orders[0]["id"]
    init = {"refunds": [], "credits": [], "reships": [], "pos": [],
            "orders": {oid: {"status": "placed", "address": "x"}}, "accounts": {}}
    assert expected_snapshot(init, sc)["orders"][oid]["status"] == "cancelled"
