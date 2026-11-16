"""Tracing + alerting.

* OpenTelemetry (optional): set RESOLVEOS_OTEL_ENABLED=1 and OTEL_EXPORTER_OTLP_ENDPOINT, install
  opentelemetry-sdk + opentelemetry-exporter-otlp. Every graph node and LLM call becomes a span.
* Alerts: the worker evaluates health rules every 30 s per org, opens/resolves alerts, and posts
  new ones to Slack.
"""
from __future__ import annotations

import contextlib
import functools

from .config import settings

_tracer = None


def _init():
    global _tracer
    if _tracer is not None or not settings.otel_enabled:
        return _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        tp = TracerProvider(resource=Resource.create({"service.name": "resolveos"}))
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tp)
        _tracer = trace.get_tracer("resolveos")
    except Exception:
        _tracer = False
    return _tracer


@contextlib.contextmanager
def span(name: str, **attrs):
    t = _init()
    if not t:
        yield None
        return
    with t.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield s


def traced(name: str):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state, *a, **kw):
            with span(name, workflow_id=state.get("workflow_id"), step=state.get("step")):
                return fn(state, *a, **kw)
        return wrapper
    return deco


# ------------------------------------------------------------------ alerts
def check_alerts() -> None:
    from . import audit
    from .autonomy import org_settings
    from .db import conn
    from .notify import slack

    with conn() as c:
        orgs = [r["id"] for r in c.execute("SELECT id FROM orgs").fetchall()]
    for org in orgs:
        new = []
        with conn() as c:
            s = org_settings(c, org)
            recent = c.execute(
                """SELECT status FROM workflows WHERE org_id=%s AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC LIMIT 30""", (org,)).fetchall()
            n = len(recent)
            esc = sum(r["status"] == "escalated" for r in recent)
            failed = c.execute(
                "SELECT count(*) AS n FROM workflows WHERE org_id=%s AND status='failed' AND finished_at > now() - interval '15 minutes'",
                (org,)).fetchone()["n"]
            backlog = c.execute("SELECT count(*) AS n FROM approvals WHERE org_id=%s AND status='pending'", (org,)).fetchone()["n"]
            llm_err = c.execute(
                "SELECT count(*) FILTER (WHERE NOT ok) AS e, count(*) AS n FROM llm_usage WHERE org_id=%s AND created_at > now() - interval '10 minutes'",
                (org,)).fetchone()
            conds = {
                "escalation_rate": (n >= 10 and esc / n > s["alert_escalation_rate"],
                                    f"Escalation rate {esc}/{n} over the last {n} tickets exceeds {s['alert_escalation_rate']:.0%}"),
                "approval_backlog": (backlog > s["alert_backlog"], f"{backlog} approvals waiting for a human"),
                "workflow_failures": (failed > 0, f"{failed} workflow(s) failed in the last 15 minutes"),
                "llm_errors": (llm_err["n"] >= 5 and llm_err["e"] / llm_err["n"] > 0.2,
                               f"LLM error rate {llm_err['e']}/{llm_err['n']} in the last 10 minutes"),
            }
            for kind, (bad, msg) in conds.items():
                open_ = c.execute("SELECT id FROM alerts WHERE org_id=%s AND kind=%s AND resolved_at IS NULL", (org, kind)).fetchone()
                if bad and not open_:
                    c.execute("INSERT INTO alerts(org_id, kind, message) VALUES (%s,%s,%s)", (org, kind, msg))
                    audit.log(c, None, "monitor", "alert.opened", {"org": org, "kind": kind, "message": msg})
                    new.append(msg)
                elif not bad and open_:
                    c.execute("UPDATE alerts SET resolved_at=now() WHERE id=%s", (open_["id"],))
        for msg in new:
            slack(org, f":rotating_light: ResolveOS alert: {msg}")
