#!/usr/bin/env bash
# Pre-fetch model weights into $HF_HOME (run where internet is available).
set -euo pipefail
source "$(dirname "$0")/env.sh"
activate "$LLM_ENV"
python - <<PY
from huggingface_hub import snapshot_download
import os
p = snapshot_download(os.environ["MODEL"])
print("downloaded to", p)
PY
