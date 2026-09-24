"""Ad-hoc live monitor for active SkillGo agent runs (run via stdin in api container).

Usage (on host):
  docker compose exec -T api python - < monitor_running.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AgentRun, AgentRunEvent, WorkflowJob, RunStatus

ACTIVE = {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_USER}


def _age(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60}s"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


with SessionLocal() as db:
    runs = db.scalars(
        select(AgentRun).where(AgentRun.status.in_(ACTIVE)).order_by(AgentRun.created_at)
    ).all()
    print(f"active runs: {len(runs)}")
    for run in runs:
        job = run.workflow_job
        skill_name = job.skill.name if job is not None and job.skill else None
        print("=" * 70)
        print(json.dumps({
            "run_id": run.id,
            "job_id": job.id if job else None,
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            "run_type": run.run_type,
            "skill": skill_name,
            "attempt": run.attempt_count,
            "age": _age(run.started_at or run.created_at),
            "heartbeat_ago": _age(run.heartbeat_at),
            "lease_owner": run.lease_owner,
            "error_code": run.error_code,
        }, ensure_ascii=False))
        if job is not None:
            print("instruction:", (job.instruction or "")[:300].replace("\n", " "))
        evts = db.scalars(
            select(AgentRunEvent)
            .where(AgentRunEvent.run_id == run.id)
            .order_by(AgentRunEvent.sequence.desc())
            .limit(12)
        ).all()
        print(f"-- last {len(evts)} events (newest last) --")
        for ev in reversed(evts):
            data = ev.data or {}
            brief = {}
            for k in ("tool", "name", "step_key", "status", "phase", "intent", "kind", "code", "message"):
                if k in data:
                    brief[k] = data[k]
            ts = ev.created_at.strftime("%H:%M:%S")
            print(f"  #{ev.sequence} {ts} {ev.event_type}/{ev.status} {json.dumps(brief, ensure_ascii=False)[:220]}")
