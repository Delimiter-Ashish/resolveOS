"""LangGraph workflow:

  START -> agent -> gate -> [request_approval -> await_approval(interrupt)] -> execute -> agent ...
                      \\-> recover (retry / auto-escalate) <-/
  Every node boundary is checkpointed in PostgreSQL, so a run survives process death and an
  approval can take hours without holding a thread.
"""
from __future__ import annotations

import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import ValidationError

from .. import audit
from ..config import settings
from . import client as api
from .llm import chat, tool_message
from .prompts import SYSTEM_PROMPT
from .tools import schemas_for, tools_for


class SimulatedCrash(BaseException):
    """Raised by fault injection to emulate sudden process death (not catchable by `except Exception`)."""


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    workflow_id: str
    ticket: dict
    step: int
    nudges: int
    pending: dict | None
    recovery_attempts: int
    outcome: str | None
    summary: str | None
    stats: dict


def maybe_crash(workflow_id: str, point: str) -> None:
    if not settings.chaos_enabled:
        return
    from ..db import conn

    with conn() as c:
        row = c.execute(
            """UPDATE workflows SET chaos = jsonb_set(chaos, '{crash_fired}', 'true')
               WHERE id=%s AND chaos->>'crash' = %s AND COALESCE(chaos->>'crash_fired','false') <> 'true'
               RETURNING id""",
            (workflow_id, point),
        ).fetchone()
    if row:
        audit.safe_log(workflow_id, "chaos", "chaos.crash", {"point": point})
        raise SimulatedCrash(point)


def _bump(state: AgentState, **inc) -> dict:
    s = dict(state["stats"])
    for k, v in inc.items():
        s[k] = round(s.get(k, 0) + v, 4)
    return s


# ------------------------------------------------------------------ nodes
def _org(state: AgentState) -> str:
    return state["ticket"].get("org_id", "default")


def _hint(state: AgentState) -> dict:
    t = state["ticket"]
    text = t.get("subject", "") + " " + t.get("body", "")
    ids = set(__import__("re").findall(r"\b(?:O\d{3,}[A-Z]|SKU-\d+|ACC-\d+)\b", text))
    last = state["messages"][-1] if state["messages"] else None
    last_txt = getattr(last, "content", "") if last is not None else ""
    return {
        "injection": bool((t.get("flags") or {}).get("injection")),
        "complex": len(ids) >= 2 or len(text) > 700,
        "recovering": state["nudges"] > 0 or state["recovery_attempts"] > 0
                      or (isinstance(last_txt, str) and last_txt.startswith("ERROR")),
        "deep": state["step"] >= 6,
    }


def agent_node(state: AgentState) -> dict:
    wf = state["workflow_id"]
    t0 = time.perf_counter()
    ai, meta = chat([SystemMessage(SYSTEM_PROMPT)] + state["messages"], schemas_for(_org(state)),
                    workflow_id=wf, org_id=_org(state), hint=_hint(state))
    dt = time.perf_counter() - t0
    maybe_crash(wf, "after_llm")
    audit.safe_log(wf, "agent", "llm.step", {
        "step": state["step"] + 1, "latency_s": round(dt, 3), "model": meta["model"], "route": meta["reason"],
        "tokens": meta["tokens_in"] + meta["tokens_out"], "cost_usd": round(meta["cost"], 6),
        "tool_calls": [{"name": t["name"], "args": t["args"]} for t in ai.tool_calls],
        "text": (ai.content if isinstance(ai.content, str) else "")[:200],
    })
    models = dict(state["stats"].get("models", {}))
    models[meta["model"]] = models.get(meta["model"], 0) + 1
    stats = _bump(state, llm_calls=1, llm_s=dt, llm_retries=meta["retries"], llm_cost_usd=meta["cost"],
                  tokens=meta["tokens_in"] + meta["tokens_out"])
    stats["models"] = models
    return {"messages": [ai], "step": state["step"] + 1, "stats": stats}


