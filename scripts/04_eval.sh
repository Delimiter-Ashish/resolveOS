#!/usr/bin/env bash
# Run the 120-scenario evaluation. Extra args are passed through (e.g. --concurrency 1).
set -euo pipefail
source "$(dirname "$0")/env.sh"
activate "$APP_ENV"
python -m resolveos.eval.runner --reset --concurrency "${CONCURRENCY:-8}" "$@"
