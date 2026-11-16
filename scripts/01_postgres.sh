#!/usr/bin/env bash
# User-space PostgreSQL (no root needed). Uses the binaries bundled in the venv.
set -euo pipefail
source "$(dirname "$0")/env.sh"
activate
PGB="$(pgbin)"
if [ ! -d "$PGDATA" ]; then
  "$PGB/initdb" -D "$PGDATA" -U resolveos --auth=trust >/dev/null
fi
if ! "$PGB/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  "$PGB/pg_ctl" -D "$PGDATA" -l "$RUNTIME/postgres.log" -w \
    -o "-p $PGPORT -c listen_addresses=127.0.0.1 -k $PGSOCK -c max_connections=200" start
fi
"$PGB/createdb" -h 127.0.0.1 -p "$PGPORT" -U resolveos resolveos 2>/dev/null || true
echo "postgres ready on 127.0.0.1:$PGPORT"
