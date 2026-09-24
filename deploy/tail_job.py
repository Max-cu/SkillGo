"""Tail agent_run_events (+ job status) for one SkillGo job, polling until terminal.

Run inside api container:
  JOB_ID=... python tail_job.py [interval] [max_minutes]
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AgentRunEvent, WorkflowJob, JobStatus

JOB_ID = os.environ["JOB_ID"]
INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 else 15
MAX_MIN = int(sys.argv[2]) if len(sys.argv) > 2 else 240
TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.BLOCKED}

INTERESTING = {
    "tool", "name", "intent", "cached", "status", "phase", "code", "error_code",
    "message", "step_key", "title", "tool_name", "duration_ms", "bytes", "path",
    "model", "turn", "attempt", "kind", "reason", "hint",
}


def age(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    return f"{s//60}m{s%60}s" if s >= 60 else f"{s}s"


def brief(data: dict) -> dict:
    out = {k: data[k] for k in INTERESTING if k in data}
    if "plan" in data and isinstance(data["plan"], dict):
        steps = data["plan"].get("steps") or []
        done = sum(1 for st in steps if st.get("status") == "completed")
        out["plan"] = f"{done}/{len(steps)} steps"
    return out


last_seq = 0
deadline = time.monotonic() + MAX_MIN * 60
last_status = None

with SessionLocal() as db:
    job = db.get(WorkflowJob, JOB_ID)
    if job is None:
        raise SystemExit("job not found")
    print(f"tail start job={JOB_ID} skill={job.skill.name if job.skill else '?'} "
          f"status={job.status.value} age={age(job.started_at)}", flush=True)

while time.monotonic() < deadline:
    with SessionLocal() as db:
        job = db.get(WorkflowJob, JOB_ID)
        run = job.agent_run
        st = job.status.value
        if st != last_status:
            print(f"[{datetime.now():%H:%M:%S}] *** JOB STATUS: {st} "
                  f"(age {age(job.started_at)}, heartbeat {age(run.heartbeat_at) if run else '-'}) ***",
                  flush=True)
            last_status = st
        if run is not None:
            evts = db.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run.id, AgentRunEvent.sequence > last_seq)
                .order_by(AgentRunEvent.sequence)
            ).all()
            for ev in evts:
                last_seq = ev.sequence
                d = ev.data or {}
                print(f"[{ev.created_at:%H:%M:%S}] #{ev.sequence} {ev.event_type}/{ev.status} "
                      f"{json.dumps(brief(d), ensure_ascii=False)[:300]}", flush=True)
        artifact_count = len(job.artifacts)
        if job.status in TERMINAL:
            print(f"=== TERMINAL: {job.status.value} error_code={job.error_code} "
                  f"error={(job.error_message or '')[:300]} artifacts={artifact_count} ===",
                  flush=True)
            sys.exit(0 if job.status == JobStatus.SUCCEEDED else 1)
    time.sleep(INTERVAL)

print("tail monitor timed out", flush=True)
