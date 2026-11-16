"""Durable workflow worker.

* Workflows live in PostgreSQL. Workers claim them with `FOR UPDATE SKIP LOCKED` and a lease.
* A heartbeat renews the lease while a run is active. If a worker dies (kill -9, OOM, power loss)
  the lease expires, another worker re-claims the workflow and the graph resumes from its last
  PostgreSQL checkpoint. Side effects stay exactly-once thanks to idempotency keys.
* Approvals release the thread entirely (graph `interrupt`); the API wakes the workflow when a human decides.

Run: python -m resolveos.worker
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from . import audit
from .agent.graph import SimulatedCrash, build_graph, initial_state
from .config import settings
from .db import checkpoint_pool, conn

OWNER = f"{socket.gethostname()}:{os.getpid()}"

PICK_SQL = """
SELECT id, status FROM workflows
WHERE status='queued' OR (status='running' AND lease_expires_at < now())
ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
"""


def claim() -> dict | None:
    with conn() as c:
        pick = c.execute(PICK_SQL).fetchone()
        if not pick:
            return None
        takeover = pick["status"] == "running"  # previous owner died: lease expired
        return c.execute(
            """UPDATE workflows SET status='running', lease_owner=%s,
                      lease_expires_at = now() + make_interval(secs => %s::float8),
                      attempts = attempts + 1, resumes = resumes + %s,
                      started_at = COALESCE(started_at, now())
               WHERE id=%s RETURNING *""",
            (OWNER, settings.lease_seconds, 1 if takeover else 0, pick["id"]),
        ).fetchone()


class Heartbeat(threading.Thread):
    def __init__(self, wf_id: str):
        super().__init__(daemon=True)
        self.wf_id, self.stop_evt = wf_id, threading.Event()

    def run(self):
        while not self.stop_evt.wait(settings.lease_seconds / 3):
            try:
                with conn() as c:
                    c.execute(
                        "UPDATE workflows SET lease_expires_at = now() + make_interval(secs => %s) "
                        "WHERE id=%s AND lease_owner=%s AND status='running'",
                        (settings.lease_seconds, self.wf_id, OWNER),
                    )
            except Exception:
                pass


def _pending_approval(wf_id: str) -> bool:
    with conn() as c:
        return bool(c.execute(
            "SELECT 1 AS x FROM approvals WHERE workflow_id=%s AND status='pending'", (wf_id,)
        ).fetchone())


def _finish(wf_id: str, status: str, result: dict | None = None, error: str | None = None):
    with conn() as c:
        c.execute(
            """UPDATE workflows SET status=%s, result=%s::jsonb, error=%s, lease_owner=NULL,
                   lease_expires_at=NULL, finished_at=now() WHERE id=%s""",
            (status, json.dumps(result) if result is not None else None, error, wf_id),
        )
        audit.log(c, wf_id, "worker", f"workflow.{status}", {"result": result, "error": error})


def _park_for_approval(wf_id: str):
    with conn() as c:
        c.execute(
            """UPDATE workflows SET lease_owner=NULL, lease_expires_at=NULL,
                   status = CASE WHEN wake THEN 'queued' ELSE 'awaiting_approval' END, wake = FALSE
               WHERE id=%s""",
            (wf_id,),
        )
        audit.log(c, wf_id, "worker", "workflow.parked_for_approval", {})


def run_one(graph, wf: dict) -> None:
    wf_id = wf["id"]
    cfg = {"configurable": {"thread_id": wf_id}, "recursion_limit": 80}
    hb = Heartbeat(wf_id)
    hb.start()
    try:
        with conn() as c:
            t = c.execute("SELECT * FROM tickets WHERE id=%s", (wf["ticket_id"],)).fetchone()
        ticket = {"id": t["id"], "customer_id": t["customer_id"], "subject": t["subject"],
                  "body": t["body"], "chaos_api": (wf["chaos"] or {}).get("api"),
                  "org_id": wf["org_id"], "channel": t["channel"], "flags": wf["flags"] or {}}
        snap = graph.get_state(cfg)
        started = bool(snap and snap.created_at)
        interrupted = started and any(tk.interrupts for tk in snap.tasks)
        if interrupted:
            if _pending_approval(wf_id):
                hb.stop_evt.set()
                _park_for_approval(wf_id)
                return
            inp = Command(resume=True)
        elif started and snap.next:
            inp = None  # crash recovery: continue from last checkpoint
        elif started:
            inp = None
        else:
            inp = initial_state(wf_id, ticket)
        audit.safe_log(wf_id, "worker", "workflow.run_started",
                       {"owner": OWNER, "attempt": wf["attempts"], "mode": "resume" if started else "fresh"})
        graph.invoke(inp, cfg)

        snap = graph.get_state(cfg)
        if any(tk.interrupts for tk in snap.tasks):
            hb.stop_evt.set()
            _park_for_approval(wf_id)
            return
        outcome = snap.values.get("outcome")
        stats = snap.values.get("stats", {})
        result = {"outcome": outcome, "summary": snap.values.get("summary"),
                  "steps": snap.values.get("step"), "stats": stats}
        hb.stop_evt.set()
        final = {"resolved": "completed", "escalated": "escalated"}.get(outcome, "failed")
        _finish(wf_id, final, result)
        if final in ("completed", "escalated"):
            try:
                from .replies import draft

                draft(wf_id)
            except Exception:
                traceback.print_exc()
    except SimulatedCrash as e:
        # emulate sudden death: no cleanup, no lease release, heartbeat stops.
        hb.stop_evt.set()
        print(f"[worker] SIMULATED CRASH wf={wf_id} at {e}", flush=True)
    except Exception as e:
        hb.stop_evt.set()
        traceback.print_exc()
        with conn() as c:
            attempts = c.execute("SELECT attempts FROM workflows WHERE id=%s", (wf_id,)).fetchone()["attempts"]
        if attempts < settings.max_workflow_attempts:
            time.sleep(min(2.0 * attempts, 6.0))
            with conn() as c:
                c.execute("UPDATE workflows SET status='queued', lease_owner=NULL, lease_expires_at=NULL WHERE id=%s", (wf_id,))
                audit.log(c, wf_id, "worker", "workflow.requeued_after_error", {"error": str(e)[:300], "attempts": attempts})
        else:
            _finish(wf_id, "failed", None, str(e)[:500])
    finally:
        hb.stop_evt.set()


def expire_approvals() -> None:
    with conn() as c:
        rows = c.execute(
            """UPDATE approvals SET status='expired', decided_at=now(), comment='approval window elapsed'
               WHERE status='pending' AND expires_at < now() RETURNING workflow_id, id"""
        ).fetchall()
        for r in rows:
            c.execute(
                """UPDATE workflows SET wake=TRUE,
                       status = CASE WHEN status='awaiting_approval' THEN 'queued' ELSE status END WHERE id=%s""",
                (r["workflow_id"],),
            )
            audit.log(c, r["workflow_id"], "worker", "approval.expired", {"approval_id": r["id"]})


def worker_loop(idx: int, stop: threading.Event) -> None:
    saver = PostgresSaver(checkpoint_pool())  # one saver (own lock) per thread
    graph = build_graph(saver)
    while not stop.is_set():
        try:
            wf = claim()
        except Exception:
            traceback.print_exc()
            time.sleep(1)
            continue
        if wf is None:
            stop.wait(settings.worker_poll_s)
            continue
        run_one(graph, wf)


def main() -> None:
    stop = threading.Event()
    threads = [threading.Thread(target=worker_loop, args=(i, stop), daemon=True) for i in range(settings.worker_threads)]
    for t in threads:
        t.start()
    print(f"[worker] {OWNER} running with {settings.worker_threads} threads", flush=True)
    last_alert = 0.0
    try:
        while True:
            time.sleep(2)
            try:
                expire_approvals()
                if time.time() - last_alert > 30:
                    last_alert = time.time()
                    from .observability import check_alerts

                    check_alerts()
            except Exception:
                traceback.print_exc()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
