"""Quick snapshot: heartbeat age + last N events for one job."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone

from sqlalchemy import select
from app.database import SessionLocal
from app.models import AgentRunEvent, WorkflowJob

JOB_ID = os.environ["JOB_ID"]
N = int(os.environ.get("N", "10"))


def age(dt):
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    return f"{s//60}m{s%60}s" if s >= 60 else f"{s}s"


with SessionLocal() as db:
    job = db.get(WorkflowJob, JOB_ID)
    run = job.agent_run
    print(f"status={job.status.value} heartbeat_ago={age(run.heartbeat_at)} "
          f"started={age(job.started_at)} attempts={run.attempt_count}")
    evts = db.scalars(
        select(AgentRunEvent).where(AgentRunEvent.run_id == run.id)
        .order_by(AgentRunEvent.sequence.desc()).limit(N)).all()
    for ev in reversed(evts):
        d = ev.data or {}
        brief = {k: d[k] for k in ("tool", "title", "detail", "turn", "operation", "status", "intent") if k in d}
        print(f"#{ev.sequence} [{ev.created_at:%H:%M:%S}] {ev.event_type}/{ev.status} "
              f"{json.dumps(brief, ensure_ascii=False)[:200]}")
    print("ARTIFACTS:", [(a.filename, getattr(a, 'size', None)) for a in job.artifacts])
