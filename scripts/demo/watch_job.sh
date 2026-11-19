#!/usr/bin/env bash
# Print a job's status every second until it reaches a terminal state, then its results.
#   ./scripts/demo/watch_job.sh <job_id>
set -euo pipefail
GATEWAY="${GATEWAY:-http://localhost:8000}"
JOB="${1:?usage: watch_job.sh <job_id>}"
start=$(date +%s)
FMT=$(cat <<'PY'
import json, sys
d = json.load(sys.stdin)
worker = d.get("worker_id") or "-"
print(f"{d['status']:<24} {d['progress']:>3}%  worker={worker:<12}  {d.get('status_detail') or ''}")
print("TERMINAL" if d["status"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED") else "")
PY
)
last=""
while true; do
  out=$(curl -s "$GATEWAY/jobs/$JOB" | python3 -c "$FMT")
  line=$(echo "$out" | head -1)
  if [ "$line" != "$last" ]; then echo "[$(( $(date +%s) - start ))s] $line"; last="$line"; fi
  if echo "$out" | grep -q TERMINAL; then
    echo "--- GET /jobs/$JOB/results"
    curl -s "$GATEWAY/jobs/$JOB/results" | python3 -m json.tool
    exit 0
  fi
  sleep 1
done
