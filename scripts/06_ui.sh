#!/usr/bin/env bash
# Print how to open the Mission Control UI (served by the API on $API_PORT).
source "$(dirname "$0")/env.sh"
curl -sf "http://127.0.0.1:$API_PORT/healthz" >/dev/null || { echo "API not running: bash scripts/03_services.sh"; exit 1; }
echo "Mission Control UI is served at http://127.0.0.1:$API_PORT/ui"
echo
echo "Open it from your laptop:"
echo "  VS Code (remote / code-server): PORTS tab -> Forward a Port -> $API_PORT -> click the globe icon"
echo "  SSH tunnel:  ssh -L $API_PORT:127.0.0.1:$API_PORT <user>@<host>   then open http://localhost:$API_PORT/ui"
echo
echo "Admin key to paste into the UI login:"
grep ADMIN_API_KEY .env.keys | cut -d= -f2
