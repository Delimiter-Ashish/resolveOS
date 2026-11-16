#!/usr/bin/env bash
# Everything: postgres -> vLLM -> API+worker -> evaluation.
set -euo pipefail
D="$(dirname "$0")"
"$D/01_postgres.sh"
"$D/02_vllm.sh"
"$D/03_services.sh"
"$D/04_eval.sh" "$@"
