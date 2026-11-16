# ResolveOS: Complete Project Guide

> Handover document: what ResolveOS is, how every part works, how to run it, what has been measured,
> what is known to be weak, and where it goes next. Written so a teammate can pick it up cold.

**Stack:** Python 3.10+, LangGraph, FastAPI, PostgreSQL 16, vLLM (Qwen2.5-7B-Instruct), Gemini (OpenAI-compatible
endpoint), vanilla JS single-file UI. About 6,500 lines of code, 82 HTTP endpoints, 12 unit tests, a 120-scenario
benchmark plus a 10-scenario adversarial suite.

---

## 1. What it is (one paragraph)

ResolveOS is an **autonomous operations agent platform**. A business ticket ("my order arrived broken, refund me",
"unlock my account", "restock SKU-120") comes in from email, chat, Slack, a webhook or the API. A LangGraph agent
reads the relevant records through authenticated REST tools, decides what to do, and executes side effects
(refunds, cancellations, purchase orders, CRM writes) **only through a policy engine** that can allow, deny or require
human approval. Every run is checkpointed in Postgres so it survives crashes, every write is idempotent so retries never
double-charge, and every event lands in a hash-chained, append-only audit log. Around that core sits a product layer:
connectors for any REST API, plain-English policies with what-if simulation, shadow mode with earned autonomy,
guardrails against PII and prompt injection, reply drafting, knowledge-base retrieval, multi-tenant login, a cost-aware
model router, monitoring, ROI reporting and a deploy kit.

---

## 2. Timeline: how we got here

| Version | What changed |
|---|---|
| v1 | Core platform: schema, policy engine, auth, idempotent mutations, audit chain, LangGraph agent, durable worker, 120-scenario eval harness, run scripts |
| v2 | Single venv instead of conda (target cluster has no conda); Postgres binaries come from the `pixeltable-pgserver` wheel (no root) |
| first real run | Qwen2.5-7B on A100 via vLLM: **119/120 = 99.2% task success, p95 5.9 s** (details in §9) |
| v3 | Mission Control UI (live stream, animated execution graph, approvals inbox, audit verifier, demo lab, eval page), live kill-worker and burst scripts, DEMO.md. cluster fixes baked in (`--enforce-eager`, HF cache path) |
| v4 | 15 product features (§6), Gemini as strong model in a router, multi-tenancy, deploy kit, 47-check end-to-end test |
| v4 hotfix | UI also sends the token as `X-Api-Key`, because some web proxies (e.g. Open OnDemand) drop the `Authorization` header |
| v5 | Visual intelligence layer (roadmap A1-A3): Live Graph from the real compiled LangGraph topology (`api/insights.py`), Replay Theater with per-step DB effects, Operations Galaxy particle map, Analytics (Sankey, heatmap, trust curves, cost series) |

---

## 3. Architecture

```
      channels                         API process(es)  ── FastAPI ──────────────────────────────────────────
 email / webhook / Slack /     ──►  intake ──► guardrails (PII redact, injection score) ──► tickets + workflows
 chat widget / REST / UI            │
                                    │   console + product APIs (UI, policies, connectors, KB, replies, ROI, metrics)
                                    │   mutation core: auth → idempotency → policy.decide → approval check
                                    │                  → business handler → idempotency record → audit  (1 txn)
                                    ▼
                               PostgreSQL ── business tables · workflows · approvals · audit_log (hash chain)
                                    ▲          · LangGraph checkpoints · llm_usage · policy_rules · connectors · kb
                                    │
      worker process ── N threads, each: claim workflow (SKIP LOCKED + lease + heartbeat) ── run LangGraph:
          agent(LLM via router) → gate(policy dry-run) ─┬─ ALLOW ────────────► execute (REST tool) → agent …
                                                        ├─ REQUIRE_APPROVAL ─► request_approval → await (interrupt; thread released)
                                                        └─ DENY ─► told to the agent
          execute fails transiently → retry/backoff/circuit breaker → recover → auto-escalate
          on finish → reply draft · every 30 s → alert rules

      model router ── primary: local vLLM (Qwen2.5-7B) · strong: Gemini Flash · fallback both ways · metered in USD
```

