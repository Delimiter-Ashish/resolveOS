"""LLM access with a cost-aware model router.

Providers (both OpenAI-compatible):
  primary - RESOLVEOS_LLM_*         (e.g. a local vLLM server; cheap/free)
  strong  - RESOLVEOS_STRONG_LLM_*  (e.g. Gemini via its OpenAI-compatible endpoint)

Routing (RESOLVEOS_ROUTER_MODE=auto): the primary model handles routine steps; the strong model is
used for tickets flagged by guardrails, complex multi-part tickets, and any step after the agent
hit an error, was nudged, or is deep into a run. If a provider fails, the other one is tried.
Every call is metered (tokens, latency, USD) into llm_usage.

`fake` mode is a tiny scripted agent used ONLY to smoke-test the platform plumbing without a GPU.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from ..config import settings


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    price_in: float
    price_out: float
    parallel_flag: bool  # vLLM accepts parallel_tool_calls; some hosted endpoints reject it


def providers() -> dict[str, Provider]:
    out: dict[str, Provider] = {}
    if settings.llm_mode != "fake" and settings.llm_base_url and settings.llm_model:
        out["primary"] = Provider("primary", settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                                  settings.llm_price_in, settings.llm_price_out, True)
    if settings.strong_llm_base_url and settings.strong_llm_model:
        out["strong"] = Provider("strong", settings.strong_llm_base_url, settings.strong_llm_api_key,
                                 settings.strong_llm_model, settings.strong_price_in, settings.strong_price_out, False)
    return out


@lru_cache(maxsize=8)
def _client(p: Provider):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(base_url=p.base_url, api_key=p.api_key or "EMPTY", model=p.model,
                      temperature=settings.llm_temperature, max_tokens=settings.llm_max_tokens,
                      timeout=settings.llm_timeout_s, max_retries=0)


def choose(hint: dict[str, Any] | None = None) -> tuple[list[Provider], str]:
    ps = providers()
    hint = hint or {}
    if not ps:
        return [], "no provider configured"
    if len(ps) == 1:
        p = next(iter(ps.values()))
        return [p], f"only {p.name} configured"
    mode = settings.router_mode
    if mode in ("primary", "strong"):
        first = ps[mode]
        return [first, ps["strong" if mode == "primary" else "primary"]], f"router_mode={mode}"
    reasons = [k for k in ("injection", "complex", "recovering", "deep") if hint.get(k)]
    if reasons:
        return [ps["strong"], ps["primary"]], "strong: " + ",".join(reasons)
    return [ps["primary"], ps["strong"]], "primary: routine step"


def _usage(ai: AIMessage) -> tuple[int, int]:
    u = getattr(ai, "usage_metadata", None) or {}
    return int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)


def _record(p: Provider | None, purpose: str, reason: str, tin: int, tout: int, dt: float, ok: bool,
            workflow_id: str | None, org_id: str) -> float:
    cost = ((tin * p.price_in + tout * p.price_out) / 1e6) if p else 0.0
    try:
        from ..db import conn

        with conn() as c:
            c.execute(
                """INSERT INTO llm_usage(org_id, workflow_id, purpose, provider, model, route_reason, input_tokens,
                                         output_tokens, cost_usd, latency_s, ok) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (org_id, workflow_id, purpose, p.name if p else "fake", p.model if p else "scripted", reason,
                 tin, tout, cost, dt, ok),
            )
    except Exception:
        pass
    return cost


def chat(messages: list[BaseMessage], tool_schemas: list[dict], *, workflow_id: str | None = None,
         org_id: str = "default", hint: dict | None = None) -> tuple[AIMessage, dict]:
    """Tool-calling completion with routing, retries, fallback and metering."""
    if settings.llm_mode == "fake":
        t0 = time.perf_counter()
        ai = _fake(messages)
        _record(None, "agent_step", "fake", 0, 0, time.perf_counter() - t0, True, workflow_id, org_id)
        return ai, {"provider": "fake", "model": "scripted", "reason": "fake", "retries": 0, "cost": 0.0,
                    "tokens_in": 0, "tokens_out": 0}
    order, reason = choose(hint)
    if not order:
        raise RuntimeError("no LLM provider configured")
    last: Exception | None = None
    retries = 0
    for i, p in enumerate(order):
        kwargs = {"tool_choice": "auto"}
        if p.parallel_flag:
            kwargs["parallel_tool_calls"] = False
        llm = _client(p).bind_tools(tool_schemas, **kwargs)
        for attempt in range(settings.llm_retries):
            t0 = time.perf_counter()
            try:
                ai = llm.invoke(messages)
                dt = time.perf_counter() - t0
                tin, tout = _usage(ai)
                why = reason if i == 0 else f"fallback after {order[0].name} failed"
                cost = _record(p, "agent_step", why, tin, tout, dt, True, workflow_id, org_id)
                return ai, {"provider": p.name, "model": p.model, "reason": why, "retries": retries,
                            "cost": cost, "tokens_in": tin, "tokens_out": tout}
            except Exception as e:  # network / 5xx / timeout / bad request
                last = e
                retries += 1
                _record(p, "agent_step", f"error: {type(e).__name__}", 0, 0, time.perf_counter() - t0, False, workflow_id, org_id)
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"all LLM providers failed: {last}")


