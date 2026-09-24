"""Dump full data of one event sequence for a job."""
from __future__ import annotations
import json
import os

from sqlalchemy import select
from app.database import SessionLocal
from app.models import AgentRunEvent, WorkflowJob

JOB_ID = os.environ["JOB_ID"]
SEQ = int(os.environ["SEQ"])

with SessionLocal() as db:
    job = db.get(WorkflowJob, JOB_ID)
    ev = db.scalars(select(AgentRunEvent).where(
        AgentRunEvent.run_id == job.agent_run.id, AgentRunEvent.sequence == SEQ)).one()
    print(json.dumps(ev.data, ensure_ascii=False, indent=2))
