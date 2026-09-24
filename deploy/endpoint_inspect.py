"""Inspect external API endpoints + recent endpoint-triggered jobs."""
from __future__ import annotations
import json

from sqlalchemy import select
from app.database import SessionLocal
from app.models import Endpoint, WorkflowJob

with SessionLocal() as db:
    eps = db.scalars(select(Endpoint).order_by(Endpoint.created_at)).all()
    print(f"endpoints: {len(eps)}")
    for e in eps:
        print(json.dumps({
            "slug": e.slug, "name": e.name, "active": e.is_active,
            "mode": e.invocation_mode if hasattr(e, "invocation_mode") else None,
            "key_prefix": e.api_key_prefix,
            "created": e.created_at.isoformat(),
        }, ensure_ascii=False, default=str))
    jobs = db.scalars(
        select(WorkflowJob).where(WorkflowJob.trigger == "api")
        .order_by(WorkflowJob.created_at.desc()).limit(10)).all()
    print(f"api-triggered jobs (last 10): {len(jobs)}")
    for j in jobs:
        print(json.dumps({"id": j.id[:8], "status": j.status.value,
                          "skill": j.skill.name if j.skill else None,
                          "created": j.created_at.isoformat()}, ensure_ascii=False, default=str))
