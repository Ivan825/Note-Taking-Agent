#!/usr/bin/env bash
# Force frame capture failures to demonstrate graceful degradation.
#   ./scripts/demo/frame_fail.sh first:2      # first 2 flagged frames fail -> COMPLETED_WITH_WARNINGS
#   ./scripts/demo/frame_fail.sh all          # every frame fails -> text-only page, still published
#   ./scripts/demo/frame_fail.sh off          # back to normal
set -euo pipefail
GATEWAY="${GATEWAY:-http://localhost:8000}"
SPEC="${1:-first:1}"
[ "$SPEC" = "off" ] && SPEC=""
curl -s -X POST "$GATEWAY/debug/chaos" -H 'content-type: application/json' -d "{\"frame_fail\": \"$SPEC\"}" | python3 -m json.tool
