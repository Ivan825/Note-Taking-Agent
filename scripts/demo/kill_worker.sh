#!/usr/bin/env bash
# SIGKILL the worker container that is currently processing a job, then watch the job get
# redelivered to a healthy worker and finish.
#   ./scripts/demo/kill_worker.sh <job_id>
set -euo pipefail
GATEWAY="${GATEWAY:-http://localhost:8000}"
JOB="${1:?usage: kill_worker.sh <job_id>}"

worker=$(curl -s "$GATEWAY/jobs/$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("worker_id") or "")')
if [ -z "$worker" ]; then echo "job has not been claimed by a worker yet - try again in a second" >&2; exit 1; fi
container=$(docker ps --filter "id=$worker" --format '{{.Names}}' | head -1)
if [ -z "$container" ]; then echo "worker $worker is not running" >&2; exit 1; fi

echo "→ SIGKILL $container (worker $worker) while job is $(curl -s "$GATEWAY/jobs/$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
docker kill --signal KILL "$container" >/dev/null
echo "→ killed at $(date +%T); RabbitMQ will redeliver the unacked message to another worker"
echo "  (restart it later with: docker compose up -d --scale worker=2)"
exec "$(dirname "$0")/watch_job.sh" "$JOB"
