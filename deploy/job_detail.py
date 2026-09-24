"""One-shot detailed dump for one job: status, plan, input files, all tool-call events."""
from __future__ import annotations

import json
import os

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AgentRunEvent, WorkflowJob

JOB_ID = os.environ["JOB_ID"]

with SessionLocal() as db:
    job = db.get(WorkflowJob, JOB_ID)
    print("STATUS:", job.status.value, "error:", job.error_code, "network:", job.network_enabled)
    print("INPUTS:")
    for f in job.input_files:
        print("  -", f.filename, getattr(f, "size", None), getattr(f, "kind", None))
    mem = job.memory.data if job.memory else {}
    plan = mem.get("plan")
    if plan:
        steps = plan.get("steps") or []
        print(f"PLAN ({len(steps)} steps):")
        for s in steps:
            print("  [{}] {}".format(s.get("status", "?"), (s.get("title") or s.get("description") or "")[:100]))
    else:
        print("PLAN: none yet")
    run = job.agent_run
    evts = db.scalars(
        select(AgentRunEvent).where(AgentRunEvent.run_id == run.id).order_by(AgentRunEvent.sequence)
    ).all()
    print(f"TOTAL EVENTS: {len(evts)}")
    for ev in evts:
        d = ev.data or {}
        et = ev.event_type
        if "tool" in et or "inspect" in json.dumps(d, ensure_ascii=False)[:0]:
            pass
        # print tool calls, plan events, errors and anything mentioning inspect/document/mineru
        blob = json.dumps(d, ensure_ascii=False)
        interesting = (
            "tool" in et
            or et in {"agent.plan.updated", "job.question"}
            or ev.status == "failed"
            or "inspect_document" in blob
            or "mineru" in blob.lower()
        )
        if not interesting:
            continue
        keep = {k: v for k, v in d.items() if k in {
            "tool", "name", "intent", "cached", "status", "code", "error_code",
            "message", "step_key", "title", "path", "kind", "reason", "hint",
            "args", "duration_ms",
        }}
        print(f"#{ev.sequence} [{ev.created_at:%H:%M:%S}] {et}/{ev.status} "
              f"{json.dumps(keep, ensure_ascii=False)[:400]}")
