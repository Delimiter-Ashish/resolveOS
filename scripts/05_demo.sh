#!/usr/bin/env bash
# One refund that needs human approval, end to end, then print its audit trail.
set -euo pipefail
source "$(dirname "$0")/env.sh"
activate "$APP_ENV"
python -m resolveos.eval.runner --reset --categories refund_approved --limit 1 --out results/demo
python -m resolveos.trace "$(python - <<'PY'
from resolveos.db import conn
with conn() as c:
    print(c.execute("SELECT id FROM workflows ORDER BY created_at DESC LIMIT 1").fetchone()["id"])
PY
)"
