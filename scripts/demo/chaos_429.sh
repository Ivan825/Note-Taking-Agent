#!/usr/bin/env bash
# Make the next N calls to an external API behave exactly like an HTTP 429.
#   ./scripts/demo/chaos_429.sh llm|notion|youtube [N]
set -euo pipefail
GATEWAY="${GATEWAY:-http://localhost:8000}"
API="${1:?usage: chaos_429.sh llm|notion|youtube [count]}"
N="${2:-1}"
curl -s -X POST "$GATEWAY/debug/chaos" -H 'content-type: application/json' -d "{\"${API}_429\": $N}" | python3 -m json.tool
echo "→ next $N $API call(s) will be rate-limited; affected jobs wait with backoff instead of failing"