def route_agent(state: AgentState) -> str:
    if state["step"] > settings.max_agent_steps:
        return "auto_escalate"
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "gate"
    return "nudge" if state["nudges"] < settings.max_nudges else "auto_escalate"


def nudge_node(state: AgentState) -> dict:
    return {
        "messages": [HumanMessage("You must finish by calling a tool: resolve_ticket(summary) if the request is "
                                  "handled or declined, or escalate_ticket(summary) if a human is needed. "
                                  "Do not reply with plain text.")],
        "nudges": state["nudges"] + 1,
    }


def gate_node(state: AgentState) -> dict:
    wf = state["workflow_id"]
    ai: AIMessage = state["messages"][-1]
    tc, extra = ai.tool_calls[0], ai.tool_calls[1:]
    msgs = [tool_message(e["id"], "SKIPPED: issue one tool call per turn; call it again if still needed.") for e in extra]
    tools = tools_for(_org(state))
    tool = tools.get(tc["name"])
    if tool is None:  # maybe a connector registered moments ago: refresh once
        from . import tools as T

        T._conn_cache.pop(_org(state), None)
        tools = tools_for(_org(state))
        tool = tools.get(tc["name"])
    if tool is None:
        msgs.append(tool_message(tc["id"], f"ERROR: unknown tool '{tc['name']}'. Available: {', '.join(tools)}"))
        return {"messages": msgs, "pending": None}
    try:
        args = tool.validate(tc["args"] or {})
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        msgs.append(tool_message(tc["id"], f"ERROR: invalid arguments for {tc['name']}: {problems}"))
        return {"messages": msgs, "pending": None}
    except (ValueError, TypeError) as e:
        msgs.append(tool_message(tc["id"], f"ERROR: invalid arguments for {tc['name']}: {e}"))
        return {"messages": msgs, "pending": None}

    pending = {"id": tc["id"], "name": tc["name"], "args": args, "stage": "execute", "approval_id": None, "gate": "ALLOW"}
    if tool.kind != "write":
        return {"messages": msgs, "pending": pending}

    r = api.request("POST", "/v1/policy/check", json={"action": tool.action, "args": args},
                    workflow_id=wf, breaker_name="policy")
    if r.unavailable:
        return {"messages": msgs, "pending": {**pending, "stage": "gate", "gate": "UNAVAILABLE"},
                "stats": _bump(state, retries=max(r.attempts - 1, 0))}
    if not r.ok:
        msgs.append(tool_message(tc["id"], f"ERROR: policy check failed: {r.body}"))
        return {"messages": msgs, "pending": None}
    effect, reason, rule = r.body["effect"], r.body["reason"], r.body["rule"]
    if effect == "DENY":
        audit.safe_log(wf, "agent", "gate.denied", {"tool": tc["name"], "args": args, "rule": rule, "reason": reason})
        msgs.append(tool_message(tc["id"], f"POLICY_DENIED [{rule}]: {reason}. This action is not permitted; do not retry it."))
        return {"messages": msgs, "pending": None, "stats": _bump(state, gate_denials=1)}
    return {"messages": msgs, "pending": {**pending, "gate": effect, "reason": reason, "rule": rule}}


def route_gate(state: AgentState) -> str:
    p = state["pending"]
    if p is None:
        return "agent"
    if p["gate"] == "UNAVAILABLE":
        return "recover"
    if p["gate"] == "REQUIRE_APPROVAL":
        return "request_approval"
    return "execute"


def request_approval_node(state: AgentState) -> dict:
    wf, p = state["workflow_id"], state["pending"]
    r = api.request(
        "POST", "/v1/approvals",
        json={"workflow_id": wf, "action": tools_for(_org(state))[p["name"]].action, "args": p["args"],
              "justification": f"{p.get('rule')}: {p.get('reason')}"},
        idem_key=f"{wf}:{p['id']}:approval", workflow_id=wf, breaker_name="approvals",
    )
    if not r.ok:
        raise RuntimeError(f"could not request approval: {r.status} {r.body}")
    return {"pending": {**p, "approval_id": r.body["id"]}, "stats": _bump(state, approvals=1)}


