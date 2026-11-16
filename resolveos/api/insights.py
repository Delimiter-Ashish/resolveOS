"""Insight APIs for the visual layer:

* /v1/graph/topology  - the REAL compiled LangGraph topology (nodes + edges), exported from the
                        state machine itself, so the UI never draws a hard-coded picture.
* /v1/graph/live      - where every active workflow currently sits in that graph + per-node 24h traffic.
* /v1/analytics/flows - Sankey flows (channel -> policy verdict -> outcome), volume heatmap,
                        per-action trust curves, hourly LLM cost series, latency by hour.
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder

from ..auth import Principal, require
from ..db import conn

router = APIRouter()

LABELS = {
    "__start__": ("Ticket in", "guardrails"),
    "agent": ("Agent", "picks a tool"),
    "nudge": ("Nudge", "force a tool"),
    "gate": ("Policy gate", "policy dry-run"),
    "request_approval": ("Ask human", "hash-bound"),
    "await_approval": ("Await human", "parked"),
    "execute": ("Execute", "idempotent"),
    "recover": ("Recover", "retry · breaker"),
    "auto_escalate": ("Escalate", "safe give-up"),
    "__end__": ("Done", "terminal"),
}


@lru_cache
def _topology() -> dict:
    from ..agent.graph import build_graph

    g = build_graph(None).get_graph()
    nodes, edges = [], []
    for n in g.nodes:
        label, sub = LABELS.get(n, (n, ""))
        nodes.append({"id": n, "label": label, "sub": sub})
    for e in g.edges:
        edges.append({"source": e.source, "target": e.target, "conditional": bool(e.conditional)})
    # layer = longest acyclic distance from __start__ (loops ignored), for a layered layout
    order = ["__start__", "agent", "nudge", "gate", "request_approval", "await_approval", "execute", "recover",
             "auto_escalate", "__end__"]
    layers = {"__start__": 0, "agent": 1, "nudge": 1, "gate": 2, "request_approval": 3, "await_approval": 3,
              "execute": 4, "recover": 4, "auto_escalate": 2, "__end__": 5}
    for n in nodes:
        n["layer"] = layers.get(n["id"], 2)
        n["order"] = order.index(n["id"]) if n["id"] in order else 99
    return {"nodes": nodes, "edges": edges}


@router.get("/v1/graph/topology")
def topology(_: Principal = Depends(require("workflow:read"))):
    return _topology()


EVENT_NODE = [
    ("llm.step", "agent"), ("policy.checked", "gate"), ("gate.denied", "gate"),
    ("approval.requested", "request_approval"), ("workflow.parked_for_approval", "await_approval"),
    ("approval.decided", "await_approval"), ("tool.executed", "execute"), ("recovery.attempt", "recover"),
    ("chaos.crash", "execute"), ("workflow.run_started", "agent"), ("workflow.submitted", "__start__"),
    ("workflow.auto_escalated", "auto_escalate"), ("guardrail.", "__start__"), ("workflow.requeued", "recover"),
]


def _node_of(event: str) -> str | None:
    if event.endswith(".committed"):
        return "execute"
    for prefix, node in EVENT_NODE:
        if event.startswith(prefix):
            return node
    return None


@router.get("/v1/graph/live")
def live(p: Principal = Depends(require("workflow:read"))):
    with conn() as c:
        rows = c.execute(
            """SELECT DISTINCT ON (a.workflow_id) a.workflow_id, a.event, w.status
               FROM audit_log a JOIN workflows w ON w.id = a.workflow_id
               WHERE w.org_id=%s AND w.status IN ('queued','running','awaiting_approval')
               ORDER BY a.workflow_id, a.id DESC""", (p.org,)).fetchall()
        traffic = c.execute(
            """SELECT a.event, count(*) AS n FROM audit_log a
               JOIN workflows w ON w.id = a.workflow_id
               WHERE w.org_id=%s AND a.ts > now() - interval '24 hours' GROUP BY a.event""", (p.org,)).fetchall()
        done = c.execute(
            """SELECT status, count(*) AS n FROM workflows WHERE org_id=%s AND finished_at > now() - interval '24 hours'
               GROUP BY status""", (p.org,)).fetchall()
    active: dict[str, list[str]] = {}
    for r in rows:
        node = "await_approval" if r["status"] == "awaiting_approval" else (_node_of(r["event"]) or "agent")
        active.setdefault(node, []).append(r["workflow_id"])
    flow: dict[str, int] = {}
    for t in traffic:
        node = _node_of(t["event"])
        if node:
            flow[node] = flow.get(node, 0) + t["n"]
    return {"active": active, "traffic24h": flow,
            "done24h": {d["status"]: d["n"] for d in done}}


@router.get("/v1/analytics/flows")
def flows(days: int = 30, p: Principal = Depends(require("workflow:read"))):
    days = max(1, min(days, 90))
    with conn() as c:
        sankey = c.execute(
            """SELECT t.channel,
                      CASE WHEN w.flags->>'injection' = 'true' THEN 'injection locked'
                           WHEN EXISTS (SELECT 1 FROM approvals a WHERE a.workflow_id = w.id) THEN 'human approval'
                           WHEN EXISTS (SELECT 1 FROM audit_log g WHERE g.workflow_id = w.id AND g.event='gate.denied')
                                THEN 'policy denied'
                           ELSE 'autonomous' END AS verdict,
                      w.status AS outcome, count(*) AS n
               FROM workflows w JOIN tickets t ON t.id = w.ticket_id
               WHERE w.org_id=%s AND w.finished_at IS NOT NULL AND w.created_at > now() - make_interval(days => %s)
               GROUP BY 1, 2, 3""", (p.org, days)).fetchall()
        heat = c.execute(
            """SELECT EXTRACT(DOW FROM created_at)::int AS dow, EXTRACT(HOUR FROM created_at)::int AS hour,
                      count(*) AS n
               FROM workflows WHERE org_id=%s AND created_at > now() - make_interval(days => %s)
               GROUP BY 1, 2""", (p.org, days)).fetchall()
        lat = c.execute(
            """SELECT EXTRACT(HOUR FROM created_at)::int AS hour,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at - created_at))) AS p50,
                      count(*) AS n
               FROM workflows WHERE org_id=%s AND finished_at IS NOT NULL
                     AND created_at > now() - make_interval(days => %s)
               GROUP BY 1 ORDER BY 1""", (p.org, days)).fetchall()
        trust = c.execute(
            """SELECT action, status, decided_at FROM approvals
               WHERE org_id=%s AND decided_at IS NOT NULL AND status IN ('approved','rejected')
               ORDER BY decided_at""", (p.org,)).fetchall()
        cost = c.execute(
            """SELECT date_trunc('hour', created_at) AS h, provider, sum(cost_usd) AS cost, count(*) AS calls,
                      sum(input_tokens + output_tokens) AS tokens
               FROM llm_usage WHERE org_id=%s AND created_at > now() - interval '48 hours'
               GROUP BY 1, 2 ORDER BY 1""", (p.org,)).fetchall()
    curves: dict[str, list] = {}
    counters: dict[str, list[int]] = {}
    for t in trust:
        ok, n = counters.setdefault(t["action"], [0, 0])
        n += 1
        ok += 1 if t["status"] == "approved" else 0
        counters[t["action"]] = [ok, n]
        curves.setdefault(t["action"], []).append(round(ok / n, 3))
    return jsonable_encoder({
        "days": days,
        "sankey": sankey,
        "heatmap": heat,
        "latency_by_hour": lat,
        "trust_curves": [{"action": k, "points": v[-60:], "reviews": counters[k][1],
                          "rate": round(counters[k][0] / counters[k][1], 3)} for k, v in curves.items()],
        "cost_series": cost,
    })
