"""Dump full data for event sequences given in SEQ env (comma separated)."""
from __future__ import annotations
import json
import os

from sqlalchemy import select
from app.database import SessionLocal
from app.models import AgentRunEvent, WorkflowJob

JOB_ID = os.environ["JOB_ID"]
SEQS = [int(x) for x in os.environ["SEQ"].split(",")]

with SessionLocal() as db:
    job = db.get(WorkflowJob, JOB_ID)
    for seq in SEQS:
        ev = db.scalars(select(AgentRunEvent).where(
            AgentRunEvent.run_id == job.agent_run.id, AgentRunEvent.sequence == seq)).one()
        print(f"----- #{seq} {ev.event_type}/{ev.status} @ {ev.created_at:%H:%M:%S}")
        print(json.dumps(ev.data, ensure_ascii=False, indent=2)[:2500])