def complete_text(prompt: str, *, purpose: str, json_mode: bool = False, workflow_id: str | None = None,
                  org_id: str = "default", prefer: str = "strong") -> tuple[str, dict]:
    """Plain completion (policy compiler, reply drafts). Prefers the strong model when configured."""
    if settings.llm_mode == "fake":
        raise RuntimeError("fake mode: no text completions")
    ps = providers()
    order = [ps[k] for k in ([prefer] + [x for x in ("strong", "primary") if x != prefer]) if k in ps]
    last: Exception | None = None
    for p in order:
        t0 = time.perf_counter()
        try:
            llm = _client(p)
            if json_mode:
                try:
                    ai = llm.bind(response_format={"type": "json_object"}).invoke([HumanMessage(prompt)])
                except Exception:
                    ai = llm.invoke([HumanMessage(prompt)])
            else:
                ai = llm.invoke([HumanMessage(prompt)])
            tin, tout = _usage(ai)
            cost = _record(p, purpose, f"text:{p.name}", tin, tout, time.perf_counter() - t0, True, workflow_id, org_id)
            text = ai.content if isinstance(ai.content, str) else json.dumps(ai.content)
            return text, {"provider": p.name, "model": p.model, "cost": cost}
        except Exception as e:
            last = e
    raise RuntimeError(f"text completion failed: {last}")


# ----------------------------- scripted smoke-test agent ---------------------
def _fake(messages: list[BaseMessage]) -> AIMessage:
    human = next(m for m in messages if isinstance(m, HumanMessage)).content
    calls = [tc for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls]
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    names = [c["name"] for c in calls]
    last = tool_msgs[-1].content if tool_msgs else ""
    cid = f"call_{len(messages)}"

    def call(name, **args):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])

    low = human.lower()
    order = (re.search(r"\bO\d{3}[A-Z]\b", human) or [None])[0]
    cust = (re.search(r"\bC\d{3}\b", human) or [None])[0]
    sku = (re.search(r"\bSKU-\d{3}\b", human) or [None])[0]
    money = re.search(r"\$(\d+(?:\.\d+)?)", human)
    amount = float(money.group(1)) if money else 0.0
    qty = re.search(r"(\d+) units", human)

    if "loyalty" in low and cust:
        read, act = ("acme_crm__get_crm_profile", {"customer_id": cust}), ("acme_crm__add_crm_note", {"customer_id": cust, "text": "customer asked about loyalty status"})
    elif "policy" in low and "?" in human:
        read, act = ("search_knowledge", {"query": human[-200:]}), None
    elif "restock" in low and sku:
        read, act = ("get_inventory", {"sku": sku}), ("create_purchase_order", {"sku": sku, "quantity": int(qty.group(1)) if qty else 1})
    elif "new address" in low and order:
        addr = human.split("New address:")[-1].strip().splitlines()[0]
        read, act = ("get_order", {"order_id": order}), ("update_shipping_address", {"order_id": order, "new_address": addr})
    elif "store credit" in low and cust:
        read, act = ("get_customer", {"customer_id": cust}), ("issue_credit", {"customer_id": cust, "amount": amount})
    elif "cancel" in low and order:
        read, act = ("get_order", {"order_id": order}), ("cancel_order", {"order_id": order})
    elif "refund" in low and order:
        read, act = ("get_order", {"order_id": order}), ("issue_refund", {"order_id": order, "amount": amount})
    else:
        return call("escalate_ticket", summary="fake agent could not parse ticket")

    if not names:
        return call(read[0], **read[1])
    if names[-1] == read[0]:
        if act is None:
            return call("resolve_ticket", summary="Answered from the knowledge base.")
        return call(act[0], **act[1])
    if act and names[-1] == act[0]:
        if "POLICY_DENIED" in last or "APPROVAL_REJECTED" in last:
            return call("resolve_ticket", summary="Request declined: " + last[:120])
        return call("resolve_ticket", summary="Done.")
    return call("escalate_ticket", summary="unexpected state")


def tool_message(tool_call_id: str, payload) -> ToolMessage:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return ToolMessage(content=text[:1800], tool_call_id=tool_call_id)
