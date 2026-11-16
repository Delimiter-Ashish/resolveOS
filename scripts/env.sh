#!/usr/bin/env bash
# Common environment. Source from the other scripts: `source "$(dirname "$0")/env.sh"`
export ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RUNTIME="${RUNTIME:-$ROOT/runtime}"
export PGPORT="${PGPORT:-5433}"
export PGDATA="$RUNTIME/pgdata"
export PGSOCK="${PGSOCK:-/tmp/resolveos_pg_${USER:-$(id -un)}}"
export MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
export HF_HOME="$RUNTIME/hf"; unset HF_TOKEN_PATH HF_HUB_CACHE HUGGINGFACE_HUB_CACHE TRANSFORMERS_CACHE
export LLM_PORT="${LLM_PORT:-8000}"
export API_PORT="${API_PORT:-8080}"
export APP_ENV=venv LLM_ENV=venv
export PYTHONPATH="$ROOT"
export RESOLVEOS_DATABASE_URL="postgresql://resolveos@127.0.0.1:$PGPORT/resolveos"
export RESOLVEOS_API_BASE_URL="http://127.0.0.1:$API_PORT"
export RESOLVEOS_LLM_BASE_URL="http://127.0.0.1:$LLM_PORT/v1"
export RESOLVEOS_WORKER_THREADS="${RESOLVEOS_WORKER_THREADS:-8}"
export RESOLVEOS_LEASE_SECONDS="${RESOLVEOS_LEASE_SECONDS:-5}"
export RESOLVEOS_CHAOS_ENABLED="${RESOLVEOS_CHAOS_ENABLED:-1}"
mkdir -p "$RUNTIME" "$PGSOCK" "$ROOT/results"
cd "$ROOT"
export VENV="${VENV:-$ROOT/resolveos-venv}"   # the single virtualenv for this project
activate() { # activate the project venv (argument ignored, kept for compatibility)
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
}
pgbin() { # directory with initdb/pg_ctl/createdb (bundled in the pixeltable-pgserver wheel unless system postgres exists)
  if command -v pg_ctl >/dev/null 2>&1; then dirname "$(command -v pg_ctl)"; return; fi
  python - <<'PY'
import pathlib, pixeltable_pgserver
print(pathlib.Path(pixeltable_pgserver.__file__).parent / "pginstall" / "bin")
PY
}
