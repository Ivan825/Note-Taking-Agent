#!/usr/bin/env bash
# Submit a video that does not exist / is unavailable. It is retried with backoff
# (10s, 30s, 90s, 90s by default) and then routed to the dead-letter queue (jobs.dlq).
#   ./scripts/demo/private_video.sh [video_id]
set -euo pipefail
GATEWAY="${GATEWAY:-http://localhost:8000}"
VIDEO="${1:-xxxxxxxxxx0}"
TOKEN="${NOTION_TOKEN:-demo-token-not-needed-for-this-failure}"
resp=$(curl -s -X POST "$GATEWAY/jobs" -H 'content-type: application/json' \
  -d "{\"youtube_url\": \"https://www.youtube.com/watch?v=$VIDEO\", \"notion_token\": \"$TOKEN\"}")
echo "$resp"
job=$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
echo "→ watch it retry and dead-letter (RabbitMQ UI: http://localhost:15672, user/pass notes/notes, queue jobs.dlq)"
exec "$(dirname "$0")/watch_job.sh" "$job"
