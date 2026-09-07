from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import os
import signal
import socket
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .artifact_validation import (
    normalize_artifact_paths as _normalize_artifact_paths,
    validate_artifact_content as _validate_artifact_content,
)
from .config import settings
from .database import SessionLocal, initialize_schema
from .deterministic_runtime import execute_fixed_skill
from .workflow_tools import effective_instruction
from .execution_runtime import (
    append_run_event,
    complete_run,
    ensure_job_run,
    fail_run,
)
from .model_gateway import ModelGatewayError, OpenAICompatibleGateway, get_model_gateway
from .models import AgentRun, Artifact, JobStatus, JobStepStatus, RunStatus, User, WorkflowJob, WorkflowJobMemory, utcnow
from .runtime_profile import version_runtime_profile
from .sandbox_agent_loop import (
    AgentNeedsInput,
    AgentJobCancelled as JobCancelled,
    _action_skill_context,
    _agent_messages,
    _append_tool_result,
    _finish_tool_event,
    _run_agent_loop,
    _safe_tool_event,
    _trim_messages,
)
from .sandbox_runtime import (
    DockerSandbox,
    SandboxRuntimeError,
    cleanup_execution_sandbox,
    cleanup_stale_sandboxes,
    docker_client,
    package_skill_root,
)
from .sandbox_tool_registry import (
    validate_agent_action as _validate_agent_action,
)
from .services import add_audit
from .skill_execution_spec import fixed_execution_spec
from .storage import storage
from .workflow_execution import add_job_event, set_step
from .workspace_service import file_sha256


logger = logging.getLogger(__name__)
TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.BLOCKED}


class JobLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class JobLease:
    job_id: str
    run_id: str
    token: str
    attempt: int
    owner: str

    @property
    def execution_id(self) -> str:
        return f"{self.job_id}-a{self.attempt}"


@dataclass(frozen=True)
class ReclaimedSandbox:
    job_id: str
    execution_id: str


WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


def _lease_is_after(value, reference) -> bool:
    if value is None:
        return False
    if value.tzinfo is None and reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)
    return value > reference


def _claim_job(worker_id: str = WORKER_ID) -> JobLease | None:
    with SessionLocal() as db:
        statement = (
            select(WorkflowJob)
            .where(
                WorkflowJob.status == JobStatus.QUEUED,
                WorkflowJob.execution_mode == "sandbox_required",
            )
            .order_by(WorkflowJob.created_at)
            .with_for_update(skip_locked=True)
        )
        job = db.scalars(statement).first()
        if job is None:
            return None
        run = ensure_job_run(db, job)
        answered_attempts = (job.memory.data or {}).get('resumed_attempts', 0) if job.memory else 0
        if run.attempt_count - answered_attempts >= settings.sandbox_worker_max_attempts:
            job.status = JobStatus.FAILED
            job.error_code = "SANDBOX_WORKER_RETRY_EXHAUSTED"
            job.error_message = (
                f"任务已连续中断 {run.attempt_count} 次，已停止自动重试"
            )
            job.finished_at = utcnow()
            for step in job.steps:
                if step.status == JobStepStatus.RUNNING:
                    set_step(db, job, step.step_key, JobStepStatus.FAILED, job.error_message)
                elif step.status == JobStepStatus.PENDING:
                    set_step(db, job, step.step_key, JobStepStatus.SKIPPED, "自动重试次数已耗尽")
            add_job_event(
                db,
                job,
                "error",
                "任务自动恢复失败",
                job.error_message,
                status="failed",
                data={"error_code": job.error_code, "attempts": run.attempt_count},
            )
            fail_run(
                db,
                run,
                error_code=job.error_code,
                error_message=job.error_message,
            )
            db.commit()
            return None

        now = utcnow()
        token = uuid4().hex
        run.status = RunStatus.RUNNING
        run.attempt_count += 1
        run.lease_owner = worker_id
        run.lease_token = token
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(
            seconds=settings.sandbox_worker_lease_seconds
        )
        run.started_at = run.started_at or now
        run.finished_at = None
        run.error_code = None
        run.error_message = None
        append_run_event(
            db,
            run,
            "attempt.started",
            status="running",
            data={
                "attempt": run.attempt_count,
                "worker": worker_id,
                "restart_policy": "fresh_attempt",
                "workspace_restored": False,
            },
        )
        job.status = JobStatus.RUNNING
        job.started_at = job.started_at or now
        job.finished_at = None
        job.error_code = None
        job.error_message = None
        set_step(db, job, "execute-workflow", JobStepStatus.RUNNING, "正在创建独立 Linux 沙箱")
        add_job_event(
            db,
            job,
            "status",
            "正在创建独立沙箱",
            "本次任务拥有独立文件系统、进程和工具状态",
            status="running",
        )
        db.commit()
        return JobLease(
            job_id=job.id,
            run_id=run.id,
            token=token,
            attempt=run.attempt_count,
            owner=worker_id,
        )


