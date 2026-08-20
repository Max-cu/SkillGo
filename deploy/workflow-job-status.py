from __future__ import annotations

import json
import os
import sys


sys.path.insert(0, "/app")

from app.database import SessionLocal
from app.models import WorkflowJob


with SessionLocal() as db:
    job = db.get(WorkflowJob, os.environ["SKILLGO_JOB_ID"])
    if job is None:
        raise SystemExit("job not found")
    reasoning_events = [event for event in job.events if event.event_type == "reasoning"]
    tool_events = [event for event in job.events if event.event_type == "tool"]
    result_event = next(
        (event for event in reversed(job.events) if event.event_type == "result"),
        None,
    )
    print(
        json.dumps(
            {
                "id": job.id,
                "status": job.status.value,
                "error_code": job.error_code,
                "error_message": job.error_message,
                "statistics": {
                    "elapsed_seconds": (
                        round((job.finished_at - job.started_at).total_seconds(), 3)
                        if job.started_at and job.finished_at
                        else None
                    ),
                    "reasoning_turns": len(reasoning_events),
                    "tool_operations": len(tool_events),
                    "failed_tool_operations": sum(
                        event.status == "failed" for event in tool_events
                    ),
                    "tool_sequence": [
                        str((event.data or {}).get("tool") or event.title)
                        for event in tool_events
                    ],
                    "reported": result_event.data if result_event else {},
                },
                "steps": [
                    {
                        "key": step.step_key,
                        "status": step.status.value,
                        "detail": step.detail,
                    }
                    for step in job.steps
                ],
                "artifacts": [
                    {
                        "filename": artifact.filename,
                        "size_bytes": artifact.size_bytes,
                        "verified": artifact.verified,
                    }
                    for artifact in job.artifacts
                ],
            },
            ensure_ascii=False,
        )
    )
