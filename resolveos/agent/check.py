"""Verify every configured LLM provider: reachability, tool calling, latency, token metering.
    python -m resolveos.agent.check"""
from __future__ import annotations

import time

from langchain_core.messages import HumanMessage

from .llm import _client, providers

TOOL = {"type": "function", "function": {"name": "get_order", "description": "Look up an order",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}}


def main() -> None:
    ps = providers()
    if not ps:
        raise SystemExit("no provider configured (set RESOLVEOS_LLM_* and/or RESOLVEOS_STRONG_LLM_*)")
    for name, p in ps.items():
        print(f"[{name}] {p.model} @ {p.base_url}")
        try:
            kw = {"tool_choice": "auto", **({"parallel_tool_calls": False} if p.parallel_flag else {})}
            t0 = time.perf_counter()
            ai = _client(p).bind_tools([TOOL], **kw).invoke([HumanMessage("Check the status of order O123A.")])
            dt = time.perf_counter() - t0
            u = getattr(ai, "usage_metadata", None) or {}
            ok = bool(ai.tool_calls) and ai.tool_calls[0]["name"] == "get_order"
            print(f"   {'OK ' if ok else 'WARN'} tool call: {ai.tool_calls[:1] or ai.content[:80]!r}  {dt:.2f}s  tokens={u.get('total_tokens')}")
        except Exception as e:
            print(f"   FAIL {type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    main()
