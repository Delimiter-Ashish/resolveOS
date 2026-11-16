"""Unit tests for the product layer (no DB / LLM needed)."""
from resolveos import guardrails, rules
from resolveos.connectors.demo_crm import crm
from resolveos.connectors.openapi import parse
from resolveos.eval.scenarios import generate, generate_adversarial


def test_card_redaction_luhn_only():
    t, pii = guardrails.redact("card 4111 1111 1111 1111 and order 1234567890123")
    assert "4111 1111" not in t and "****1111" in t and pii == ["payment_card"]
    t2, pii2 = guardrails.redact("tracking 1234567890123456")  # fails Luhn -> untouched
    assert "1234567890123456" in t2 and pii2 == []


def test_ssn_redaction():
    t, pii = guardrails.redact("SSN 123-45-6789")
    assert "123-45-6789" not in t and "ssn" in pii


def test_injection_detected_and_no_false_positives_on_benchmark():
    assert all(guardrails.scan(s.subject, s.body)["injection"] for s in generate_adversarial() if s.category == "adversarial_injection")
    assert not any(guardrails.scan(s.subject, s.body)["injection"] for s in generate(7))


def test_rules_tighten_but_never_loosen_builtin():
    r = [{**rules.validate({"action": "refund.issue", "when": [{"field": "amount", "op": ">", "value": 50}],
                           "effect": "REQUIRE_APPROVAL"}), "_name": "big"}]
    assert rules.apply(r, "refund.issue", {"amount": 80}, "ALLOW", "x", False)[0] == "REQUIRE_APPROVAL"
    assert rules.apply(r, "refund.issue", {"amount": 20}, "ALLOW", "x", False)[0] == "ALLOW"
    loosen = [{**rules.validate({"action": "refund.issue", "effect": "ALLOW"}), "_name": "loose"}]
    assert rules.apply(loosen, "refund.issue", {"amount": 500}, "REQUIRE_APPROVAL", "x", False)[0] == "REQUIRE_APPROVAL"
    assert rules.apply(loosen, "refund.issue", {"amount": 500}, "REQUIRE_APPROVAL", "x", True)[0] == "ALLOW"


def test_heuristic_compiler():
    r = rules._heuristic("Never issue store credit above $150")
    assert r["action"] == "credit.issue" and r["effect"] == "DENY" and r["when"][0]["value"] == 150


def test_openapi_parse_demo_crm():
    ops = parse(crm.openapi())
    assert set(ops) == {"get_crm_profile", "add_crm_note", "set_loyalty_tier", "create_callback_task"}
    assert ops["add_crm_note"]["method"] == "POST" and "text" in ops["add_crm_note"]["schema"]["required"]
