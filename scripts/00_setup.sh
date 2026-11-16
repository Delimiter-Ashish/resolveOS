#!/usr/bin/env bash
# One-time setup: ONE virtualenv (resolveos-venv) inside the project folder.
# Needs a Python >= 3.10 (on a cluster: `module load python3/<version>` first).
set -euo pipefail
source "$(dirname "$0")/env.sh"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3,10), "need Python >= 3.10 (module load python3/3.10.12)"'
"$PY" -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt        # app deps + postgres binaries (pixeltable-pgserver)
pip install -r requirements-llm.txt    # vLLM (large download, pulls torch/CUDA)
echo "setup done. venv: $VENV"
echo "Next: scripts/download_model.sh (needs internet), then scripts/run_all.sh"
