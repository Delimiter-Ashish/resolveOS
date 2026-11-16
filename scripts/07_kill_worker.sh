#!/usr/bin/env bash
# LIVE DEMO: kill -9 the worker process mid-flight and start a fresh one.
# In-flight workflows keep their Postgres checkpoints; once their lease expires
# (RESOLVEOS_LEASE_SECONDS) the new worker re-claims and resumes them.
source "$(dirname "$0")/env.sh"
activate
if [ -f "$RUNTIME/worker.pid" ]; then
  OLD=$(cat "$RUNTIME/worker.pid")
  kill -9 "$OLD" 2>/dev/null && echo "killed worker pid $OLD (SIGKILL, no cleanup)"
fi
sleep 1
nohup python -m resolveos.worker > "$RUNTIME/worker.log" 2>&1 &
echo $! > "$RUNTIME/worker.pid"
echo "started fresh worker pid $(cat "$RUNTIME/worker.pid"); orphaned workflows resume after ${RESOLVEOS_LEASE_SECONDS}s lease expiry"