def _heartbeat_job(lease: JobLease) -> bool:
    now = utcnow()
    with SessionLocal() as db:
        result = db.execute(
            update(AgentRun)
            .where(
                AgentRun.id == lease.run_id,
                AgentRun.status == RunStatus.RUNNING,
                AgentRun.lease_token == lease.token,
                AgentRun.lease_owner == lease.owner,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now
                + timedelta(seconds=settings.sandbox_worker_lease_seconds),
            )
        )
        db.commit()
        return bool(result.rowcount)


def _assert_job_lease(
    db: Session,
    lease: JobLease | None,
    lease_lost: asyncio.Event | None = None,
    *,
    lock: bool = False,
) -> None:
    if lease is None:
        return
    if lease_lost is not None and lease_lost.is_set():
        raise JobLeaseLost("Workflow job lease was lost")
    if lock:
        db.execute(
            select(WorkflowJob.id)
            .where(WorkflowJob.id == lease.job_id)
            .with_for_update()
        )
        run = db.scalar(
            select(AgentRun).where(AgentRun.id == lease.run_id).with_for_update()
        )
    else:
        run = db.get(AgentRun, lease.run_id)
    if run is None:
        raise JobLeaseLost("Workflow job run no longer exists")
    if not lock:
        db.refresh(run)
    if (
        run.status != RunStatus.RUNNING
        or run.lease_token != lease.token
        or run.lease_owner != lease.owner
        or not _lease_is_after(run.lease_expires_at, utcnow())
    ):
        raise JobLeaseLost("Workflow job lease is no longer current")


async def _heartbeat_loop(
    lease: JobLease,
    stopping: asyncio.Event,
    lease_lost: asyncio.Event,
) -> None:
    while not stopping.is_set():
        try:
            await asyncio.wait_for(
                stopping.wait(), timeout=settings.sandbox_worker_heartbeat_seconds
            )
            return
        except TimeoutError:
            pass
        try:
            current = await asyncio.to_thread(_heartbeat_job, lease)
        except Exception:
            logger.exception("Sandbox Worker heartbeat failed", extra={"job_id": lease.job_id})
            current = False
        if not current:
            lease_lost.set()
            return


def _job_is_cancelled(
    db: Session,
    job: WorkflowJob,
    *,
    lease: JobLease | None = None,
    lease_lost: asyncio.Event | None = None,
) -> bool:
    """Assert attempt ownership and project the latest cancellation state."""

    _assert_job_lease(db, lease, lease_lost)
    db.refresh(job, attribute_names=["status"])
    return job.status == JobStatus.CANCELLED


def _persist_artifact(db: Session, job: WorkflowJob, actor: User, path: str, data: bytes) -> Artifact:
    filename = PurePosixPath(path).name
    if not filename or filename in {".", ".."}:
        raise SandboxRuntimeError("SANDBOX_ARTIFACT_INVALID", "Artifact filename is invalid")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    artifact = Artifact(
        job_id=job.id,
        user_id=actor.id,
        filename=filename[:180],
        content_type=content_type,
        size_bytes=len(data),
        sha256=file_sha256(data),
        storage_path="pending",
        kind="result",
        verified=False,
    )
    db.add(artifact)
    db.flush()
    artifact.storage_path = storage.put(
        f"job-artifacts/{actor.id}/{job.id}/{artifact.id}/{artifact.filename}", data
    )
    return artifact


