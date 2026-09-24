"""Dump full error payloads + last events with complete data for a failed job."""
from __future__ import annotations

import json
import os

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AgentRunEvent, WorkflowJob

JOB_ID = os.environ["JOB_ID"]

with SessionLocal() as db:
    job = db.get(WorkflowJob, JOB_ID)
    print("JOB error_code:", job.error_code)
    print("JOB error_message:", job.error_message)
    run = job.agent_run
    print("RUN error_code:", run.error_code)
    print("RUN error_message:", run.error_message)
    print("lease_owner:", run.lease_owner, "attempts:", run.attempt_count)
    print("summary:", json.dumps(run.summary, ensure_ascii=False, indent=2)[:2000])
    evts = db.scalars(
        select(AgentRunEvent)
        .where(AgentRunEvent.run_id == run.id, AgentRunEvent.sequence >= 80)
        .order_by(AgentRunEvent.sequence)
    ).all()
    for ev in evts:
        print(f"----- #{ev.sequence} {ev.event_type}/{ev.status} @ {ev.created_at:%H:%M:%S}")
        print(json.dumps(ev.data, ensure_ascii=False, indent=2)[:3000])
