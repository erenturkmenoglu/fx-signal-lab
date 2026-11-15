#!/usr/bin/env bash
# Wait until the application reports ready, or fail with the container state.
set -euo pipefail
for _ in $(seq 1 90); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/api/health/ready >/dev/null 2>&1; then
    echo "ready"
    exit 0
  fi
  sleep 2
done
docker compose ps
exit 1