Key design choice: **the agent never touches the database directly.** It only calls the same authenticated REST
endpoints any client would. Policy is enforced *inside* those endpoints, so a buggy or manipulated agent cannot bypass it
by skipping its own gate.

---

## 4. Repository map

```
ResolveOS/
├── resolveos/
│   ├── config.py            every setting (RESOLVEOS_* env vars, .env, .env.keys)
│   ├── schema.sql           full schema, idempotent (safe to re-run; v2 part uses ALTER ... IF NOT EXISTS)
│   ├── db.py                connection pools, init (schema + keys + admin user + checkpointer), reset
│   ├── auth.py              API keys + signed sessions, scrypt passwords, role scopes, org resolution
│   ├── common.py            canonical JSON, action hashing (binds approvals to exact args)
│   ├── policy.py            built-in rules + decide(): builtin → custom rules → guardrail lock → autonomy
│   ├── rules.py             custom-rule DSL, natural-language compiler, what-if simulator
│   ├── autonomy.py          shadow / progressive / autonomous, trust stats
│   ├── guardrails.py        PII redaction (Luhn, SSN, IBAN), prompt-injection scoring
│   ├── tickets.py           single create_ticket() used by every channel
│   ├── knowledge.py         chunking, PDF text, Postgres full-text retrieval
│   ├── replies.py           reply drafting + delivery (SMTP, Slack, webhook, chat, outbox)
│   ├── notify.py            Slack + signed one-tap approval links
│   ├── observability.py     OpenTelemetry spans (optional) + alert rules
│   ├── audit.py             hash-chained append-only log + verifier
│   ├── worker.py            leases, heartbeats, crash resume, approval parking/expiry, reply + alert hooks
│   ├── trace.py             CLI pretty-printer for a workflow's audit trail
│   ├── agent/
│   │   ├── graph.py         LangGraph state machine (nodes, routing, crash points)
│   │   ├── tools.py         built-in tools + dynamic connector tools per org
│   │   ├── client.py        HTTP client: retries, backoff+jitter, circuit breaker, idempotency keys
│   │   ├── llm.py           providers, router, metering, fallback; scripted "fake" agent for smoke tests
│   │   ├── prompts.py       system prompt
│   │   └── check.py         `python -m resolveos.agent.check` provider/tool-calling test
│   ├── api/
│   │   ├── app.py           business REST API, approvals, workflows, audit
│   │   ├── mutation.py      the transactional mutation core (+ fault injection)
│   │   ├── console.py       UI serving, event stream (SSE + polling), stats, demo lab, eval results
│   │   └── product.py       auth/orgs, settings, autonomy, policies, connectors, KB, replies, feedback,
│   │                        intake channels, one-tap approvals, ROI, costs, alerts, /metrics
│   ├── connectors/
│   │   ├── openapi.py       OpenAPI 3 → tool definitions
│   │   └── demo_crm.py      mock CRM (its own OpenAPI), mounted at /mock/crm
│   ├── eval/
│   │   ├── scenarios.py     120 core + 10 adversarial deterministic scenarios
│   │   ├── judge.py         ground truth = final DB state
│   │   ├── runner.py        harness, simulated approver, metrics, reports
│   │   └── regression.py    capture human corrections → replayable tests
│   └── ui/index.html, ui/widget.js
├── scripts/                 00_setup … 99_stop (see §8)
├── tests/                   test_unit.py, test_product.py
├── deploy/                  Dockerfile companion files: docker-compose.yml, render.yaml, DEPLOY.md, .env.example
├── Dockerfile, requirements*.txt, .env.example, README.md, DEMO.md, docs/PROJECT_GUIDE.md
```

---

## 5. Core engine, in depth

### 5.1 A ticket's life
1. **Intake** (`tickets.create_ticket`): guardrails redact PII and score injection; the ticket and a `queued` workflow are
   inserted in one transaction; flags (`injection`, `pii`) are stored on the workflow; audit events are written.
