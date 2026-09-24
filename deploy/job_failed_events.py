"""All non-succeeded tool results + any payload mentioning timeout for a job."""
from __future__ import annotations
import json
import os

from sqlalchemy import select
from app.database import SessionLocal
from app.models import AgentRunEvent, WorkflowJob

JOB_ID = os.environ["JOB_ID"]

with SessionLocal() as db:
    job = db.get(WorkflowJob, JOB_ID)
    evts = db.scalars(
        select(AgentRunEvent).where(AgentRunEvent.run_id == job.agent_run.id)
        .order_by(AgentRunEvent.sequence)).all()
    for ev in evts:
        d = ev.data or {}
        blob = json.dumps(d, ensure_ascii=False)
        if ev.status not in ("succeeded", "running") or "TIMEOUT" in blob.upper() or "timed_out" in blob:
            keep = {k: v for k, v in d.items() if k in (
                "tool", "turn", "operation", "error_code", "message", "detail",
                "exit_code", "timed_out", "duration_ms", "hint", "title")}
            print(f"#{ev.sequence} [{ev.created_at:%H:%M:%S}] {ev.event_type}/{ev.status} "
                  f"{json.dumps(keep, ensure_ascii=False)[:500]}")
