from __future__ import annotations

import json
import os
import sys
import time


sys.path.insert(0, "/app")

from app.database import SessionLocal
from app.models import JobStatus, WorkflowJob


job_id = os.environ["SKILLGO_JOB_ID"]
deadline = time.monotonic() + int(os.getenv("SKILLGO_MONITOR_SECONDS", "1800"))
terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.BLOCKED}
last_state: tuple | None = None

while time.monotonic() < deadline:
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        if job is None:
            raise SystemExit("job not found")
        running_step = next(
            (step for step in job.steps if step.status.value == "running"),
            None,
        )
        state = (
            job.status.value,
            running_step.step_key if running_step else None,
            running_step.detail if running_step else None,
            len(job.artifacts),
        )
        if state != last_state:
            print(
                json.dumps(
                    {
                        "status": job.status.value,
                        "step": running_step.step_key if running_step else None,
                        "detail": running_step.detail if running_step else None,
                        "artifacts": len(job.artifacts),
                        "error_code": job.error_code,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            last_state = state
        if job.status in terminal:
            raise SystemExit(0 if job.status == JobStatus.SUCCEEDED else 1)
    time.sleep(5)

raise SystemExit("monitor timeout")
