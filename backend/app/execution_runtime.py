from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .models import (
    AgentRun,
    AgentRunEvent,
    JobEvent,
    RunStatus,
    WorkflowJob,
    utcnow,
)


logger = logging.getLogger(__name__)


def ensure_job_run(db: Session, job: WorkflowJob) -> AgentRun:
    """Return the durable logical run for a workflow job, creating it lazily."""

    if job.agent_run is not None:
        return job.agent_run
    run = AgentRun(
        user_id=job.user_id,
        run_type="skill_job",
        workflow_job_id=job.id,
        status=RunStatus.QUEUED,
        summary={
            "execution_mode": job.execution_mode,
            "trigger": job.trigger,
        },
    )
    job.agent_run = run
    db.add(run)
    db.flush()
    return run


def create_conversation_run(
    db: Session,
    *,
    user_id: str,
    conversation_id: str,
    model_name: str,
) -> AgentRun:
    now = utcnow()
    run = AgentRun(
        user_id=user_id,
        run_type="conversation_turn",
        conversation_id=conversation_id,
        status=RunStatus.RUNNING,
        attempt_count=1,
        started_at=now,
        heartbeat_at=now,
        summary={"model_name": model_name},
    )
    db.add(run)
    db.flush()
    append_run_event(
        db,
        run,
        "turn.started",
        status="running",
        data={"model_name": model_name},
    )
    return run


def append_run_event(
    db: Session,
    run: AgentRun,
    event_type: str,
    *,
    status: str = "running",
    data: dict | None = None,
) -> AgentRunEvent:
    sequence = int(
        db.scalar(
            select(func.coalesce(func.max(AgentRunEvent.sequence), 0)).where(
                AgentRunEvent.run_id == run.id
            )
        )
        or 0
    ) + 1
    event = AgentRunEvent(
        run_id=run.id,
        sequence=sequence,
        event_type=event_type[:48],
        status=status[:30],
        data=data or {},
    )
    db.add(event)
    db.flush()
    return event


def complete_run(
    db: Session,
    run: AgentRun,
    *,
    summary: dict | None = None,
    response_message_id: str | None = None,
) -> None:
    if run.status == RunStatus.SUCCEEDED and run.finished_at is not None:
        return
    run.status = RunStatus.SUCCEEDED
    run.finished_at = utcnow()
    run.lease_owner = None
    run.lease_token = None
    run.lease_expires_at = None
    run.error_code = None
    run.error_message = None
    run.response_message_id = response_message_id or run.response_message_id
    if summary:
        run.summary = {**(run.summary or {}), **summary}
    append_run_event(db, run, "run.completed", status="succeeded", data=summary or {})


def fail_run(
    db: Session,
    run: AgentRun,
    *,
    error_code: str,
    error_message: str,
    cancelled: bool = False,
) -> None:
    target_status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED
    if run.status == target_status and run.finished_at is not None:
        return
    run.status = target_status
    run.finished_at = utcnow()
    run.lease_owner = None
    run.lease_token = None
    run.lease_expires_at = None
    run.error_code = error_code[:80]
    run.error_message = error_message[:4000]
    append_run_event(
        db,
        run,
        "run.cancelled" if cancelled else "run.failed",
        status="cancelled" if cancelled else "failed",
        data={"error_code": run.error_code},
    )


def cleanup_execution_history(
    *,
    now: datetime | None = None,
    success_detail_days: int | None = None,
    failure_detail_days: int | None = None,
) -> dict[str, int]:
    """Prune reconstructable detail while retaining each run and final job summary.

    Job events are the user-facing projection.  Old reasoning/tool detail is
    removed, while input, status, artifact, result and error summaries remain.
    AgentRun rows, user messages and generated artifacts are never deleted here.
    """

    current = now or utcnow()
    success_cutoff = current - timedelta(
        days=settings.agent_run_success_detail_days
        if success_detail_days is None
        else success_detail_days
    )
    failure_cutoff = current - timedelta(
        days=settings.agent_run_failure_detail_days
        if failure_detail_days is None
        else failure_detail_days
    )
    terminal_failure = (RunStatus.FAILED, RunStatus.CANCELLED)
    retained_job_event_types = ("input", "status", "artifact", "result", "error")

    with SessionLocal() as db:
        expired_success_ids = select(AgentRun.id).where(
            AgentRun.status == RunStatus.SUCCEEDED,
            AgentRun.finished_at.is_not(None),
            AgentRun.finished_at <= success_cutoff,
        )
        expired_failure_ids = select(AgentRun.id).where(
            AgentRun.status.in_(terminal_failure),
            AgentRun.finished_at.is_not(None),
            AgentRun.finished_at <= failure_cutoff,
        )
        run_events_deleted = int(
            db.execute(
                delete(AgentRunEvent).where(
                    or_(
                        AgentRunEvent.run_id.in_(expired_success_ids),
                        AgentRunEvent.run_id.in_(expired_failure_ids),
                    )
                )
            ).rowcount
            or 0
        )

        expired_success_jobs = select(AgentRun.workflow_job_id).where(
            AgentRun.id.in_(expired_success_ids), AgentRun.workflow_job_id.is_not(None)
        )
        expired_failure_jobs = select(AgentRun.workflow_job_id).where(
            AgentRun.id.in_(expired_failure_ids), AgentRun.workflow_job_id.is_not(None)
        )
        job_events_deleted = int(
            db.execute(
                delete(JobEvent).where(
                    or_(
                        JobEvent.job_id.in_(expired_success_jobs),
                        JobEvent.job_id.in_(expired_failure_jobs),
                    ),
                    JobEvent.event_type.not_in(retained_job_event_types),
                )
            ).rowcount
            or 0
        )
        db.commit()

    result = {
        "run_events_deleted": run_events_deleted,
        "job_events_deleted": job_events_deleted,
    }
    if run_events_deleted or job_events_deleted:
        logger.info("Pruned expired Agent run detail", extra=result)
    return result


def fail_stale_conversation_runs(*, now: datetime | None = None) -> int:
    """Close chat turns left running by a terminated API process.

    Chat responses cannot be resumed after the client connection disappears,
    but their durable run must not remain "running" forever.
    """

    current = now or utcnow()
    cutoff = current - timedelta(seconds=max(60, int(settings.model_timeout_seconds) + 60))
    with SessionLocal() as db:
        runs = db.scalars(
            select(AgentRun).where(
                AgentRun.run_type == "conversation_turn",
                AgentRun.status == RunStatus.RUNNING,
                AgentRun.started_at.is_not(None),
                AgentRun.started_at <= cutoff,
            )
        ).all()
        for run in runs:
            fail_run(
                db,
                run,
                error_code="API_PROCESS_INTERRUPTED",
                error_message="API process ended before the conversation turn completed",
            )
        db.commit()
    return len(runs)
