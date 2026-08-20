from __future__ import annotations

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import SessionLocal
from .execution_runtime import (
    append_run_event,
    complete_run,
    ensure_job_run,
    fail_run,
)
from .model_gateway import ModelGatewayError, OpenAICompatibleGateway
from .models import Artifact, JobEvent, JobStatus, JobStep, JobStepStatus, RunStatus, User, WorkflowJob, utcnow
from .services import add_audit
from .storage import storage
from .workspace_service import file_sha256


logger = logging.getLogger(__name__)


def add_job_event(
    db: Session,
    job: WorkflowJob,
    event_type: str,
    title: str,
    detail: str = "",
    *,
    status: str = "running",
    data: dict | None = None,
) -> JobEvent:
    """Append a safe operational event to a job's user-visible timeline."""

    sequence = int(
        db.scalar(select(func.coalesce(func.max(JobEvent.sequence), 0)).where(JobEvent.job_id == job.id))
        or 0
    ) + 1
    event = JobEvent(
        job_id=job.id,
        sequence=sequence,
        event_type=event_type[:40],
        status=status[:30],
        title=title[:180],
        detail=detail[:4000],
        data=data or {},
    )
    db.add(event)
    db.flush()
    run = ensure_job_run(db, job)
    append_run_event(
        db,
        run,
        f"job.{event.event_type}",
        status=event.status,
        data={
            "job_event_id": event.id,
            "title": event.title,
            "detail": event.detail,
            **(event.data or {}),
        },
    )
    return event


def set_step(
    db: Session,
    job: WorkflowJob,
    step_key: str,
    status: JobStepStatus,
    detail: str = "",
) -> JobStep:
    step = next(item for item in job.steps if item.step_key == step_key)
    step.status = status
    step.detail = detail
    if status == JobStepStatus.RUNNING and step.started_at is None:
        step.started_at = utcnow()
    if status in {
        JobStepStatus.SUCCEEDED,
        JobStepStatus.FAILED,
        JobStepStatus.BLOCKED,
        JobStepStatus.SKIPPED,
    }:
        step.started_at = step.started_at or utcnow()
        step.finished_at = utcnow()
    db.flush()
    return step