def _required_sandbox_binaries(skill_contexts: list[dict[str, Any]]) -> list[str]:
    """Return normalized third-party command dependencies for one task."""

    required: set[str] = set()
    for context in skill_contexts:
        requirements = context.get("runtime_requirements") or {}
        for raw in requirements.get("required_binaries", requirements.get("binaries")) or []:
            value = PurePosixPath(str(raw).replace("\\", "/")).name.casefold()
            if (
                value
                and len(value) <= 80
                and all(character.isalnum() or character in "._+-" for character in value)
            ):
                required.add(value)
    return sorted(required)[:100]


async def _preflight_sandbox_binaries(
    sandbox: DockerSandbox,
    skill_contexts: list[dict[str, Any]],
) -> list[str]:
    """Fail before model reasoning when declared command dependencies are absent."""

    required = _required_sandbox_binaries(skill_contexts)
    if not required:
        return []
    check = await sandbox.command(
        [
            "python3",
            "-c",
            (
                "import json,shutil,sys; required=json.loads(sys.argv[1]); "
                "missing=[item for item in required if shutil.which(item) is None]; "
                "print(json.dumps({'required':required,'missing':missing},separators=(',',':'))); "
                "raise SystemExit(2 if missing else 0)"
            ),
            json.dumps(required, separators=(",", ":")),
        ],
        cwd="/workspace",
        timeout_seconds=30,
    )
    try:
        payload = json.loads(check.stdout.strip() or "{}")
    except json.JSONDecodeError:
        payload = {}
    missing = [str(item) for item in (payload.get("missing") or []) if str(item)]
    if check.exit_code != 0 or missing:
        names = ", ".join(missing or required)
        raise SandboxRuntimeError(
            "SANDBOX_DEPENDENCY_MISSING",
            f"Skill requires command(s) unavailable in the sandbox runtime: {names}",
        )
    return required


