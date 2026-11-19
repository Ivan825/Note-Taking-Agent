#!/usr/bin/env bash
# Wait until GET /healthz reports every component healthy.
set -euo pipefail
GATEWAY="${GATEWAY:-http://localhost:8000}"
for _ in $(seq 1 60); do
  if body=$(curl -fs "$GATEWAY/healthz" 2>/dev/null); then
    echo "✓ stack healthy at $GATEWAY"
    echo "$body" | python3 -c "$(cat <<'PY'
import json, sys
d = json.load(sys.stdin)
parts = [f"{name}: {c['status']}" for name, c in d["components"].items()]
print("  " + ", ".join(parts), "| workers consuming:", d["components"]["broker"].get("consumers"))
PY
)"
    exit 0
  fi
  sleep 2
done
echo "✗ stack did not become healthy" >&2
exit 1