def _artifact_text(output: dict) -> str:
    for key in ("message", "report", "result", "summary", "content"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(output, ensure_ascii=False, indent=2)


async def execute_instruction_job(
    db: Session,
    *,
    job: WorkflowJob,
    actor: User,
    gateway: OpenAICompatibleGateway,
) -> WorkflowJob:
    run = ensure_job_run(db, job)
    run.status = RunStatus.RUNNING
    run.attempt_count += 1
    run.started_at = run.started_at or utcnow()
    run.finished_at = None
    append_run_event(
        db,
        run,
        "attempt.started",
        status="running",
        data={"attempt": run.attempt_count, "executor": "instruction"},
    )
    job.status = JobStatus.RUNNING
    job.started_at = utcnow()
    set_step(db, job, "execute-workflow", JobStepStatus.RUNNING, "正在调用私有模型执行完整指令")
    add_job_event(db, job, "reasoning", "正在理解任务", "Skill 正在结合指令和已上传文件制定回答", status="running")
    db.commit()

    workspace_files = [
        {"filename": item.filename, "content": item.extracted_text or ""}
        for item in job.input_files
        if item.extracted_text
    ]
    instruction = job.instruction.strip() or (
        "请根据已上传文件完整执行 SKILL.md 中适用于纯指令环境的全部步骤。"
        "不要等待用户发送“继续”，一次返回最终结果；无法完成的步骤必须明确说明，禁止声称执行了未实际执行的工具或脚本。"
    )

    try:
        result = await gateway.execute(
            skill_md=job.skill_version.skill_md,
            input_schema=job.skill_version.input_schema,
            output_schema=job.skill_version.output_schema,
            input_data={"message": instruction},
            history=[],
            chat_mode=True,
            workspace_files=workspace_files,
        )
        set_step(db, job, "execute-workflow", JobStepStatus.SUCCEEDED, f"模型 {result.model_name} 已返回结果")
        add_job_event(db, job, "reasoning", "已完成分析", f"模型 {result.model_name} 已返回结果", status="succeeded")

        job.status = JobStatus.PRODUCING_ARTIFACTS
        set_step(db, job, "collect-artifacts", JobStepStatus.RUNNING, "正在收集平台可信执行器产物")
        content = _artifact_text(result.output)
        data = content.encode("utf-8")
        if not data:
            raise ModelGatewayError("EMPTY_WORKFLOW_OUTPUT", "Workflow returned no artifact content")
        artifact = Artifact(
            job_id=job.id,
            user_id=actor.id,
            filename=f"{job.skill.slug}-result-{job.id[:8]}.txt",
            content_type="text/plain; charset=utf-8",
            size_bytes=len(data),
            sha256=file_sha256(data),
            storage_path="pending",
            kind="result",
            verified=False,
        )
        db.add(artifact)
        db.flush()
        key = f"job-artifacts/{actor.id}/{job.id}/{artifact.id}/{artifact.filename}"
        artifact.storage_path = storage.put(key, data)
        set_step(db, job, "collect-artifacts", JobStepStatus.SUCCEEDED, f"已生成 {artifact.filename}")
        add_job_event(
            db,
            job,
            "artifact",
            f"已生成 {artifact.filename}",
            f"{len(data)} 字节 · 正在校验完整性",
            status="succeeded",
            data={"artifact_id": artifact.id, "filename": artifact.filename},
        )

        job.status = JobStatus.VERIFYING
        set_step(db, job, "verify-artifacts", JobStepStatus.RUNNING, "正在校验产物大小与哈希")
        stored = storage.read(artifact.storage_path)
        if not stored or file_sha256(stored) != artifact.sha256:
            raise ModelGatewayError("ARTIFACT_VERIFICATION_FAILED", "Generated artifact failed verification")
        artifact.verified = True
        set_step(db, job, "verify-artifacts", JobStepStatus.SUCCEEDED, "产物已通过完整性校验")
        job.status = JobStatus.SUCCEEDED
        job.error_code = None
        job.error_message = None
        job.finished_at = utcnow()
        add_job_event(
            db,
            job,
            "result",
            "任务已完成",
            content[:4000],
            status="succeeded",
            data={"artifact_id": artifact.id, "model_name": result.model_name},
        )
        add_audit(
            db,
            actor=actor,
            action="workflow_job.succeeded",
            resource_type="workflow_job",
            resource_id=job.id,
            details={"artifact_id": artifact.id, "model_name": result.model_name},
        )
        complete_run(
            db,
            run,
            summary={
                "model_name": result.model_name,
                "artifact_count": 1,
                "token_usage": result.token_usage,
            },
        )
    except ModelGatewayError as exc:
        running = next((item for item in job.steps if item.status == JobStepStatus.RUNNING), None)
        if running:
            set_step(db, job, running.step_key, JobStepStatus.FAILED, str(exc)[:1000])
        for step in job.steps:
            if step.status == JobStepStatus.PENDING:
                set_step(db, job, step.step_key, JobStepStatus.SKIPPED, "上一步失败，未执行")
        job.status = JobStatus.FAILED
        job.error_code = exc.code
        job.error_message = str(exc)[:4000]
        job.finished_at = utcnow()
        add_job_event(db, job, "error", "任务执行失败", job.error_message, status="failed", data={"error_code": exc.code})
        add_audit(
            db,
            actor=actor,
            action="workflow_job.failed",
            resource_type="workflow_job",
            resource_id=job.id,
            details={"error_code": exc.code},
        )
        fail_run(db, run, error_code=exc.code, error_message=str(exc))
    except Exception:
        logger.exception("Unexpected workflow job failure", extra={"job_id": job.id})
        running = next((item for item in job.steps if item.status == JobStepStatus.RUNNING), None)
        if running:
            set_step(db, job, running.step_key, JobStepStatus.FAILED, "平台执行器发生内部错误")
        job.status = JobStatus.FAILED
        job.error_code = "WORKFLOW_INTERNAL_ERROR"
        job.error_message = "工作流执行失败，请查看平台日志"
        job.finished_at = utcnow()
        add_job_event(db, job, "error", "任务执行失败", job.error_message, status="failed", data={"error_code": job.error_code})
        fail_run(
            db,
            run,
            error_code=job.error_code,
            error_message=job.error_message,
        )
    db.commit()
    db.refresh(job)
    return job


async def execute_instruction_job_background(
    job_id: str,
    user_id: str,
    gateway: OpenAICompatibleGateway,
) -> None:
    """Run a PoC instruction job after the HTTP response has been returned."""
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        actor = db.get(User, user_id)
        if job is None or actor is None or job.status == JobStatus.CANCELLED:
            return
        await execute_instruction_job(db, job=job, actor=actor, gateway=gateway)
