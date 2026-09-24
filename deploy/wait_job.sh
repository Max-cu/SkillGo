#!/bin/bash
# Poll one SkillGo job until it reaches a terminal status. Env: JOB_ID, INTERVAL=60
set -u
JOB="${JOB_ID:?JOB_ID required}"
INTERVAL="${INTERVAL:-60}"
cd /opt/skillgo
while true; do
  ST=$(JOB_ID="$JOB" docker compose exec -T -e JOB_ID api python -c \
    "import os; from app.database import SessionLocal; from app.models import WorkflowJob; db=SessionLocal(); j=db.get(WorkflowJob, os.environ['JOB_ID']); print(j.status.value if j else 'missing')" 2>/dev/null | tail -1)
  echo "$(date -u '+%Y-%m-%d %H:%M:%S') $JOB status=${ST:-unknown}"
  case "$ST" in
    succeeded|failed|cancelled|blocked|missing)
      echo "TERMINAL:$ST"
      exit 0 ;;
  esac
  sleep "$INTERVAL"
done
