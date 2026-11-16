# ResolveOS: 3-minute live demo

Setup (once per session): services running (`bash scripts/03_services.sh`), then `bash scripts/06_ui.sh`
to get the URL + admin key. Open the UI, paste the key. Keep a terminal visible next to the browser.

| time | do | say |
|---|---|---|
| 0:00 | Mission Control | "ResolveOS is an autonomous ops agent. Every decision it makes streams here from a tamper-evident audit log." |
| 0:20 | Demo Lab → **Damaged item refund** | "A small refund is inside policy, so the agent reads the order, refunds, and closes the ticket on its own." Point at the graph lighting up. |
| 0:45 | Demo Lab → **High-value return** | "Above $100 the policy engine requires a human. The workflow pauses and releases its thread. It is just a checkpoint in Postgres now." |
| 1:05 | Approvals → read the card → **Approve** | "The approval is bound to this exact order and amount by a hash. The agent can't change the amount or reuse it." Graph resumes and goes green. |
| 1:30 | Demo Lab → **Fraud-hold refund** | "Policy denies. The agent never touches the payment API, and the server would block it even if it tried." |
| 1:50 | Demo Lab → **Worker crash mid-workflow** | "The worker process dies right after the refund commits. Watch: lease expires, another worker resumes from the checkpoint, and the retry hits the idempotency key. Exactly one refund." Open the workflow: `resumed ×1`, `Duplicate prevented`. |
| 2:20 | Terminal: `bash scripts/08_burst.sh 15` then immediately `bash scripts/07_kill_worker.sh` | "Real `kill -9` on the worker under load. Nothing is lost; everything resumes." |
| 2:40 | Audit Chain → pick a workflow → **Verify chain** | "Every event is sha256-chained to the previous one; the table is append-only at the DB level." |
| 2:50 | Evaluation | "120-scenario benchmark judged on final database state, with its success rate and p95 latency." |

Recording tip: browser at 1600×1000, dark OS theme, hide bookmarks bar (Ctrl+Shift+B).
