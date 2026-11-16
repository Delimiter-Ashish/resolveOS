"""Evaluation harness.

    python -m resolveos.eval.runner --reset --concurrency 8
    python -m resolveos.eval.runner --categories refund_auto,cancel_order --limit 20

Seeds scenario data, submits tickets through the public REST API, plays a simulated human
approver (with its own API key and a small think-time), waits for every workflow to terminate,
then judges the *final database state* against ground truth. Latency and reliability metrics
are computed from what was measured, never from constants.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .. import audit
from ..config import settings
from ..db import conn, reset_data
from .judge import judge, snapshot
from .scenarios import CATEGORIES, Scenario, generate, generate_adversarial, seed

TERMINAL = {"completed", "escalated", "failed"}


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * q
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


class Runner:
    def __init__(self, timeout_s: float, approver_delay: tuple[float, float]):
        self.http = httpx.Client(base_url=settings.api_base_url, timeout=20.0)
        self.timeout_s = timeout_s
        self.delay = approver_delay
        self.rng = random.Random(1234)
        self.lock = threading.Lock()

    def _h(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}

    def run_scenario(self, sc: Scenario, initial: dict) -> dict:
        t0 = time.time()
        res: dict = {"sid": sc.sid, "category": sc.category, "submitted_at": t0}
        try:
            body = {"customer_id": sc.customer_id, "subject": sc.subject, "body": sc.body}
            if sc.chaos:
                body["chaos"] = sc.chaos
            r = self.http.post("/v1/workflows", json=body, headers=self._h(settings.operator_api_key))
            r.raise_for_status()
            wid = r.json()["workflow_id"]
            res["workflow_id"] = wid
            decided: set[str] = set()
            status = "queued"
            while time.time() - t0 < self.timeout_s:
                g = self.http.get(f"/v1/workflows/{wid}", headers=self._h(settings.operator_api_key)).json()
                status = g["workflow"]["status"]
                for ap in g["approvals"]:
                    if ap["status"] == "pending" and ap["id"] not in decided:
                        decided.add(ap["id"])
                        with self.lock:
                            think = self.rng.uniform(*self.delay)
                        time.sleep(think)
                        self.http.post(
                            f"/v1/approvals/{ap['id']}/decision",
                            json={"decision": sc.approver, "comment": f"simulated reviewer: {sc.approver}"},
                            headers=self._h(settings.approver_api_key),
                        ).raise_for_status()
                if status in TERMINAL:
                    break
                time.sleep(0.2)
            else:
                status = f"timeout({status})"
            res["status"] = status
        except Exception as e:  # harness-level failure counts as scenario failure
            res.update(status="harness_error", harness_error=str(e)[:300])
        return res

    def collect(self, sc: Scenario, initial: dict, res: dict) -> dict:
        wid = res.get("workflow_id")
        with conn() as c:
            observed = snapshot(c, sc)
            wf = c.execute("SELECT * FROM workflows WHERE id=%s", (wid,)).fetchone() if wid else None
            aps = c.execute("SELECT * FROM approvals WHERE workflow_id=%s", (wid,)).fetchall() if wid else []
            ev = c.execute("SELECT event, count(*) AS n FROM audit_log WHERE workflow_id=%s GROUP BY event",
                           (wid,)).fetchall() if wid else []
        events = {e["event"]: e["n"] for e in ev}
        status = res.get("status", "unknown")
        verdict = judge(sc, initial, observed, status, len(aps))
        if wf and wf["finished_at"]:
            total = (wf["finished_at"] - wf["created_at"]).total_seconds()
            wait = sum((a["decided_at"] - a["created_at"]).total_seconds() for a in aps if a["decided_at"])
            res["latency_e2e_s"] = total
            res["approval_wait_s"] = wait
            res["latency_exec_s"] = max(total - wait, 0.0)
        stats = ((wf or {}).get("result") or {}).get("stats", {}) if wf else {}
        res.update(
            success=verdict["success"], reasons=verdict["reasons"], approvals=len(aps),
            resumes=(wf or {}).get("resumes", 0), attempts=(wf or {}).get("attempts", 0),
            llm_calls=stats.get("llm_calls", 0), tool_calls=stats.get("tool_calls", 0),
            tool_retries=stats.get("retries", 0), recoveries=stats.get("recoveries", 0),
            replays=events.get("refund.issue.replayed", 0) + sum(v for k, v in events.items() if k.endswith(".replayed") and k != "refund.issue.replayed"),
            blocked_by_policy=sum(v for k, v in events.items() if k.endswith(".blocked")),
            gate_denials=events.get("gate.denied", 0), auto_escalated=stats.get("auto_escalated", 0),
            audit_events=sum(events.values()),
        )
        res["audit_chain_valid"] = audit.verify_chain(wid)["valid"] if wid else False
        res["expected_terminal"] = sc.expect_terminal
        return res


def summarize(rows: list[dict]) -> dict:
    def block(rs: list[dict]) -> dict:
        ex = [r["latency_exec_s"] for r in rs if "latency_exec_s" in r]
        e2e = [r["latency_e2e_s"] for r in rs if "latency_e2e_s" in r]
        return {
            "n": len(rs), "success": sum(1 for r in rs if r.get("success")),
            "success_rate": (sum(1 for r in rs if r.get("success")) / len(rs)) if rs else 0.0,
            "exec_p50_s": pct(ex, .5), "exec_p95_s": pct(ex, .95), "exec_p99_s": pct(ex, .99), "exec_mean_s": statistics.fmean(ex) if ex else float("nan"),
            "e2e_p95_s": pct(e2e, .95),
        }

    out = {"overall": block(rows), "by_category": {}}
    for cat in CATEGORIES + ["adversarial_injection", "adversarial_pii"]:
        rs = [r for r in rows if r["category"] == cat]
        if rs:
            out["by_category"][cat] = block(rs)
    out["excluding_crash_resume"] = block([r for r in rows if r["category"] != "chaos_crash_resume"])
    out["totals"] = {
        k: sum(r.get(k, 0) for r in rows)
        for k in ["llm_calls", "tool_calls", "tool_retries", "recoveries", "approvals", "resumes", "replays",
                  "blocked_by_policy", "gate_denials", "auto_escalated"]
    }
    out["audit_chain_valid_rate"] = sum(1 for r in rows if r.get("audit_chain_valid")) / max(len(rows), 1)
    out["mean_llm_calls"] = out["totals"]["llm_calls"] / max(len(rows), 1)
    return out


def write_report(rows: list[dict], summ: dict, meta: dict, outdir: Path) -> Path:
    ts = meta["timestamp"]
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"eval_{ts}.json").write_text(json.dumps({"meta": meta, "summary": summ, "scenarios": rows}, indent=2, default=str))
    with open(outdir / f"scenarios_{ts}.csv", "w", newline="") as f:
        cols = ["sid", "category", "success", "status", "latency_exec_s", "latency_e2e_s", "approval_wait_s", "llm_calls",
                "tool_calls", "tool_retries", "recoveries", "approvals", "resumes", "replays", "audit_chain_valid", "reasons"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r2 = dict(r)
            r2["reasons"] = " | ".join(r.get("reasons", []))
            w.writerow(r2)

    o = summ["overall"]
    x = summ["excluding_crash_resume"]
    L = []
    if meta["llm_mode"] == "fake":
        L += ["> **SMOKE TEST ONLY - scripted stand-in agent, NOT a model evaluation. Do not quote these numbers.**", ""]
    L += [
        f"# ResolveOS evaluation - {ts}", "",
        f"- Scenarios: **{o['n']}**  |  model: `{meta['model']}`  |  concurrency: {meta['concurrency']}  |  worker threads: {meta['worker_threads']}",
        f"- **Task success: {o['success']}/{o['n']} = {o['success_rate']*100:.1f}%**",
        f"- **Execution latency (excl. human approval wait):** p50 {o['exec_p50_s']:.1f}s, **p95 {o['exec_p95_s']:.1f}s**, p99 {o['exec_p99_s']:.1f}s",
        f"- p95 excluding crash-resume scenarios: {x['exec_p95_s']:.1f}s  (those include the {meta['lease_seconds']:.0f}s lease-expiry wait by design)",
        f"- End-to-end p95 (including approval wait): {o['e2e_p95_s']:.1f}s",
        f"- Audit hash-chain valid: {summ['audit_chain_valid_rate']*100:.1f}%",
        f"- Suite: {meta.get('suite')}  |  router: {meta.get('router_mode')}  |  LLM usage: "
        + ", ".join(f"{x['model']} x{x['calls']} (${x['cost_usd']:.4f})" for x in meta.get("llm_usage", [])),
        "", "Definitions: success = final DB state equals ground truth exactly (no missing, wrong or duplicated side effect), "
        "terminal state acceptable, approval behaviour as expected. Latency = workflow finished_at - created_at - sum(approval waits), "
        "measured under the stated concurrency.", "",
        "## By category", "", "| category | n | success | rate | p50 s | p95 s |", "|---|---|---|---|---|---|",
    ]
    for cat, b in summ["by_category"].items():
        L.append(f"| {cat} | {b['n']} | {b['success']} | {b['success_rate']*100:.0f}% | {b['exec_p50_s']:.1f} | {b['exec_p95_s']:.1f} |")
    t = summ["totals"]
    L += ["", "## Reliability counters", "",
          f"- LLM calls: {t['llm_calls']} (mean {summ['mean_llm_calls']:.1f}/ticket), tool calls: {t['tool_calls']}",
          f"- Transient-failure retries absorbed: {t['tool_retries']}, recovery-node activations: {t['recoveries']}, auto-escalations: {t['auto_escalated']}",
          f"- Idempotent replays served (duplicate side effects prevented): {t['replays']}",
          f"- Crash resumes from checkpoint: {t['resumes']}",
          f"- Approvals requested: {t['approvals']}, gate-level policy denials: {t['gate_denials']}, server-side blocks: {t['blocked_by_policy']}"]
    fails = [r for r in rows if not r.get("success")]
    L += ["", f"## Failures ({len(fails)})", ""]
    for r in fails:
        L.append(f"- **{r['sid']}** ({r['category']}, status={r.get('status')}): " + "; ".join(r.get("reasons", []) or [r.get("harness_error", "")]))
    path = outdir / f"report_{ts}.md"
    path.write_text("\n".join(L) + "\n")
    (outdir / "latest_report.md").write_text("\n".join(L) + "\n")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="truncate all data before running")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--concurrency", type=int, default=settings.worker_threads)
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--categories", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="results")
    ap.add_argument("--suite", default="core", choices=["core", "adversarial", "all"])
    ap.add_argument("--allow-custom-policy", action="store_true",
                    help="run even if custom rules / non-autonomous mode are active (results not comparable)")
    a = ap.parse_args()

    with conn() as c:
        mode = c.execute("SELECT autonomy_mode FROM org_settings WHERE org_id='default'").fetchone()
        nrules = c.execute("SELECT count(*) AS n FROM policy_rules WHERE org_id='default' AND enabled").fetchone()["n"]
    if not a.allow_custom_policy and ((mode and mode["autonomy_mode"] != "autonomous") or nrules):
        raise SystemExit(f"Benchmark needs the built-in policy only: autonomy_mode={mode['autonomy_mode'] if mode else '?'}, "
                         f"custom rules enabled={nrules}. Switch to autonomous / disable rules, or pass --allow-custom-policy.")

    scenarios = {"core": generate(a.seed), "adversarial": generate_adversarial(),
                 "all": generate(a.seed) + generate_adversarial()}[a.suite]
    if a.categories:
        keep = set(a.categories.split(","))
        scenarios = [s for s in scenarios if s.category in keep]
    if a.limit:
        scenarios = scenarios[: a.limit]
    if a.reset:
        reset_data()
        from ..db import seed_api_keys  # keys survive reset; harmless re-assert
        seed_api_keys()

    initials: dict[str, dict] = {}
    with conn() as c:
        for sc in scenarios:
            seed(c, sc)
    with conn() as c:
        for sc in scenarios:
            initials[sc.sid] = snapshot(c, sc)

    runner = Runner(a.timeout, (0.3, 0.9))
    print(f"running {len(scenarios)} scenarios, concurrency={a.concurrency}", flush=True)
    t0 = time.time()
    done = 0

    def task(sc):
        nonlocal done
        r = runner.run_scenario(sc, initials[sc.sid])
        done += 1
        if done % 10 == 0:
            print(f"  {done}/{len(scenarios)} finished ({time.time()-t0:.0f}s)", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        raw = list(ex.map(task, scenarios))
    rows = [runner.collect(sc, initials[sc.sid], r) for sc, r in zip(scenarios, raw)]
    summ = summarize(rows)
    meta = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), "model": settings.llm_model,
        "llm_mode": settings.llm_mode, "concurrency": a.concurrency, "worker_threads": settings.worker_threads,
        "lease_seconds": settings.lease_seconds, "seed": a.seed, "wall_clock_s": round(time.time() - t0, 1),
        "suite": a.suite, "router_mode": settings.router_mode, "strong_model": settings.strong_llm_model or None,
    }
    with conn() as c:
        wids = [r.get("workflow_id") for r in rows if r.get("workflow_id")]
        u = c.execute("""SELECT model, count(*) AS calls, COALESCE(sum(cost_usd),0) AS cost FROM llm_usage
                         WHERE workflow_id = ANY(%s) GROUP BY model""", (wids,)).fetchall()
    meta["llm_usage"] = [{"model": x["model"], "calls": x["calls"], "cost_usd": float(x["cost"])} for x in u]
    path = write_report(rows, summ, meta, Path(a.out))
    print(Path(path).read_text())
    print(f"report: {path}")


if __name__ == "__main__":
    main()
