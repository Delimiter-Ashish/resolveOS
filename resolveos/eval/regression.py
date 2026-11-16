"""Self-growing regression suite.

Every human correction becomes a test case automatically:
  * an approver REJECTS a proposed action          -> "agent must not propose/execute this action"
  * an operator flags a finished workflow as wrong  -> expected outcome + forbidden actions

Each case stores the ticket and a snapshot of the business records it touched (times stored as
ages, so policy windows replay faithfully). Run all cases after any prompt/model/policy change:

    python -m resolveos.eval.regression            # replays every case (resets business data!)
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone

import httpx

from .. import audit
from ..common import action_hash
from ..config import settings
from ..db import conn, reset_business
from ..tickets import new_id

TS_FIELDS = {"placed_at", "delivered_at"}


def _ids(text: str) -> dict[str, set[str]]:
    return {"orders": set(re.findall(r"\bO\d{3,}[A-Z]\b", text)), "skus": set(re.findall(r"\bSKU-\d+\b", text)),
            "accounts": set(re.findall(r"\bACC-\d+\b", text))}


def _rows(c, sql, params) -> list[dict]:
    now = datetime.now(timezone.utc)
    out = []
    for r in c.execute(sql, params).fetchall():
        d = {}
        for k, v in dict(r).items():
            if k in TS_FIELDS and v is not None:
                d[k] = {"age_s": (now - v).total_seconds()}
            elif hasattr(v, "quantize"):
                d[k] = float(v)
            else:
                d[k] = v
        out.append(d)
    return out


def snapshot_for(c, customer_id: str, text: str) -> dict:
    ids = _ids(text)
    orders = _rows(c, "SELECT * FROM orders WHERE customer_id=%s OR id = ANY(%s)", (customer_id, list(ids["orders"])))
    oids = [o["id"] for o in orders]
    custs = {customer_id} | {o["customer_id"] for o in orders}
    return {
        "customers": _rows(c, "SELECT * FROM customers WHERE id = ANY(%s)", (list(custs),)),
        "orders": orders,
        "shipments": _rows(c, "SELECT * FROM shipments WHERE order_id = ANY(%s)", (oids,)),
        "inventory": _rows(c, "SELECT * FROM inventory WHERE sku = ANY(%s)", (list(ids["skus"]),)),
        "accounts": _rows(c, "SELECT * FROM accounts WHERE id = ANY(%s)", (list(ids["accounts"]),)),
        "reships": _rows(c, "SELECT order_id, reason FROM reships WHERE order_id = ANY(%s) AND workflow_id IS NULL", (oids,)),
    }


def capture_case(c, workflow_id: str, *, created_by: str, reason: str, forbidden: list[dict] | None = None,
                 terminal: str = "any") -> str | None:
    wf = c.execute("SELECT * FROM workflows WHERE id=%s", (workflow_id,)).fetchone()
    if not wf:
        return None
    t = c.execute("SELECT * FROM tickets WHERE id=%s", (wf["ticket_id"],)).fetchone()
    # snapshot is taken now; side effects this workflow already committed are rolled back in the snapshot
    snap = snapshot_for(c, t["customer_id"], t["subject"] + "\n" + t["body"])
    for r in c.execute("SELECT order_id, amount FROM refunds WHERE workflow_id=%s", (workflow_id,)).fetchall():
        for o in snap["orders"]:
            if o["id"] == r["order_id"]:
                o["refunded"] = round(float(o["refunded"]) - float(r["amount"]), 2)
    cid = new_id("rc")
    expect = {"forbidden": forbidden or [], "terminal": terminal}
    c.execute(
        """INSERT INTO eval_cases(id, org_id, source_workflow, reason, ticket, snapshot, expect, created_by)
           VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)""",
        (cid, wf["org_id"], workflow_id, reason[:300],
         json.dumps({"customer_id": t["customer_id"], "subject": t["subject"], "body": t["body"]}),
         json.dumps(snap, default=str), json.dumps(expect, default=str), created_by),
    )
    audit.log(c, workflow_id, created_by, "eval.case_captured", {"case_id": cid, "reason": reason[:200], "expect": expect})
    return cid


def _seed(c, snap: dict) -> None:
    def ins(table, row):
        row = dict(row)
        cols, vals, ph = [], [], []
        for k, v in row.items():
            cols.append(k)
            if isinstance(v, dict) and "age_s" in v:
                vals.append(v["age_s"])
                ph.append("now() - make_interval(secs => %s::float8)")
            else:
                vals.append(v)
                ph.append("%s")
        c.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join(ph)}) ON CONFLICT DO NOTHING", vals)

    for table in ("customers", "orders", "shipments", "inventory", "accounts"):
        for r in snap.get(table, []):
            ins(table, r)
    for r in snap.get("reships", []):
        c.execute("INSERT INTO reships(id, order_id, reason) VALUES (%s,%s,%s)", (new_id("rs"), r["order_id"], r["reason"]))


def run(timeout: float = 180.0) -> dict:
    with conn() as c:
        cases = c.execute("SELECT * FROM eval_cases ORDER BY created_at").fetchall()
    http = httpx.Client(base_url=settings.api_base_url, timeout=20)
    op = {"Authorization": f"Bearer {settings.operator_api_key}"}
    ap = {"Authorization": f"Bearer {settings.approver_api_key}"}
    results = []
    for case in cases:
        reset_business()
        with conn() as c:
            _seed(c, case["snapshot"])
        forb = {action_hash(f["action"], f["args"]) for f in case["expect"].get("forbidden", [])}
        t = case["ticket"]
        wid = http.post("/v1/workflows", json=t, headers=op).json()["workflow_id"]
        t0, status, decided = time.time(), "queued", set()
        while time.time() - t0 < timeout:
            g = http.get(f"/v1/workflows/{wid}", headers=op).json()
            status = g["workflow"]["status"]
            for a in g["approvals"]:
                if a["status"] == "pending" and a["id"] not in decided:
                    decided.add(a["id"])
                    bad = a["action_hash"] in forb
                    http.post(f"/v1/approvals/{a['id']}/decision", headers=ap,
                              json={"decision": "reject" if bad else "approve", "comment": "regression replay"})
            if status in ("completed", "escalated", "failed"):
                break
            time.sleep(0.25)
        with conn() as c:
            proposed = c.execute("SELECT action_hash, action FROM approvals WHERE workflow_id=%s", (wid,)).fetchall()
            committed = c.execute(
                "SELECT event, payload FROM audit_log WHERE workflow_id=%s AND event LIKE '%%.committed'", (wid,)
            ).fetchall()
        violations = [p["action"] for p in proposed if p["action_hash"] in forb]
        for e in committed:
            act = e["event"].removesuffix(".committed")
            if action_hash(act, e["payload"].get("args") or {}) in forb:
                violations.append(act + " (executed)")
        reasons = [f"repeated forbidden action: {v}" for v in violations]
        want = case["expect"].get("terminal", "any")
        if status not in ("completed", "escalated"):
            reasons.append(f"workflow ended {status}")
        elif want == "resolved" and status != "completed":
            reasons.append("expected resolved")
        elif want == "escalated" and status != "escalated":
            reasons.append("expected escalated")
        results.append({"case": case["id"], "reason": case["reason"], "workflow_id": wid, "status": status,
                        "passed": not reasons, "failures": reasons})
        print(f"  {'PASS' if not reasons else 'FAIL'} {case['id']}  {case['reason'][:70]}  {'; '.join(reasons)}", flush=True)
    passed = sum(r["passed"] for r in results)
    return {"cases": len(results), "passed": passed, "results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="confirm: business tables will be reset")
    a = ap.parse_args()
    if not a.yes:
        raise SystemExit("This resets business data (orders, customers...). Re-run with --yes.")
    r = run()
    print(f"\nregression: {r['passed']}/{r['cases']} passed")
    with open("results/regression_latest.json", "w") as f:
        json.dump(r, f, indent=2, default=str)


if __name__ == "__main__":
    main()
