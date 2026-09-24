#!/bin/bash
# Grep all worker logs for a job/run id and errors. Usage: bash grep_workers.sh <id-substring>
set -u
cd /opt/skillgo
NEEDLE="${1:-db628789}"
for w in worker-1 worker-2 worker-3 worker-4 worker-5; do
  OUT=$(docker compose logs --since 60m "$w" 2>&1 | grep -E "$NEEDLE|SANDBOX_INTERNAL|Traceback|ERROR|Exception" | tail -40)
  if [ -n "$OUT" ]; then
    echo "===== $w ====="
    echo "$OUT"
  fi
done