async def execute_sandbox_job(
    job_id: str,
    client: object,
    *,
    lease: JobLease | None = None,
    lease_lost: asyncio.Event | None = None,
) -> None:
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        if job is None:
            return
        run = ensure_job_run(db, job)
        if job.status == JobStatus.CANCELLED:
            fail_run(
                db,
                run,
                error_code="WORKFLOW_CANCELLED",
                error_message="任务已由用户取消",
                cancelled=True,
            )
            db.commit()
            return
        _assert_job_lease(db, lease, lease_lost)
        if lease is None and run.status == RunStatus.QUEUED:
            run.status = RunStatus.RUNNING
            run.attempt_count += 1
            run.started_at = run.started_at or utcnow()
            append_run_event(
                db,
                run,
                "attempt.started",
                status="running",
                data={
                    "attempt": run.attempt_count,
                    "worker": "direct",
                    "restart_policy": "fresh_attempt",
                    "workspace_restored": False,
                },
            )
        actor = db.get(User, job.user_id)
        if actor is None:
            job.status = JobStatus.FAILED
            job.error_code = "WORKFLOW_USER_MISSING"
            job.error_message = "任务所属用户不存在"
            job.finished_at = utcnow()
            fail_run(
                db,
                run,
                error_code=job.error_code,
                error_message=job.error_message,
            )
            db.commit()
            return
        try:
            gateway = get_model_gateway().for_model(job.model_name)
            selected_versions = (
                [binding.skill_version for binding in job.skill_bindings]
                if job.skill_bindings
                else [job.skill_version]
            )
            staged_packages: dict[str, bytes] = {}
            skill_contexts: list[dict[str, Any]] = []
            for index, version in enumerate(selected_versions, 1):
                package = storage.read(version.package_path)
                with zipfile.ZipFile(io.BytesIO(package)) as archive:
                    names = archive.namelist()
                runtime_requirements = (
                    version_runtime_profile(version).get("requirements") or {}
                )
                dependency_files = [
                    name
                    for name in names
                    if PurePosixPath(name).name.casefold()
                    in {
                        "requirements.txt",
                        "pyproject.toml",
                        "package.json",
                        "package-lock.json",
                        "pnpm-lock.yaml",
                        "yarn.lock",
                    }
                ]
                if dependency_files:
                    runtime_requirements = {
                        **runtime_requirements,
                        "network": True,
                        "dependency_download": True,
                        "dependency_files": dependency_files[:50],
                    }
                archive_path = f"skill-packages/{index:02d}.zip"
                extract_root = f"/workspace/skills/{index:02d}-{version.skill.slug}"
                staged_packages[archive_path] = package
                skill_contexts.append(
                    {
                        "name": version.skill.name,
                        "summary": version.skill.summary,
                        "version": version.version,
                        "root": package_skill_root(names, base_root=extract_root),
                        "extract_root": extract_root,
                        "archive_path": f"/workspace/{archive_path}",
                        "skill_md": version.skill_md,
                        "fixed_execution": fixed_execution_spec(version.manifest or {}),
                        "runtime_requirements": runtime_requirements,
                    }
                )
            input_files = {
                f"input/{item.filename}": storage.read(item.storage_path)
                for item in job.input_files
            }
            # Network access is an administrator-controlled, version-level
            # permission snapshotted onto the job at creation time. Runtime
            # requirement detection remains advisory and never grants access.
            network_enabled = bool(job.network_enabled)
            with DockerSandbox(
                client,
                job_id=job.id,
                execution_id=lease.execution_id if lease is not None else None,
                network_enabled=network_enabled,
            ) as sandbox:
                sandbox.put_files({**staged_packages, **input_files})
                workspace_setup = await sandbox.command(
                    [
                        "mkdir",
                        "-p",
                        "/workspace/output",
                        "/workspace/work",
                        "/workspace/scripts",
                        "/workspace/home",
                        "/workspace/deps/python",
                        "/workspace/deps/node",
                    ],
                    cwd="/workspace",
                    timeout_seconds=30,
                )
                if workspace_setup.exit_code != 0:
                    raise SandboxRuntimeError(
                        "SANDBOX_WORKSPACE_SETUP_FAILED",
                        workspace_setup.stderr or "Could not prepare standard workspace directories",
                    )
                for context in skill_contexts:
                    setup = await sandbox.command(
                        [
                            "python3",
                            "-c",
                            "import sys,zipfile;zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])",
                            str(context["archive_path"]),
                            str(context["extract_root"]),
                        ],
                        timeout_seconds=60,
                    )
                    if setup.exit_code != 0:
                        raise SandboxRuntimeError(
                            "SANDBOX_PACKAGE_SETUP_FAILED",
                            setup.stderr or f"Could not unpack Skill package: {context['name']}",
                        )
                available_binaries = await _preflight_sandbox_binaries(sandbox, skill_contexts)
                add_job_event(
                    db,
                    job,
                    "status",
                    f"已挂载 {len(skill_contexts)} 个 Skill",
                    " · ".join(str(item["name"]) for item in skill_contexts),
                    status="succeeded",
                    data={
                        "skill_count": len(skill_contexts),
                        "skills": [str(item["name"]) for item in skill_contexts],
                        "required_binaries": available_binaries,
                    },
                )
                db.commit()
                fixed_contexts = [
                    context for context in skill_contexts if context.get("fixed_execution")
                ]
                if fixed_contexts and len(skill_contexts) == 1:
                    context = fixed_contexts[0]
                    add_job_event(
                        db,
                        job,
                        "status",
                        "正在执行固定 Skill 入口",
                        "平台将直接运行已审核版本声明的 argv，不经过模型选择",
                        status="running",
                        data={"execution_mode": "fixed"},
                    )
                    db.commit()
                    fixed_result = await execute_fixed_skill(
                        sandbox,
                        execution=context["fixed_execution"],
                        skill_root=str(context["root"]),
                        instruction=effective_instruction(job),
                        input_files=[
                            {
                                "filename": item.filename,
                                "path": f"/workspace/input/{item.filename}",
                                "size_bytes": item.size_bytes,
                            }
                            for item in job.input_files
                        ],
                    )
                    summary = fixed_result.summary
                    artifact_paths = list(fixed_result.artifact_paths)
                    reasoning_turns = 0
                    tool_operations = fixed_result.tool_operations
                    append_run_event(
                        db,
                        run,
                        "fixed.validation",
                        status="succeeded",
                        data={
                            "artifact_sha256": fixed_result.validation_evidence[
                                "artifact_sha256"
                            ],
                            "entrypoint_exit_code": 0,
                            "verifier_type": fixed_result.validation_evidence[
                                "verifier"
                            ]["type"],
                        },
                    )
                    db.commit()
                else:
                    summary, artifact_paths, reasoning_turns, tool_operations = await _run_agent_loop(
                        db,
                        job,
                        sandbox,
                        skill_contexts=skill_contexts,
                        gateway=gateway,
                        job_cancelled=lambda: _job_is_cancelled(
                            db,
                            job,
                            lease=lease,
                            lease_lost=lease_lost,
                        ),
                    )
                _assert_job_lease(db, lease, lease_lost)
                set_step(
                    db,
                    job,
                    "execute-workflow",
                    JobStepStatus.SUCCEEDED,
                    (
                        f"{summary[:820]} · {reasoning_turns} 轮推理 / "
                        f"{tool_operations} 个工具操作"
                    )
                    if summary
                    else (
                        f"Skill 已在独立沙箱中执行完成 · {reasoning_turns} 轮推理 / "
                        f"{tool_operations} 个工具操作"
                    ),
                )
                job.status = JobStatus.PRODUCING_ARTIFACTS
                set_step(db, job, "collect-artifacts", JobStepStatus.RUNNING, "正在从沙箱收集产物")
                add_job_event(db, job, "status", "正在收集任务产物", "将真实文件从一次性沙箱保存到你的工作区", status="running")
                db.commit()
                persisted: list[Artifact] = []
                seen_names: set[str] = set()
                for path in artifact_paths:
                    name = PurePosixPath(path).name
                    if name in seen_names:
                        continue
                    data = sandbox.download_file(path)
                    artifact = _persist_artifact(db, job, actor, path, data)
                    persisted.append(artifact)
                    add_job_event(
                        db,
                        job,
                        "artifact",
                        f"已生成 {artifact.filename}",
                        f"{artifact.size_bytes} 字节 · 等待完整性校验",
                        status="succeeded",
                        data={"artifact_id": artifact.id, "filename": artifact.filename},
                    )
                    seen_names.add(name)
                if not persisted:
                    raise SandboxRuntimeError("SANDBOX_ARTIFACT_MISSING", "No artifact could be collected")
                set_step(
                    db,
                    job,
                    "collect-artifacts",
                    JobStepStatus.SUCCEEDED,
                    f"已收集 {len(persisted)} 个真实文件",
                )

            job.status = JobStatus.VERIFYING
            set_step(db, job, "verify-artifacts", JobStepStatus.RUNNING, "正在校验产物哈希与完整性")
            db.commit()
            _assert_job_lease(db, lease, lease_lost)
            for artifact in persisted:
                stored = storage.read(artifact.storage_path)
                if not stored or len(stored) != artifact.size_bytes or file_sha256(stored) != artifact.sha256:
                    raise SandboxRuntimeError(
                        "ARTIFACT_VERIFICATION_FAILED", f"Artifact verification failed: {artifact.filename}"
                    )
                _validate_artifact_content(artifact.filename, stored)
                artifact.verified = True
            _assert_job_lease(db, lease, lease_lost, lock=True)
            set_step(db, job, "verify-artifacts", JobStepStatus.SUCCEEDED, "所有产物已通过完整性校验")
            job.status = JobStatus.SUCCEEDED
            job.error_code = None
            job.error_message = None
            job.finished_at = utcnow()
            add_job_event(
                db,
                job,
                "result",
                "任务已完成",
                summary[:4000] or f"Skill 已完成执行并生成 {len(persisted)} 个产物。",
                status="succeeded",
                data={
                    "reasoning_turns": reasoning_turns,
                    "tool_operations": tool_operations,
                    "artifact_count": len(persisted),
                },
            )
            add_audit(
                db,
                actor=actor,
                action="workflow_job.sandbox_succeeded",
                resource_type="workflow_job",
                resource_id=job.id,
                details={"artifact_ids": [item.id for item in persisted], "runtime": settings.sandbox_runtime},
            )
            complete_run(
                db,
                run,
                summary={
                    "reasoning_turns": reasoning_turns,
                    "tool_operations": tool_operations,
                    "artifact_count": len(persisted),
                    "runtime": settings.sandbox_runtime,
                },
            )
            db.commit()
        except AgentNeedsInput as exc:
            try:
                _assert_job_lease(db, lease, lease_lost, lock=True)
            except JobLeaseLost:
                db.rollback()
                return
            if job.memory is None:
                job.memory = WorkflowJobMemory(data={})
            job.memory.data = {**job.memory.data, 'pending_question': {'id': uuid4().hex, 'question': str(exc)}}
            job.status = JobStatus.WAITING_USER
            run.status = RunStatus.WAITING_USER
            run.lease_owner = run.lease_token = run.lease_expires_at = None
            for event in job.events:
                if event.status == 'running':
                    event.status = 'waiting_user'
            add_job_event(db, job, 'question', '需要补充信息', str(exc), status='waiting_user')
            db.commit()
        except JobLeaseLost:
            db.rollback()
            logger.info(
                "Stopped stale sandbox attempt after its lease was replaced",
                extra={"job_id": job_id, "attempt": lease.attempt if lease else None},
            )
        except JobCancelled:
            job.status = JobStatus.CANCELLED
            job.finished_at = utcnow()
            for step in job.steps:
                if step.status in {JobStepStatus.PENDING, JobStepStatus.RUNNING}:
                    set_step(db, job, step.step_key, JobStepStatus.SKIPPED, "任务已由用户取消")
            add_job_event(db, job, "status", "任务已取消", "独立沙箱已停止并回收", status="cancelled")
            fail_run(
                db,
                run,
                error_code="WORKFLOW_CANCELLED",
                error_message="任务已由用户取消",
                cancelled=True,
            )
            db.commit()
        except (SandboxRuntimeError, ModelGatewayError) as exc:
            # Do not commit partially collected artifact rows when a later
            # declared file is missing or invalid.
            db.rollback()
            db.refresh(job)
            try:
                _assert_job_lease(db, lease, lease_lost, lock=True)
            except JobLeaseLost:
                db.rollback()
                return
            run = ensure_job_run(db, job)
            running = next((item for item in job.steps if item.status == JobStepStatus.RUNNING), None)
            if running:
                set_step(db, job, running.step_key, JobStepStatus.FAILED, str(exc)[:1000])
            for step in job.steps:
                if step.status == JobStepStatus.PENDING:
                    set_step(db, job, step.step_key, JobStepStatus.SKIPPED, "前序步骤失败，未执行")
            job.error_code = getattr(exc, "code", "SANDBOX_WORKFLOW_FAILED")
            job.error_message = str(exc)[:4000]
            job.finished_at = utcnow()
            blocked = job.error_code in {
                "SKILL_GOAL_BLOCKED",
                "SANDBOX_DEPENDENCY_MISSING",
            }
            job.status = JobStatus.BLOCKED if blocked else JobStatus.FAILED
            if running and blocked:
                set_step(db, job, running.step_key, JobStepStatus.BLOCKED, job.error_message)
            add_job_event(
                db,
                job,
                "status" if blocked else "error",
                "任务受阻" if blocked else "任务执行失败",
                job.error_message,
                status="blocked" if blocked else "failed",
                data={"error_code": job.error_code},
            )
            add_audit(
                db,
                actor=actor,
                action="workflow_job.sandbox_failed",
                resource_type="workflow_job",
                resource_id=job.id,
                details={"error_code": job.error_code},
            )
            fail_run(
                db,
                run,
                error_code=job.error_code,
                error_message=job.error_message,
            )
            db.commit()
        except Exception:
            logger.exception("Unexpected sandbox workflow failure", extra={"job_id": job.id})
            db.rollback()
            db.refresh(job)
            try:
                _assert_job_lease(db, lease, lease_lost, lock=True)
            except JobLeaseLost:
                db.rollback()
                return
            run = ensure_job_run(db, job)
            running = next((item for item in job.steps if item.status == JobStepStatus.RUNNING), None)
            if running:
                set_step(db, job, running.step_key, JobStepStatus.FAILED, "沙箱执行器发生内部错误")
            for step in job.steps:
                if step.status == JobStepStatus.PENDING:
                    set_step(db, job, step.step_key, JobStepStatus.SKIPPED, "前序步骤失败，未执行")
            job.status = JobStatus.FAILED
            job.error_code = "SANDBOX_INTERNAL_ERROR"
            job.error_message = "沙箱工作流执行失败，请查看 Worker 日志"
            job.finished_at = utcnow()
            add_job_event(db, job, "error", "任务执行失败", job.error_message, status="failed", data={"error_code": job.error_code})
            fail_run(
                db,
                run,
                error_code=job.error_code,
                error_message=job.error_message,
            )
            db.commit()