2. **Claim** (`worker.claim`): a worker thread picks the oldest `queued` (or `running` with an **expired lease**) row with
   `FOR UPDATE SKIP LOCKED`, sets itself as lease owner, and starts a heartbeat that renews the lease every lease/3 s.
3. **Graph run** (`agent/graph.py`), checkpointed in Postgres at every node:
   - `agent`: router picks a model, LLM proposes exactly one tool call (extra calls are rejected back to it).
   - `gate`: args validated; for writes, `POST /v1/policy/check` (dry run of `policy.decide`).
   - `request_approval` → `await_approval`: approval row created; `interrupt()` pauses the graph and **releases the
     thread**; the worker parks the workflow as `awaiting_approval`. The decision endpoint flips it back to `queued`.
   - `execute`: the REST call with a stable `Idempotency-Key = workflow_id:tool_call_id` (+ `approval_id` if any).
   - `recover`: when a call is still failing after retries, back off and retry the stage; then auto-escalate.
   - Terminal tools `resolve_ticket` / `escalate_ticket` end the run. Loop guards: step budget, "nudge" when the model
     answers in prose, auto-escalation.
4. **Finish**: workflow `completed` / `escalated` / `failed`; reply drafted; result stats stored.

### 5.2 The mutation core (`api/mutation.py`)
One database transaction does: advisory lock on (principal, idempotency key) → if the key exists, **replay** the stored
response (same request hash required, otherwise 422) → else `policy.decide` → verify and **consume** the approval →
run the business handler → store the idempotency record → audit. So a write and its idempotency record either both
commit or neither does. That is what makes "worker died after the refund but before saving the checkpoint" safe: the
resumed run sends the same key and gets the original response back.

### 5.3 Policy (`policy.py`, `rules.py`, `autonomy.py`)
`decide(action, args, org, workflow)` layers four checks:
1. **Built-in rules** (hard safety): refund window 30 days, only delivered orders, never over remaining balance, fraud
   hold, refunds > $100 need approval; credits > $25 approval, > $200 denied; POs > $2,000 approval, discontinued SKUs
   denied; admin-account unlock/reset need approval; resets need verified identity; reship only lost or ≥ 5 days late,
   > $300 needs approval; cancel/address change only before shipping. Connector reads allowed, writes need approval.
2. **Custom rules**: can only tighten built-ins (ALLOW → REQUIRE_APPROVAL → DENY). For connector actions they may also
   relax the default (e.g. allow `add_crm_note`).
3. **Guardrail lock**: if the ticket was flagged as prompt injection, every write requires a human.
4. **Autonomy**: `shadow` turns every ALLOW into REQUIRE_APPROVAL; `progressive` allows an action automatically only after
   it has ≥ N human reviews with approval rate ≥ threshold (defaults 10, 95%); `autonomous` leaves policy alone.

Approvals are bound to `sha256(action + normalized policy-relevant args)`, single-use, workflow-scoped, expiring (15 min
default), and the requester can never approve its own request (separation of duties).

### 5.4 Reliability mechanics
| Failure | What happens |
|---|---|
| API returns 502/503/504/429 or times out | client retries with exponential backoff + jitter (5 attempts) |
| Endpoint keeps failing | per-endpoint circuit breaker opens for 3 s; graph goes to `recover`, then auto-escalates |
| Response lost after commit | retry hits the idempotency key → original response replayed, no duplicate |
| Worker `kill -9` mid-run | heartbeat stops → lease expires → another worker resumes from the last checkpoint |
| LLM provider error | retries, then router falls back to the other provider |
| Unhandled exception | requeued up to 3 attempts, then `failed` (raises an alert) |
| Human never answers | approval expires → workflow resumes and treats it as declined |

### 5.5 Audit log
Every event row stores `hash = sha256(prev_hash ‖ canonical(event))`, one chain per workflow. A database trigger rejects
UPDATE/DELETE. `GET /v1/workflows/{id}/audit/verify` recomputes the chain; the UI has a Verify button.

---

## 6. The 15 product features

