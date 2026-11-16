#!/usr/bin/env bash
# Fire N demo tickets at once (default 12) so Mission Control lights up. Usage: bash scripts/08_burst.sh 20
source "$(dirname "$0")/env.sh"
N="${1:-12}"
KEY=$(grep ADMIN_API_KEY .env.keys | cut -d= -f2)
KINDS=(refund_auto refund_auto lost_package multi_step policy_denied refund_approval api_fault crash_resume)
for i in $(seq 1 "$N"); do
  k=${KINDS[$((RANDOM % ${#KINDS[@]}))]}
  curl -s -X POST "http://127.0.0.1:$API_PORT/v1/demo/launch" -H "Authorization: Bearer $KEY" \
       -H "Content-Type: application/json" -d "{\"scenario\":\"$k\"}" | head -c 90; echo
done
