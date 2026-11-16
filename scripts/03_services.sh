#!/usr/bin/env bash
# Initialise the DB, then start the REST API and the durable worker.
set -euo pipefail
source "$(dirname "$0")/env.sh"
activate "$APP_ENV"
python -m resolveos.db init
nohup uvicorn resolveos.api.app:app --host 127.0.0.1 --port "$API_PORT" --workers "${API_WORKERS:-3}" \
  --log-level warning > "$RUNTIME/api.log" 2>&1 &
echo $! > "$RUNTIME/api.pid"
nohup python -m resolveos.worker > "$RUNTIME/worker.log" 2>&1 &
echo $! > "$RUNTIME/worker.pid"
for i in $(seq 1 60); do curl -sf "http://127.0.0.1:$API_PORT/healthz" >/dev/null && break; sleep 1; done
curl -sf "http://127.0.0.1:$API_PORT/healthz" && echo " api up"