| # | Feature | How it works | Where |
|---|---|---|---|
| 1 | Connector SDK | Parse an OpenAPI 3 spec (resolves `$ref`s) → one tool per operation with a JSON schema. GET = read (allowed), others = write (policy action `connector.<name>.<op>`, approval by default, idempotent, audited). Calls are proxied through `/v1/connectors/{name}/invoke`, so they go through the mutation core. Tools appear for the agent within ~2 s of registration. | `connectors/`, Connectors page |
| 2 | Natural-language policies | Text → JSON rule via the strong LLM in JSON mode; deterministic regex compiler as fallback. Rule = action glob + AND-ed conditions + effect. | `rules.py`, Policies page |
| 3 | Shadow mode + progressive autonomy | See §5.3. Reviews of would-be-automatic actions update `trust_stats`; a rejection lowers the rate and can demote. | `autonomy.py`, Autonomy page |
| 4 | Guardrails | Luhn-validated card numbers, SSNs, IBAN-like accounts redacted before storage (raw text never persisted). Weighted regex detectors for injection (≥ 0.6 → flagged); flagged ticket text is wrapped and the agent is told it is untrusted. | `guardrails.py` |
| 5 | Self-growing regression suite | A rejected approval or an operator's "this was wrong" captures the ticket, a snapshot of touched records (timestamps stored as ages) and the forbidden actions. Replay re-seeds each case and fails if the agent proposes or executes a forbidden action again. | `eval/regression.py`, workflow page, Evaluation page |
| 6 | Multi-channel intake | Email webhook (`from/subject/text`), generic webhook (with callback URL), Slack slash command (signature-verified), embeddable chat widget (rate-limited, per-org token, signed polling key). Unknown emails become guest customers. | `/v1/intake/*`, `ui/widget.js` |
| 7 | Reply drafting | After a run, an LLM writes the customer reply from the **audit log of what actually happened** + knowledge excerpts, in the org's tone; template fallback. Sent automatically only if org settings allow, else waits in the Reply Inbox. | `replies.py`, Reply Inbox |
| 8 | One-tap approvals | HMAC-signed, expiring link per approval, posted to Slack. Opening shows a confirmation page; the decision needs a POST, so link previewers cannot approve. | `notify.py`, `/approve/{id}` |
| 9 | Knowledge RAG | Text/Markdown/PDF → ~900-char chunks → Postgres `tsvector` + GIN index, ranked with `ts_rank_cd`; agent tool `search_knowledge`, citations `[kb:id]`. | `knowledge.py`, Knowledge page |
| 10 | What-if simulator | Replays every `policy.checked` event of the last N days under current rules vs. current + proposed; outputs a transition matrix, $ newly gated/denied/allowed, examples. | `rules.simulate`, Policies page |
| 11 | Multi-tenant orgs | Orgs, users (scrypt), roles (admin/operator/approver), signed sessions; every platform table carries `org_id`; console queries are scoped; agent actions inherit the workflow's org. | `auth.py`, Settings page |
| 12 | Model router + costs | Primary (vLLM) for routine steps; strong (Gemini) when the ticket is flagged, complex (≥ 2 ids or long), the agent is recovering, or the run is deep. Fallback on failure. Every call logged with tokens, latency, USD. | `agent/llm.py`, ROI & Costs page |
| 13 | Observability | `/metrics` (Prometheus text), optional OpenTelemetry span per graph node, alert rules every 30 s (escalation rate, approval backlog, failures, LLM error rate) → UI banner + Slack. | `observability.py` |
| 14 | ROI dashboard | Automation rate, hours and $ saved (from the org's own minutes-per-ticket and hourly cost), money protected by denials/rejections, LLM spend, net. Assumptions shown on the page. | `/v1/roi`, ROI page |
| 15 | Deploy kit | Dockerfile (ROLE=api/worker), docker-compose with Postgres, 2 workers and an optional GPU vLLM profile, Render blueprint, deployment checklist. | `Dockerfile`, `deploy/` |

---

## 7. The UI (Mission Control)
Single file, no build step, served by the API at `/ui`. Works behind proxies (relative URLs; token sent as both
`Authorization` and `X-Api-Key`; SSE with automatic fallback to polling when a proxy buffers the stream).

| Page | What you see |
|---|---|
| Mission Control | KPI tiles, live event stream (color-coded), recent workflows |
| Live Graph | The compiled LangGraph topology exported from code; badges = workflows at each node now, edge width = 24h traffic, node inspector |
| Galaxy | Canvas force map: channels → core → models/tools/guardrails, live event particles |
| Analytics | Decision Sankey, weekday×hour volume heatmap, p50-by-hour, per-action trust curves, hourly LLM cost |
| Workflows → detail | **Replay Theater** (scrub/auto-play any run; graph animates, DB-effects panel updates per step), animated execution graph (active node glows, traversed edges flow, crash/deny/wait states), timeline with payloads, ticket, outcome, guardrail badges, model-router table, reply, "was this correct?" feedback |
| Approvals | Pending cards with exact args and policy reason, Approve/Reject, history |
| Reply Inbox | Editable drafts with citations, Send/Discard |
| Policies & What-if | NL → rule → impact simulation → save; rule list with toggles |
| Autonomy | Mode switch, trust table with progress bars |
| Connectors | Register from OpenAPI URL, demo CRM button, operations with policy defaults |
| Knowledge | Upload text/PDF, retrieval tester |
| ROI & Costs | Value KPIs, per-model calls/tokens/cost/latency, routing reasons, assumptions |
| Audit Chain | Block-by-block hash chain, one-click verify |
| Evaluation | Latest benchmark report + regression suite status |
| Demo Lab | 11 one-click scenarios (incl. injection, PII, CRM, crash, API fault) + custom ticket |
| Settings | Tone, thresholds, alerts, Slack webhook, widget snippet, intake token, team users |

---

## 8. Running it

### 8.1 Cluster / GPU box (how we run it)
```bash
cd <workspace>/ResolveOS
module load python3/3.10.12                 # any Python ≥ 3.10
bash scripts/00_setup.sh                    # once: ./resolveos-venv with app + vLLM + Postgres binaries
bash scripts/download_model.sh              # once: Qwen2.5-7B-Instruct into runtime/hf (needs internet)
cp .env.example .env                        # add the Gemini key + exact model id (strong model)
bash scripts/01_postgres.sh && bash scripts/02_vllm.sh && bash scripts/03_services.sh
python -m resolveos.agent.check             # both providers must print OK
grep "UI login" .env.keys                   # first admin login
bash scripts/06_ui.sh                       # URL + admin API key; forward port 8080 to open the UI
bash scripts/04_eval.sh                     # 120-scenario benchmark (add --suite all for +10 adversarial)
bash scripts/99_stop.sh                     # stop everything (frees the GPU)
```
Other scripts: `05_demo.sh` (one approval demo + trace), `07_kill_worker.sh` (real `kill -9` + restart),
`08_burst.sh N` (N demo tickets at once), `run_all.sh` (01→04).
`python -m resolveos.eval.regression --yes` replays captured regression cases (resets business tables).

### 8.2 Gotchas we hit (all fixed in the scripts)
| Symptom | Cause | Fix |
|---|---|---|
| `conda: command not found` | no conda on the cluster | single venv + Postgres from a pip wheel |
| HF `PermissionError` on someone else's cache | `HF_HOME` set globally in `.bashrc` | `env.sh` forces `HF_HOME=$RUNTIME/hf` |
| vLLM "Engine core initialization failed" after loading | torch.compile / CUDA-graph warmup fails on the node | `--enforce-eager` |
| vLLM OOM on shared node | other users' processes on the GPU | `GPU_UTIL` default 0.6; script picks the least-used GPU if none is pinned |
| UI says "Key rejected" right after a correct login | OnDemand proxy strips `Authorization` | UI also sends `X-Api-Key` |
| Live stream shows "polling" | proxy buffers SSE | automatic polling fallback; nothing to do |
| Gemini 400 | placeholder key / wrong model id | put the real key and model id in `.env`; verify with `agent.check` |

### 8.3 Key configuration (`RESOLVEOS_*`)
`DATABASE_URL`, `LLM_BASE_URL/MODEL/API_KEY` (primary), `STRONG_LLM_BASE_URL/MODEL/API_KEY` (Gemini),
`ROUTER_MODE` (auto|primary|strong), `*_PRICE_IN/OUT` (USD per 1M tokens), `WORKER_THREADS`, `LEASE_SECONDS`,
`CHAOS_ENABLED` (eval only), `PUBLIC_URL`, `SMTP_*`, `SLACK_SIGNING_SECRET`, `METRICS_TOKEN`, `OTEL_ENABLED`,
policy thresholds (`REFUND_AUTO_LIMIT`, `CREDIT_AUTO_LIMIT`, ...). API keys and `SECRET_KEY` are generated into
`.env.keys` by `python -m resolveos.db init` (never commit it; `.env` and `.env.keys` are git-ignored).

---

## 9. Evaluation

### 9.1 Protocol
* **120 core scenarios**, seed 7, 12 categories × 10: auto refunds, approved refunds, rejected refunds, policy-denied
  refunds, cancellation, address change, inventory restock, account recovery, shipment issues, multi-step tickets,
  API fault injection (fail-before, fail-after-commit, timeout-after-commit, slow), crash-resume (worker killed before or
  after a tool, after the LLM call, during approval, after approval).
* **10 adversarial scenarios** (`--suite adversarial`): 5 prompt-injection attacks demanding oversized refunds (pass =
  no money moves; the simulated human rejects), 5 tickets containing card numbers/SSNs (pass = normal handling).
* **Success** = final database state equals ground truth exactly (missing, wrong, duplicated or policy-violating side
  effects all fail) + acceptable terminal state + expected approval behaviour. Not "did the model say the right thing".
* **Latency** = `finished_at − created_at − Σ approval waits`, measured under concurrency 8.
* The runner refuses to run if custom rules are enabled or the org is not `autonomous` (results would not be comparable).

### 9.2 Results so far (real model)
Qwen2.5-7B-Instruct on one A100 (vLLM, bf16), concurrency 8, 8 worker threads, lease 5 s, before the v4 router existed:

| Metric | Value |
|---|---|
| Task success | **119/120 = 99.2%** |
| Execution latency | p50 2.1 s, **p95 5.9 s**, p99 9.8 s (p95 without crash-resume 4.6 s) |
| Audit chains valid | 100% |
| LLM calls | 349 (2.9 per ticket), 306 tool calls |
| Duplicates prevented by idempotency | 12; crash resumes 10; retries absorbed 11 |
| Only failure | S105 (API fault after commit on an address change): the agent escalated instead of retrying; safe, no wrong action |

**To do:** re-run on v4 with the Gemini router and `--suite all`, and record the model mix and cost.

### 9.3 Honest caveats
Scenarios are synthetic and templated, which is why a 7B model scores high. The policy engine also prevents many
possible mistakes by construction. A harder, paraphrased and ambiguous scenario set is on the roadmap and should be
expected to score lower; that number will be more convincing than 99%.

---

## 10. Testing
* `pytest -q tests` → 12 unit tests (scenario determinism, action hashing, guardrails incl. zero false positives on the
  benchmark, rule tightening semantics, NL heuristic compiler, OpenAPI parsing).
* End-to-end with the scripted agent (`RESOLVEOS_LLM_MODE=fake`): 47 checks covering every feature (login, injection
  lock, PII redaction, connector read/write + approval, NL rule enforcement, shadow → progressive promotion, regression
  capture, KB search, all 4 intake channels, reply send, one-tap approval incl. bad token, ROI/costs/metrics/alerts,
  roles, and 5 tenant-isolation checks). All pass.
* The scripted agent cannot do account/shipment/multi-step tickets, so those categories fail in fake mode by design;
  the real model numbers are in §9.2.

---

## 11. Security model
API keys stored as SHA-256 only; sessions HMAC-signed with expiry; passwords scrypt; role scopes on every endpoint;
org scoping on every console query; separation of duties on approvals; approval ↔ exact-args binding; idempotency-key
reuse with a different body rejected; PII redacted before storage; injection-flagged tickets cannot act without a human;
append-only hash-chained audit; one-tap links signed + expiring + POST-only; widget/intake tokens per org with rate
limiting; Slack request signatures verified when a secret is set; fault injection only when explicitly enabled.

**Before hosting:** strong admin passwords (remove the bootstrap admin), HTTPS, `CHAOS_ENABLED=0`, rotate any secret
that was ever pasted into chat, set `METRICS_TOKEN`, start new orgs in shadow mode.

---

## 12. Known limitations
* Business domain (orders, customers, ...) is a demo store in our own tables; real stores would come in through
  connectors (§6.1).
* Knowledge retrieval is lexical (Postgres full text); no embeddings or reranker yet.
* Injection detection is heuristic (regex weights); a model-based classifier would catch paraphrased attacks.
* Polling-based worker queue (0.2 s); fine for thousands of tickets/hour, LISTEN/NOTIFY would cut idle load.
* Business tables are shared across orgs (only platform tables are tenant-scoped).
* The NL policy compiler only supports conditions on action arguments (not on derived facts like customer tier).
* Alert evaluation runs in the worker process; with many workers each evaluates (idempotent, but redundant).

---

## 13. Roadmap: the next level (logical, deep, and visual)

Each item is chosen because it is technically real, measurable, and visible in a demo.

### A. Visual intelligence (the "wow", grounded in real data)
1. **Replay Theater.** Scrub a slider through any workflow: the graph animates node by node, the LLM's tool call and
   the policy verdict appear at each step, and a side panel shows the **database diff** (before/after rows). Data already
   exists in the audit log + checkpoints.
2. **Operations Galaxy.** A live force-directed (d3-force / WebGL) map: channels → tickets → tools → external systems,
   with particles flowing along edges as events stream in; red pulses for denials, orange for crashes. Doubles as a load
   test visualiser.
3. **Decision Sankey + heatmaps.** Channel → category → policy verdict → outcome flows; latency heatmap (hour × category);
   cost waterfall per ticket; trust curves per action over time; animated what-if transition matrix.

### B. Smarter agent (research-grade, great for a PhD story)
4. **Multi-agent supervisor.** Triage agent routes to specialists (billing, logistics, identity) built as LangGraph
   subgraphs, each with a narrower toolset; measure success and cost vs. the single agent.
5. **Plan → verify → execute.** The agent first emits an explicit plan; the platform checks the whole plan against
   policy (and against invariants like "total refunds ≤ order total") **before** any side effect; the UI shows the plan.
6. **Calibrated confidence.** Self-consistency sampling on decisive steps; low agreement → escalate. Report expected
   calibration error and the success/escalation trade-off curve.
7. **Distillation: Gemini → Qwen 7B on the A100.** Collect strong-model trajectories that passed the judge, LoRA-fine-tune
   the 7B, and re-run the benchmark. Target: same success with fewer escalations to the strong model and lower $/ticket.
8. **Policy mining.** Learn from approval history ("you approved 48/50 refunds under $150") and *suggest* rules, each
   shipped with its what-if simulation.
9. **Hybrid retrieval.** pgvector embeddings + full text + a reranker; measure answer citation accuracy.

### C. Harder, more honest evaluation
10. **Paraphrased + ambiguous suite** (LLM-generated variants, typos, multiple languages, missing ids) and an
    **LLM red-team generator** that keeps producing new injection attempts; track attack success rate over time.
11. **Ablations:** remove policy gate / retries / checkpointing / router and show what breaks (a results table).
12. **Load test:** sustained workflows/minute and p95 under load with multiple worker processes.

### D. Production depth
13. **Sagas with compensation** (undo a refund if a later step of the same plan fails).
14. **LISTEN/NOTIFY** queue + SSE without polling; **per-connector circuit-breaker dashboard**.
15. **Model-based injection classifier** and output guardrails (block replies that promise things not in the audit log).

**Status:** A1-A3 shipped in v5. **Recommended next:** B5 Plan-verify-execute + B4 multi-agent (depth), then
B7 Distillation with C10/C11 (research results).