def await_approval_node(state: AgentState) -> dict:
    wf, p = state["workflow_id"], state["pending"]
    maybe_crash(wf, "during_approval")
    ap = None
    for _ in range(3):
        r = api.request("GET", f"/v1/approvals/{p['approval_id']}", workflow_id=wf, breaker_name="approvals")
        if not r.ok:
            raise RuntimeError(f"cannot read approval: {r.status} {r.body}")
        ap = r.body
        if ap["status"] != "pending":
            break
        # Durable pause: the thread is released; the API wakes the workflow on decision.
        interrupt({"approval_id": p["approval_id"], "args": p["args"]})
    if ap["status"] == "pending":
        raise RuntimeError("approval still pending after spurious wake-ups")
    if ap["status"] == "approved":
        maybe_crash(wf, "after_approval")
        return {"pending": {**p, "approved": True}}
    who = ap.get("decided_by") or "system"
    return {
        "messages": [tool_message(p["id"], f"APPROVAL_REJECTED by {who}: {ap.get('comment') or ap['status']}. "
                                           "Do not retry this action.")],
        "pending": None,
    }


def route_after_approval(state: AgentState) -> str:
    p = state["pending"]
    return "execute" if p and p.get("approved") else "agent"


def _error_text(r: api.ApiResult) -> str:
    b = r.body if isinstance(r.body, dict) else {}
    d = b.get("detail", b)
    if isinstance(d, dict):
        if d.get("error") == "policy_denied":
            return f"POLICY_DENIED [{d.get('rule')}]: {d.get('reason')}. Do not retry."
        return "ERROR: " + ", ".join(f"{k}={v}" for k, v in d.items())
    return f"ERROR: HTTP {r.status}: {str(d)[:200]}"


def execute_node(state: AgentState) -> dict:
    wf, p = state["workflow_id"], state["pending"]
    tool = tools_for(_org(state))[p["name"]]
    is_write = tool.kind == "write"
    if is_write:
        maybe_crash(wf, "before_tool_exec")
    args = p["args"]
    path = tool.path(args).replace("{ticket}", state["ticket"]["id"])
    body = tool.body(args) if tool.body else None
    if is_write and p.get("approval_id"):
        body = {**(body or {}), "approval_id": p["approval_id"]}
    r = api.request(
        tool.method, path,
        json=body if tool.method == "POST" else None,
        params=tool.query(args) if tool.query else None,
        idem_key=None if tool.kind == "read" else f"{wf}:{p['id']}",
        workflow_id=wf,
        chaos=state["ticket"].get("chaos_api") if is_write else None,
        breaker_name=tool.name,
    )
    inc = {"tool_calls": 1, "retries": max(r.attempts - 1, 0)}
    audit.safe_log(wf, "agent", "tool.executed", {
        "tool": p["name"], "args": args, "status": r.status, "attempts": r.attempts,
        "replayed": r.replayed, "events": r.events, "unavailable": r.unavailable,
    })
    if r.unavailable:
        return {"pending": {**p, "stage": "execute"}, "stats": _bump(state, **inc)}
    if r.ok:
        out: dict[str, Any] = {
            "messages": [tool_message(p["id"], r.body)], "pending": None, "recovery_attempts": 0,
            "stats": _bump(state, **inc, replays=1 if r.replayed else 0),
        }
        if is_write:
            maybe_crash(wf, "after_tool_exec")  # side effect committed, checkpoint not yet saved
        if tool.kind == "terminal":
            out["outcome"] = tool.terminal_outcome
            out["summary"] = args.get("summary", "")
        return out
    return {"messages": [tool_message(p["id"], _error_text(r))], "pending": None,
            "stats": _bump(state, **inc, tool_errors=1)}


