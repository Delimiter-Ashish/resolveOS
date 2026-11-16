# Deploying ResolveOS

## Option A: any VM with Docker (recommended first)
```bash
cp deploy/.env.example deploy/.env      # fill secrets + model settings
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
docker compose -f deploy/docker-compose.yml logs api | grep "UI login"   # first admin password
```
Put Caddy or nginx in front for HTTPS and set `RESOLVEOS_PUBLIC_URL` to the public address
(one-tap approval links and the chat widget use it). Scale workers with `deploy.replicas`.

## Option B: Render / Fly / Railway
Build the root `Dockerfile` twice: one service with `ROLE=api`, one with `ROLE=worker`, both pointing at a
managed Postgres (Neon, Supabase, Render). All four API keys and `RESOLVEOS_SECRET_KEY` must be set as env
vars and shared by both services. `deploy/render.yaml` is a starting Blueprint.

## Model choices
* Hosted only: point `RESOLVEOS_LLM_*` at Gemini's OpenAI-compatible endpoint (no GPU needed).
* Hybrid: `RESOLVEOS_LLM_*` = your vLLM server (routine steps, ~free), `RESOLVEOS_STRONG_LLM_*` = Gemini
  (flagged / complex / recovering steps). Set real prices in `*_PRICE_IN/OUT` so cost dashboards are accurate.

## Production checklist
- `RESOLVEOS_CHAOS_ENABLED=0`, strong `RESOLVEOS_SECRET_KEY`, HTTPS only.
- Start every new org in **shadow** mode; move to **progressive** after a week of reviews.
- Set a Slack webhook in Settings for approvals and alerts.
- Scrape `/metrics` (set `RESOLVEOS_METRICS_TOKEN`), optionally enable OpenTelemetry (`requirements-otel.txt`).
- Back up Postgres; the audit log is append-only and verifiable per workflow.