def _active_leased_job_ids() -> set[str]:
    now = utcnow()
    with SessionLocal() as db:
        return {
            str(job_id)
            for job_id in db.scalars(
                select(AgentRun.workflow_job_id).where(
                    AgentRun.run_type == "skill_job",
                    AgentRun.status == RunStatus.RUNNING,
                    AgentRun.workflow_job_id.is_not(None),
                    AgentRun.lease_expires_at.is_not(None),
                    AgentRun.lease_expires_at > now,
                )
            )
        }


def _recover_interrupted_jobs() -> list[ReclaimedSandbox]:
    """Requeue only jobs whose Worker lease expired, preserving other Workers."""

    reclaimed_sandboxes: list[ReclaimedSandbox] = []
    stale_artifact_paths: list[str] = []
    now = utcnow()
    active_statuses = (
        JobStatus.RUNNING,
        JobStatus.PRODUCING_ARTIFACTS,
        JobStatus.VERIFYING,
    )
    with SessionLocal() as db:
        jobs = db.scalars(
            select(WorkflowJob).where(
                WorkflowJob.execution_mode == "sandbox_required",
                WorkflowJob.status.in_(active_statuses),
            ).with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            run = db.scalar(
                select(AgentRun)
                .where(AgentRun.workflow_job_id == job.id)
                .with_for_update()
            )
            if run is None:
                run = ensure_job_run(db, job)
            lease_is_current = (
                run.status == RunStatus.RUNNING
                and run.lease_token is not None
                and _lease_is_after(run.lease_expires_at, now)
            )
            if lease_is_current:
                continue

            reclaimed_sandboxes.append(
                ReclaimedSandbox(
                    job_id=job.id,
                    execution_id=(
                        f"{job.id}-a{run.attempt_count}"
                        if run.attempt_count > 0
                        else job.id
                    ),
                )
            )
            for event in job.events:
                if event.status == "running":
                    event.status = "interrupted"
            for artifact in list(job.artifacts):
                if not artifact.verified:
                    stale_artifact_paths.append(artifact.storage_path)
                    db.delete(artifact)

            answered_attempts = (job.memory.data or {}).get('resumed_attempts', 0) if job.memory else 0
            if run.attempt_count - answered_attempts >= settings.sandbox_worker_max_attempts:
                for step in job.steps:
                    if step.status == JobStepStatus.RUNNING:
                        set_step(
                            db,
                            job,
                            step.step_key,
                            JobStepStatus.FAILED,
                            "Worker 租约过期且自动恢复次数已耗尽",
                        )
                    elif step.status == JobStepStatus.PENDING:
                        set_step(
                            db,
                            job,
                            step.step_key,
                            JobStepStatus.SKIPPED,
                            "自动恢复次数已耗尽",
                        )
                job.status = JobStatus.FAILED
                job.error_code = "SANDBOX_WORKER_RETRY_EXHAUSTED"
                job.error_message = (
                    f"任务已连续中断 {run.attempt_count} 次，已停止自动恢复"
                )
                job.finished_at = now
                add_job_event(
                    db,
                    job,
                    "error",
                    "任务自动恢复失败",
                    job.error_message,
                    status="failed",
                    data={"error_code": job.error_code, "attempts": run.attempt_count},
                )
                fail_run(
                    db,
                    run,
                    error_code=job.error_code,
                    error_message=job.error_message,
                )
                continue

            for step in job.steps:
                if step.step_key == "prepare-input":
                    continue
                step.status = JobStepStatus.PENDING
                step.detail = ""
                step.started_at = None
                step.finished_at = None
            previous_attempt = run.attempt_count
            run.status = RunStatus.QUEUED
            run.lease_owner = None
            run.lease_token = None
            run.heartbeat_at = None
            run.lease_expires_at = None
            run.finished_at = None
            run.error_code = None
            run.error_message = None
            append_run_event(
                db,
                run,
                "attempt.interrupted",
                status="interrupted",
                data={
                    "attempt": previous_attempt,
                    "reason": "lease_expired",
                    "restart_policy": "fresh_attempt",
                    "workspace_preserved": False,
                },
            )
            job.status = JobStatus.QUEUED
            job.error_code = None
            job.error_message = None
            job.finished_at = None
            add_job_event(
                db,
                job,
                "status",
                "正在自动恢复任务",
                "上一执行进程中断；下一次尝试将在新的独立沙箱中从固定 Skill 版本和原始输入重新开始",
                status="queued",
                data={
                    "interrupted_attempt": previous_attempt,
                    "restart_policy": "fresh_attempt",
                    "workspace_restored": False,
                },
            )
        db.commit()
    for path in stale_artifact_paths:
        if path == "pending":
            continue
        try:
            storage.delete(path)
        except OSError:
            logger.warning("Could not remove stale artifact", extra={"storage_path": path})
    return reclaimed_sandboxes


async def run_worker() -> None:
    if not settings.sandbox_worker_enabled:
        raise RuntimeError("SKILLGO_SANDBOX_WORKER_ENABLED must be true for the Worker")
    initialize_schema()
    client = docker_client()
    reclaimed = _recover_interrupted_jobs()
    cleanup_stale_sandboxes(client, protected_job_ids=_active_leased_job_ids())
    for sandbox in reclaimed:
        cleanup_execution_sandbox(
            client,
            job_id=sandbox.job_id,
            execution_id=sandbox.execution_id,
        )
    logger.info(
        "Sandbox Worker ready",
        extra={"runtime": settings.sandbox_runtime, "image": settings.sandbox_image},
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            pass

    while not stopping.is_set():
        reclaimed = await asyncio.to_thread(_recover_interrupted_jobs)
        for sandbox in reclaimed:
            await asyncio.to_thread(
                cleanup_execution_sandbox,
                client,
                job_id=sandbox.job_id,
                execution_id=sandbox.execution_id,
            )
        lease = await asyncio.to_thread(_claim_job)
        if lease is None:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=settings.sandbox_poll_seconds)
            except TimeoutError:
                continue
            continue
        heartbeat_stopping = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(lease, heartbeat_stopping, lease_lost)
        )
        try:
            await asyncio.wait_for(
                execute_sandbox_job(
                    lease.job_id,
                    client,
                    lease=lease,
                    lease_lost=lease_lost,
                ),
                timeout=settings.sandbox_job_timeout_seconds,
            )
        except TimeoutError:
            with SessionLocal() as db:
                job = db.get(WorkflowJob, lease.job_id)
                if job and job.status not in TERMINAL:
                    try:
                        _assert_job_lease(db, lease, lease_lost, lock=True)
                    except JobLeaseLost:
                        db.rollback()
                        continue
                    job.status = JobStatus.FAILED
                    job.error_code = "SANDBOX_JOB_TIMEOUT"
                    job.error_message = f"任务超过 {settings.sandbox_job_timeout_seconds} 秒，沙箱已回收"
                    job.finished_at = utcnow()
                    running = next((item for item in job.steps if item.status == JobStepStatus.RUNNING), None)
                    if running:
                        set_step(db, job, running.step_key, JobStepStatus.FAILED, job.error_message)
                    add_job_event(db, job, "error", "任务执行超时", job.error_message, status="failed", data={"error_code": job.error_code})
                    run = ensure_job_run(db, job)
                    fail_run(
                        db,
                        run,
                        error_code=job.error_code,
                        error_message=job.error_message,
                    )
                    db.commit()
        finally:
            heartbeat_stopping.set()
            await heartbeat_task


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
