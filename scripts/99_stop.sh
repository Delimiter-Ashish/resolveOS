#!/usr/bin/env bash
source "$(dirname "$0")/env.sh"
for f in api worker vllm; do
  [ -f "$RUNTIME/$f.pid" ] && kill "$(cat "$RUNTIME/$f.pid")" 2>/dev/null && rm -f "$RUNTIME/$f.pid"
done
pkill -f "resolveos.worker" 2>/dev/null; pkill -f "uvicorn resolveos" 2>/dev/null
activate; "$(pgbin)/pg_ctl" -D "$PGDATA" stop -m fast 2>/dev/null
echo stopped