def route_execute(state: AgentState) -> str:
    if state.get("outcome"):
        return "end"
    return "recover" if state["pending"] is not None else "agent"


def _escalate(state: AgentState, reason: str) -> dict:
    wf = state["workflow_id"]
    r = api.request(
        "POST", f"/v1/tickets/{state['ticket']['id']}/escalate", json={"summary": reason},
        idem_key=f"{wf}:auto-escalate", workflow_id=wf, max_attempts=6, breaker_name="auto-escalate",
    )
    if not r.ok:
        raise RuntimeError(f"auto-escalation failed: {r.status} {r.body}")
    audit.safe_log(wf, "agent", "workflow.auto_escalated", {"reason": reason})
    return {"outcome": "escalated", "summary": reason, "pending": None,
            "stats": _bump(state, auto_escalated=1)}


def recover_node(state: AgentState) -> dict:
    wf, p = state["workflow_id"], state["pending"]
    n = state["recovery_attempts"] + 1
    audit.safe_log(wf, "agent", "recovery.attempt", {"attempt": n, "tool": p["name"], "stage": p["stage"]})
    if n <= settings.max_recovery_attempts:
        time.sleep(min(0.5 * 2**n, 4.0))
        return {"recovery_attempts": n, "stats": _bump(state, recoveries=1)}
    out = _escalate(state, f"Auto-escalated: downstream service unavailable while running {p['name']}.")
    out["stats"] = _bump(state, recoveries=1, auto_escalated=1)
    return out


def route_recover(state: AgentState) -> str:
    if state.get("outcome"):
        return "end"
    return "gate" if state["pending"]["stage"] == "gate" else "execute"


def auto_escalate_node(state: AgentState) -> dict:
    why = "step budget exhausted" if state["step"] > settings.max_agent_steps else "agent did not conclude the ticket"
    return _escalate(state, f"Auto-escalated: {why}.")


def build_graph(checkpointer):
    from ..observability import traced

    g = StateGraph(AgentState)
    for name, fn in [
        ("agent", agent_node), ("nudge", nudge_node), ("gate", gate_node),
        ("request_approval", request_approval_node), ("await_approval", await_approval_node),
        ("execute", execute_node), ("recover", recover_node), ("auto_escalate", auto_escalate_node),
    ]:
        g.add_node(name, traced(f"node.{name}")(fn))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_agent, {"gate": "gate", "nudge": "nudge", "auto_escalate": "auto_escalate"})
    g.add_edge("nudge", "agent")
    g.add_conditional_edges("gate", route_gate, {"agent": "agent", "recover": "recover",
                                                 "request_approval": "request_approval", "execute": "execute"})
    g.add_edge("request_approval", "await_approval")
    g.add_conditional_edges("await_approval", route_after_approval, {"execute": "execute", "agent": "agent"})
    g.add_conditional_edges("execute", route_execute, {"agent": "agent", "recover": "recover", "end": END})
    g.add_conditional_edges("recover", route_recover, {"gate": "gate", "execute": "execute", "end": END})
    g.add_edge("auto_escalate", END)
    return g.compile(checkpointer=checkpointer)


def initial_state(workflow_id: str, ticket: dict) -> dict:
    from ..guardrails import UNTRUSTED_NOTE

    text = (f"Ticket {ticket['id']} (channel: {ticket.get('channel', 'api')})\nCustomer: {ticket['customer_id']}\n"
            f"Subject: {ticket['subject']}\n\n<customer_message>\n{ticket['body']}\n</customer_message>")
    if (ticket.get("flags") or {}).get("injection"):
        text = UNTRUSTED_NOTE + "\n\n" + text
    return {
        "messages": [HumanMessage(text)], "workflow_id": workflow_id, "ticket": ticket,
        "step": 0, "nudges": 0, "pending": None, "recovery_attempts": 0, "outcome": None,
        "summary": None, "stats": {},
    }
