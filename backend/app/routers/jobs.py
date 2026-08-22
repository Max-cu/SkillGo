from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..conversation_service import can_run_version
from ..database import get_db
from ..deps import current_user
from ..execution_runtime import ensure_job_run, fail_run
from ..model_gateway import ModelGatewayError, OpenAICompatibleGateway, get_model_gateway
from ..models import AgentConversation, AgentMessage, AgentMessageFile, Artifact, Endpoint, JobInputFile, JobStatus, JobStep, JobStepStatus, Skill, SkillVersion, User, VersionStatus, WorkflowEndpointRequest, WorkflowJob, WorkflowJobModel, WorkflowJobPrompt, WorkflowJobSkill, utcnow
from ..runtime_profile import version_runtime_profile
from ..schemas import ArtifactRead, Message, StoragePinUpdate, WorkflowJobRead
from ..security import verify_endpoint_key
from ..services import add_audit
from ..storage import storage
from ..workflow_execution import add_job_event, execute_instruction_job_background, set_step
from ..workspace_service import (
    WorkspaceFileError,
    extract_workspace_text,
    file_sha256,
    safe_content_type,
    safe_workspace_filename,
)


router = APIRouter(tags=["workflow-jobs"])
MAX_JOB_SKILLS = 5
MAX_JOB_FILES = 5


def _owned_job(db: Session, job_id: str, user: User) -> WorkflowJob:
    job = db.get(WorkflowJob, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Workflow job not found")
    return job


def _job_read(job: WorkflowJob) -> WorkflowJobRead:
    return WorkflowJobRead.model_validate(job)


def _resolve_version_ids(
    db: Session,
    *,
    requested_ids: list[str],
    user: User,
) -> list[SkillVersion]:
    requested_ids = [item.strip() for item in requested_ids if item.strip()]
    if not requested_ids:
        return []
    unique_ids = list(dict.fromkeys(requested_ids))
    if len(unique_ids) > MAX_JOB_SKILLS:
        raise HTTPException(
            status_code=422,
            detail={"code": "TOO_MANY_SKILLS", "message": f"一次任务最多可挂载 {MAX_JOB_SKILLS} 个 Skill"},
        )

    versions: list[SkillVersion] = []
    seen_skill_ids: set[str] = set()
    for version_id in unique_ids:
        version = db.get(SkillVersion, version_id)
        if version is None or not can_run_version(version, user):
            raise HTTPException(status_code=404, detail="Skill version not found")
        if version.skill_id in seen_skill_ids:
            raise HTTPException(
                status_code=422,
                detail={"code": "DUPLICATE_SKILL", "message": "同一任务不能同时挂载同一个 Skill 的多个版本"},
            )
        seen_skill_ids.add(version.skill_id)
        versions.append(version)
    return versions


def _resolve_job_versions(
    db: Session,
    *,
    primary_version_id: str,
    raw_version_ids: str,
    user: User,
) -> list[SkillVersion]:
    requested_ids: list[str] = [primary_version_id] if primary_version_id.strip() else []
    if raw_version_ids.strip():
        try:
            decoded = json.loads(raw_version_ids)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "SKILL_VERSION_LIST_INVALID", "message": "version_ids 必须是 JSON 数组"},
            ) from exc
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise HTTPException(
                status_code=422,
                detail={"code": "SKILL_VERSION_LIST_INVALID", "message": "version_ids 必须是字符串数组"},
            )
        requested_ids.extend(item.strip() for item in decoded if item.strip())

    return _resolve_version_ids(db, requested_ids=requested_ids, user=user)


def _parse_message_content(raw_content: str, instruction: str) -> list[dict]:
    if not raw_content.strip():
        return [{"type": "text", "text": instruction}] if instruction else []
    try:
        decoded = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "MESSAGE_CONTENT_INVALID", "message": "message_content 必须是 JSON 数组"},
        ) from exc
    if not isinstance(decoded, list) or len(decoded) > 200:
        raise HTTPException(
            status_code=422,
            detail={"code": "MESSAGE_CONTENT_INVALID", "message": "消息结构无效或节点过多"},
        )
    parts: list[dict] = []
    text_size = 0
    for item in decoded:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail={"code": "MESSAGE_CONTENT_INVALID", "message": "消息节点必须是对象"})
        part_type = item.get("type")
        if part_type == "text":
            value = item.get("text")
            if not isinstance(value, str):
                raise HTTPException(status_code=422, detail={"code": "MESSAGE_CONTENT_INVALID", "message": "文本节点缺少 text"})
            text_size += len(value)
            if value:
                if parts and parts[-1].get("type") == "text":
                    parts[-1]["text"] += value
                else:
                    parts.append({"type": "text", "text": value})
        elif part_type == "skill_ref":
            skill_id = item.get("skill_id")
            version_id = item.get("skill_version_id")
            if not isinstance(skill_id, str) or not skill_id.strip() or not isinstance(version_id, str) or not version_id.strip():
                raise HTTPException(status_code=422, detail={"code": "SKILL_REFERENCE_INVALID", "message": "Skill 节点缺少有效版本"})
            parts.append({"type": "skill_ref", "skill_id": skill_id.strip(), "skill_version_id": version_id.strip()})
        else:
            raise HTTPException(status_code=422, detail={"code": "MESSAGE_CONTENT_INVALID", "message": "消息包含不支持的节点类型"})
    if text_size > 20_000:
        raise HTTPException(status_code=422, detail={"code": "MESSAGE_TOO_LONG", "message": "任务描述不能超过 20,000 个字符"})
    return parts


def _parse_existing_file_ids(raw: str) -> list[str]:
    if not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="历史附件参数格式无效") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise HTTPException(status_code=422, detail="历史附件参数必须是文件 ID 数组")
    normalized = [item.strip() for item in decoded if item.strip()]
    if len(normalized) != len(set(normalized)):
        raise HTTPException(status_code=422, detail="不能重复选择同一个历史附件")
    return normalized


def _render_instruction(parts: list[dict]) -> str:
    rendered = "".join(
        str(item.get("text", ""))
        if item.get("type") == "text"
        else f"【使用 Skill：{item.get('skill_name') or '指定 Skill'}】"
        for item in parts
    ).strip()
    return rendered


def _normalize_message_content(parts: list[dict], versions: list[SkillVersion]) -> list[dict]:
    by_id = {item.id: item for item in versions}
    normalized: list[dict] = []
    for part in parts:
        if part.get("type") == "text":
            normalized.append({"type": "text", "text": str(part.get("text", ""))})
            continue
        version = by_id.get(str(part.get("skill_version_id", "")))
        if version is None or version.skill_id != part.get("skill_id"):
            raise HTTPException(status_code=422, detail={"code": "SKILL_REFERENCE_MISMATCH", "message": "Skill 节点与所选版本不一致"})
        normalized.append(
            {
                "type": "skill_ref",
                "skill_id": version.skill_id,
                "skill_version_id": version.id,
                "skill_name": version.skill.name,
                "version": version.version,
            }
        )
    return normalized


def _routing_candidates(db: Session, user: User) -> list[SkillVersion]:
    versions = db.scalars(
        select(SkillVersion).join(Skill).order_by(Skill.updated_at.desc(), SkillVersion.created_at.desc()).limit(400)
    ).all()
    candidates: list[SkillVersion] = []
    seen: set[str] = set()
    for version in versions:
        if version.skill_id in seen or not can_run_version(version, user):
            continue
        if version.status in {VersionStatus.DEPRECATED, VersionStatus.YANKED}:
            continue
        if not version_runtime_profile(version).get("runnable"):
            continue
        seen.add(version.skill_id)
        candidates.append(version)
        if len(candidates) >= 40:
            break
    return candidates


def _fallback_route(instruction: str, filename: str | None, candidates: list[SkillVersion]) -> list[SkillVersion]:
    haystack = f"{instruction} {filename or ''}".casefold()
    tokens = set(re.findall(r"[a-z0-9_-]{2,}", haystack))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", haystack):
        for size in (2, 3, 4):
            tokens.update(chunk[index:index + size] for index in range(max(0, len(chunk) - size + 1)))
    ranked: list[tuple[int, int, SkillVersion]] = []
    for position, version in enumerate(candidates):
        skill = version.skill
        searchable = f"{skill.name} {skill.slug} {skill.summary} {skill.description}".casefold()
        score = 100 if skill.name.casefold() in haystack or skill.slug.casefold() in haystack else 0
        score += sum(4 for token in tokens if token in searchable)
        ranked.append((score, -position, version))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [ranked[0][2]]


async def _auto_route_versions(
    db: Session,
    *,
    user: User,
    gateway: OpenAICompatibleGateway,
    instruction: str,
    filename: str | None,
) -> list[SkillVersion]:
    candidates = _routing_candidates(db, user)
    if not candidates:
        raise HTTPException(status_code=422, detail={"code": "NO_RUNNABLE_SKILL", "message": "当前没有可运行的 Skill，请先上传或发布一个 Skill"})
    if len(candidates) == 1:
        return candidates
    metadata = [
        {
            "version_id": item.id,
            "name": item.skill.name,
            "summary": item.skill.summary,
            "description": item.skill.description[:800],
            "category": item.skill.category,
        }
        for item in candidates
    ]
    try:
        result = await gateway.route_skills(instruction=instruction, filename=filename, candidates=metadata)
        ids = result.output.get("version_ids")
        allowed = {item.id for item in candidates}
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= MAX_JOB_SKILLS
            or len(ids) != len(set(ids))
            or any(not isinstance(item, str) or item not in allowed for item in ids)
        ):
            raise ValueError("model returned invalid Skill ids")
        return _resolve_version_ids(db, requested_ids=ids, user=user)
    except (ModelGatewayError, ValueError, TypeError):
        return _fallback_route(instruction, filename, candidates)


def _job_runtime_profile(versions: list[SkillVersion]) -> dict:
    if len(versions) == 1:
        return version_runtime_profile(versions[0])

    profiles = [version_runtime_profile(version) for version in versions]
    requirements = {
        "runtimes": sorted(
            {
                str(item)
                for profile in profiles
                for item in (profile.get("requirements", {}).get("runtimes") or [])
            }
        ),
        "scripts": [
            str(item)
            for profile in profiles
            for item in (profile.get("requirements", {}).get("scripts") or [])
        ][:200],
        "tools": sorted(
            {
                str(item)
                for profile in profiles
                for item in (profile.get("requirements", {}).get("tools") or [])
            }
        ),
        "tool_adapters": {
            str(tool): str(adapter)
            for profile in profiles
            for tool, adapter in (
                profile.get("requirements", {}).get("tool_adapters") or {}
            ).items()
        },
        "platform_tools": sorted(
            {
                str(item)
                for profile in profiles
                for item in (profile.get("requirements", {}).get("platform_tools") or [])
            }
        ),
        "binaries": sorted(
            {
                str(item)
                for profile in profiles
                for item in (profile.get("requirements", {}).get("binaries") or [])
            }
        ),
        "network": any(
            bool(profile.get("requirements", {}).get("network")) for profile in profiles
        ),
        "network_rules": sorted(
            {
                str(item)
                for profile in profiles
                for item in (profile.get("requirements", {}).get("network_rules") or [])
            }
        ),
        "network_targets": sorted(
            {
                str(item)
                for profile in profiles
                for item in (profile.get("requirements", {}).get("network_targets") or [])
            }
        ),
        "expected_artifacts": sorted(
            {
                str(item)
                for profile in profiles
                for item in (profile.get("requirements", {}).get("expected_artifacts") or [])
            }
        ),
    }
    # Read the active settings object at call time so environment/test runtime
    # changes are reflected consistently with version_runtime_profile.
    from .. import config

    runnable = config.settings.sandbox_worker_enabled
    return {
        "execution_mode": "sandbox_required",
        "runtime_status": "available" if runnable else "awaiting_sandbox",
        "runnable": runnable,
        "block_reason": None if runnable else "多 Skill 协同任务需要 Linux 沙箱 Worker",
        "requirements": requirements,
        "reasons": [f"协同挂载 {len(versions)} 个 Skill", "由同一个 Agent 在独立沙箱中协调执行"],
    }


def _add_job_input_file(
    db: Session,
    *,
    job: WorkflowJob,
    user: User,
    data: bytes,
    original_filename: str | None,
    content_type: str | None,
) -> tuple[JobInputFile, str | None]:
    try:
        filename = safe_workspace_filename(original_filename)
        extracted_text = extract_workspace_text(filename, data)
    except WorkspaceFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if any(item.filename.casefold() == filename.casefold() for item in job.input_files):
        raise HTTPException(status_code=422, detail=f"附件名称重复：{filename}")

    input_file = JobInputFile(
        job=job,
        user_id=user.id,
        filename=filename,
        content_type=safe_content_type(content_type),
        size_bytes=len(data),
        sha256=file_sha256(data),
        storage_path="pending",
        readable=extracted_text is not None,
        extracted_text=extracted_text,
    )
    db.add(input_file)
    db.flush()
    input_file.storage_path = storage.put(
        f"job-inputs/{user.id}/{job.id}/{input_file.id}/{filename}", data
    )
    add_job_event(
        db,
        job,
        "input",
        f"已接收 {filename}",
        f"{len(data)} 字节 · {'可直接读取' if extracted_text is not None else '由沙箱工具解析'}",
        status="succeeded",
        data={"filename": filename, "size_bytes": len(data)},
    )
    return input_file, extracted_text


def _prepare_file_job(
    db: Session,
    *,
    version: SkillVersion,
    user: User,
    data: bytes,
    original_filename: str | None,
    content_type: str | None,
    instruction: str,
    trigger: str,
    actor: User | None,
    versions: list[SkillVersion] | None = None,
    model_name: str | None = None,
    message_content: list[dict] | None = None,
    routing_mode: str = "legacy",
    audit_details: dict | None = None,
) -> tuple[WorkflowJob, str | None, dict]:
    selected_versions = versions or [version]
    profile = _job_runtime_profile(selected_versions)
    job = WorkflowJob(
        user_id=user.id,
        skill_id=version.skill_id,
        skill_version_id=version.id,
        status=JobStatus.PREPARING,
        execution_mode=str(profile["execution_mode"]),
        trigger=trigger,
        instruction=instruction.strip(),
    )
    db.add(job)
    db.flush()
    for position, selected_version in enumerate(selected_versions, 1):
        db.add(
            WorkflowJobSkill(
                job_id=job.id,
                skill_id=selected_version.skill_id,
                skill_version_id=selected_version.id,
                position=position,
            )
        )
    db.flush()
    if model_name:
        db.add(WorkflowJobModel(job_id=job.id, model_name=model_name))
        db.flush()
    db.add(
        WorkflowJobPrompt(
            job_id=job.id,
            content=message_content or ([{"type": "text", "text": instruction.strip()}] if instruction.strip() else []),
            routing_mode=routing_mode,
        )
    )
    db.flush()
    for position, (step_key, name) in enumerate(
        (
            ("prepare-input", "校验并准备输入文件"),
            ("execute-workflow", "执行 Skill 工作流"),
            ("collect-artifacts", "收集任务产物"),
            ("verify-artifacts", "校验任务产物"),
        ),
        1,
    ):
        db.add(JobStep(job_id=job.id, step_key=step_key, name=name, position=position))
    db.flush()

    input_file, extracted_text = _add_job_input_file(
        db,
        job=job,
        user=user,
        data=data,
        original_filename=original_filename,
        content_type=content_type,
    )
    set_step(
        db,
        job,
        "prepare-input",
        JobStepStatus.SUCCEEDED,
        f"已校验 {input_file.filename}（{len(data)} 字节）",
    )
    details = {
        "version_id": version.id,
        "version_ids": [item.id for item in selected_versions],
        "skill_count": len(selected_versions),
        "execution_mode": profile["execution_mode"],
        "input_sha256": input_file.sha256,
        "trigger": trigger,
    }
    details.update(audit_details or {})
    add_audit(
        db,
        actor=actor,
        action="workflow_job.create",
        resource_type="workflow_job",
        resource_id=job.id,
        details=details,
    )
    return job, extracted_text, profile


def _active_endpoint(db: Session, endpoint_slug: str, endpoint_key: str | None) -> Endpoint:
    endpoint = db.scalar(
        select(Endpoint).where(Endpoint.slug == endpoint_slug, Endpoint.is_active.is_(True))
    )
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    if not endpoint_key or not verify_endpoint_key(endpoint_key, endpoint.api_key_hash):
        raise HTTPException(status_code=401, detail="Invalid endpoint key")
    return endpoint


def _endpoint_job(db: Session, endpoint: Endpoint, job_id: str) -> WorkflowJob:
    binding = db.scalar(
        select(WorkflowEndpointRequest).where(
            WorkflowEndpointRequest.endpoint_id == endpoint.id,
            WorkflowEndpointRequest.job_id == job_id,
        )
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="Workflow job not found")
    job = db.get(WorkflowJob, job_id)
    if job is None or job.user_id != endpoint.owner_id:
        raise HTTPException(status_code=404, detail="Workflow job not found")
    return job


def _cancel_job(
    db: Session,
    job: WorkflowJob,
    *,
    actor: User | None,
    audit_details: dict | None = None,
) -> Message:
    terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.BLOCKED}
    if job.status in terminal:
        raise HTTPException(status_code=409, detail="Workflow job is already finished")
    job.status = JobStatus.CANCELLED
    job.finished_at = utcnow()
    for step in job.steps:
        if step.status in {JobStepStatus.PENDING, JobStepStatus.RUNNING}:
            set_step(db, job, step.step_key, JobStepStatus.SKIPPED, "任务已取消")
    add_job_event(db, job, "status", "任务已取消", "沙箱将安全停止并回收", status="cancelled")
    run = ensure_job_run(db, job)
    fail_run(
        db,
        run,
        error_code="WORKFLOW_CANCELLED",
        error_message="任务已由用户取消",
        cancelled=True,
    )
    add_audit(
        db,
        actor=actor,
        action="workflow_job.cancel",
        resource_type="workflow_job",
        resource_id=job.id,
        details=audit_details,
    )
    db.commit()
    return Message(message="Workflow job cancelled")


def _artifact_download(
    db: Session,
    job: WorkflowJob,
    artifact_id: str,
    *,
    actor: User | None,
    audit_details: dict | None = None,
) -> Response:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None or artifact.job_id != job.id or artifact.user_id != job.user_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.purged_at is not None:
        raise HTTPException(status_code=410, detail="产物已超过 15 天保留期")
    data = storage.read(artifact.storage_path)
    details = {"job_id": job.id, "sha256": artifact.sha256}
    details.update(audit_details or {})
    add_audit(
        db,
        actor=actor,
        action="workflow_artifact.download",
        resource_type="artifact",
        resource_id=artifact.id,
        details=details,
    )
    db.commit()
    encoded_name = quote(artifact.filename)
    return StreamingResponse(
        BytesIO(data),
        media_type=artifact.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@router.post("/jobs", response_model=WorkflowJobRead, status_code=status.HTTP_201_CREATED)
async def create_workflow_job(
    background_tasks: BackgroundTasks,
    version_id: Annotated[str, Form()] = "",
    version_ids: Annotated[str, Form(max_length=4000)] = "",
    message_content: Annotated[str, Form(max_length=60_000)] = "",
    file: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
    existing_file_ids: Annotated[str, Form(max_length=4_000)] = "",
    instruction: Annotated[str, Form(max_length=20_000)] = "",
    model_name: Annotated[str, Form(max_length=160)] = "",
    agent_conversation_id: Annotated[str, Form(max_length=36)] = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    gateway: OpenAICompatibleGateway = Depends(get_model_gateway),
) -> WorkflowJobRead:
    try:
        selected_gateway = gateway.for_model(model_name)
    except ModelGatewayError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    agent_conversation: AgentConversation | None = None
    if agent_conversation_id.strip():
        agent_conversation = db.scalar(
            select(AgentConversation).where(
                AgentConversation.id == agent_conversation_id.strip(),
                AgentConversation.user_id == user.id,
            )
        )
        if agent_conversation is None:
            raise HTTPException(status_code=404, detail="工作台会话不存在")

    incoming_uploads = ([file] if file is not None else []) + list(files or [])
    existing_ids = _parse_existing_file_ids(existing_file_ids)
    if existing_ids and agent_conversation is None:
        raise HTTPException(status_code=422, detail="复用历史附件时必须指定当前工作台会话")
    if len(incoming_uploads) + len(existing_ids) > MAX_JOB_FILES:
        raise HTTPException(status_code=422, detail=f"一次任务最多可添加 {MAX_JOB_FILES} 个附件")

    resolved_inputs: list[tuple[bytes, str, str]] = []
    for upload in incoming_uploads:
        data = await upload.read(settings.workspace_max_file_bytes + 1)
        if not data or len(data) > settings.workspace_max_file_bytes:
            raise HTTPException(status_code=422, detail="附件为空或超过平台大小限制")
        try:
            filename = safe_workspace_filename(upload.filename)
        except WorkspaceFileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        resolved_inputs.append((data, filename, safe_content_type(upload.content_type)))
    if existing_ids:
        stored_files = db.scalars(
            select(AgentMessageFile).where(
                AgentMessageFile.id.in_(existing_ids),
                AgentMessageFile.conversation_id == agent_conversation.id,
                AgentMessageFile.user_id == user.id,
                AgentMessageFile.purged_at.is_(None),
            )
        ).all()
        by_id = {item.id: item for item in stored_files}
        if len(by_id) != len(existing_ids):
            raise HTTPException(status_code=410, detail="历史附件已超过 15 天保留期，请重新上传")
        for file_id in existing_ids:
            item = by_id[file_id]
            resolved_inputs.append(
                (storage.read(item.storage_path), item.filename, item.content_type)
            )
    input_names = [item[1].casefold() for item in resolved_inputs]
    if len(input_names) != len(set(input_names)):
        raise HTTPException(status_code=422, detail="同一任务中的附件名称不能重复")

    parts = _parse_message_content(message_content, instruction)
    if not parts and not resolved_inputs:
        raise HTTPException(status_code=422, detail="请描述任务或上传文件")
    explicit_version_ids = [
        str(item["skill_version_id"])
        for item in parts
        if item.get("type") == "skill_ref"
    ]
    if explicit_version_ids:
        if len(explicit_version_ids) != len(set(explicit_version_ids)):
            raise HTTPException(
                status_code=422,
                detail={"code": "DUPLICATE_SKILL", "message": "同一任务不能重复插入同一个 Skill"},
            )
        selected_versions = _resolve_version_ids(
            db, requested_ids=explicit_version_ids, user=user
        )
        routing_mode = "explicit"
    else:
        selected_versions = _resolve_job_versions(
            db,
            primary_version_id=version_id,
            raw_version_ids=version_ids,
            user=user,
        )
        routing_mode = "explicit"

    preliminary_instruction = _render_instruction(parts) or instruction.strip()
    if not selected_versions:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SKILL_REQUIRED",
                "message": "Skill 任务必须由用户明确插入一个或多个 Skill；普通对话请使用工作台对话接口",
            },
        )
    version = selected_versions[0]
    normalized_parts = _normalize_message_content(parts, selected_versions)
    resolved_instruction = _render_instruction(normalized_parts) or preliminary_instruction

    if not resolved_inputs:
        if not resolved_instruction:
            raise HTTPException(status_code=422, detail="请描述任务或上传文件")
        resolved_inputs = [
            (resolved_instruction.encode("utf-8"), "task-request.txt", "text/plain; charset=utf-8")
        ]
        trigger = "chat_message"
    else:
        trigger = "file_upload"
    data, original_filename, content_type = resolved_inputs[0]
    job, extracted_text, profile = _prepare_file_job(
        db,
        version=version,
        user=user,
        data=data,
        original_filename=original_filename,
        content_type=content_type,
        instruction=resolved_instruction,
        trigger=trigger,
        actor=user,
        versions=selected_versions,
        model_name=selected_gateway.model_name,
        message_content=normalized_parts,
        routing_mode=routing_mode,
    )
    extracted_texts = [extracted_text]
    for extra_data, extra_filename, extra_content_type in resolved_inputs[1:]:
        _, extra_text = _add_job_input_file(
            db,
            job=job,
            user=user,
            data=extra_data,
            original_filename=extra_filename,
            content_type=extra_content_type,
        )
        extracted_texts.append(extra_text)
    if len(job.input_files) > 1:
        set_step(
            db,
            job,
            "prepare-input",
            JobStepStatus.SUCCEEDED,
            f"已校验 {len(job.input_files)} 个输入文件",
        )

    if agent_conversation is not None:
        visible_files = [
            {
                "filename": item.filename,
                "size_bytes": item.size_bytes,
                "content_type": item.content_type,
            }
            for item in job.input_files
            if not (job.trigger == "chat_message" and item.filename == "task-request.txt")
        ]
        user_message = AgentMessage(
            conversation_id=agent_conversation.id,
            user_id=user.id,
            role="user",
            kind="text",
            content={
                "message": resolved_instruction,
                "parts": normalized_parts,
                "files": visible_files,
            },
        )
        db.add(user_message)
        db.flush()
        visible_inputs = [
            item
            for item in job.input_files
            if not (job.trigger == "chat_message" and item.filename == "task-request.txt")
        ]
        for item in visible_inputs:
            input_data = storage.read(item.storage_path)
            message_file = AgentMessageFile(
                conversation_id=agent_conversation.id,
                message_id=user_message.id,
                user_id=user.id,
                filename=item.filename,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                storage_path="pending",
                extracted_text=item.extracted_text,
            )
            db.add(message_file)
            db.flush()
            message_file.storage_path = storage.put(
                f"agent-files/{user.id[:8]}/{agent_conversation.id[:8]}/{message_file.id}/{item.filename}",
                input_data,
            )
        db.add(
            AgentMessage(
                conversation_id=agent_conversation.id,
                user_id=user.id,
                job_id=job.id,
                role="assistant",
                kind="workflow",
                content={"job_id": job.id},
                model_name=selected_gateway.model_name,
            )
        )
        agent_conversation.updated_at = utcnow()
        db.flush()

    execution_mode = str(profile.get("execution_mode"))
    if not profile.get("runnable"):
        job.status = JobStatus.BLOCKED
        job.error_code = str(profile.get("runtime_status")).upper()
        job.error_message = str(
            profile.get("block_reason")
            or "运行环境尚未准备完成"
        )
        set_step(
            db,
            job,
            "execute-workflow",
            JobStepStatus.BLOCKED,
            job.error_message,
        )
        add_job_event(db, job, "error", "当前环境暂不可运行", job.error_message, status="blocked")
        db.commit()
        db.refresh(job)
        return _job_read(job)

    if execution_mode == "sandbox_required":
        job.status = JobStatus.QUEUED
        set_step(
            db,
            job,
            "execute-workflow",
            JobStepStatus.PENDING,
            "已进入 Linux 沙箱执行队列",
        )
        add_job_event(
            db,
            job,
            "status",
            "已进入独立沙箱队列",
            (
                f"Worker 将在同一个一次性隔离环境中挂载 {len(selected_versions)} 个 Skill"
                if len(selected_versions) > 1
                else "Worker 将为本次任务创建一次性隔离环境"
            ),
            status="queued",
            data={"skill_count": len(selected_versions)},
        )
        db.commit()
        db.refresh(job)
        return _job_read(job)

    if execution_mode != "instruction_only":
        job.status = JobStatus.BLOCKED
        job.error_code = "EXECUTOR_ADAPTER_NOT_CONNECTED"
        job.error_message = "运行环境已声明可用，但对应执行器适配器尚未接入"
        set_step(
            db,
            job,
            "execute-workflow",
            JobStepStatus.BLOCKED,
            job.error_message,
        )
        add_job_event(db, job, "error", "执行器尚未接入", job.error_message, status="blocked")
        db.commit()
        db.refresh(job)
        return _job_read(job)

    if any(item is None for item in extracted_texts):
        job.status = JobStatus.FAILED
        job.error_code = "INPUT_NOT_READABLE"
        job.error_message = "当前可信执行器无法读取该文件格式"
        set_step(db, job, "execute-workflow", JobStepStatus.FAILED, job.error_message)
        for key in ("collect-artifacts", "verify-artifacts"):
            set_step(db, job, key, JobStepStatus.SKIPPED, "输入文件不可读取")
        add_job_event(db, job, "error", "无法读取输入文件", job.error_message, status="failed")
        job.finished_at = utcnow()
        db.commit()
        db.refresh(job)
        return _job_read(job)

    add_job_event(db, job, "status", "Skill 已开始处理", "正在调用模型执行完整指令", status="running")
    db.commit()
    db.refresh(job)
    response = _job_read(job)
    background_tasks.add_task(
        execute_instruction_job_background,
        job.id,
        user.id,
        selected_gateway,
    )
    return response


@router.post(
    "/workflow-endpoints/{endpoint_slug}/jobs",
    response_model=WorkflowJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def invoke_workflow_endpoint(
    endpoint_slug: str,
    response: Response,
    file: UploadFile = File(...),
    instruction: Annotated[str, Form(max_length=20_000)] = "",
    endpoint_key: Annotated[str | None, Header(alias="X-SkillGo-Key")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    db: Session = Depends(get_db),
) -> WorkflowJobRead:
    endpoint = _active_endpoint(db, endpoint_slug, endpoint_key)
    version = endpoint.skill_version
    profile = version_runtime_profile(version)
    if profile.get("execution_mode") != "sandbox_required":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SYNC_ENDPOINT_REQUIRED",
                "message": "This Skill uses the synchronous /invoke endpoint",
            },
        )
    if not profile.get("runnable"):
        raise HTTPException(
            status_code=503,
            detail={
                "code": str(profile.get("runtime_status", "RUNTIME_UNAVAILABLE")).upper(),
                "message": str(profile.get("block_reason") or "Runtime is unavailable"),
            },
        )

    normalized_idempotency_key = (idempotency_key or "").strip()
    if len(normalized_idempotency_key) > 200:
        raise HTTPException(status_code=422, detail="Idempotency-Key exceeds 200 characters")
    idempotency_hash = (
        hashlib.sha256(normalized_idempotency_key.encode("utf-8")).hexdigest()
        if normalized_idempotency_key
        else None
    )
    if idempotency_hash:
        existing = db.scalar(
            select(WorkflowEndpointRequest).where(
                WorkflowEndpointRequest.endpoint_id == endpoint.id,
                WorkflowEndpointRequest.idempotency_key_hash == idempotency_hash,
            )
        )
        if existing is not None:
            job = _endpoint_job(db, endpoint, existing.job_id)
            response.headers["Location"] = (
                f"/api/v1/workflow-endpoints/{endpoint.slug}/jobs/{job.id}"
            )
            response.headers["X-Idempotent-Replay"] = "true"
            return _job_read(job)

    data = await file.read(settings.workspace_max_file_bytes + 1)
    if not data or len(data) > settings.workspace_max_file_bytes:
        raise HTTPException(status_code=422, detail="Input file is empty or exceeds the limit")
    owner = db.get(User, endpoint.owner_id)
    if owner is None or not owner.is_active:
        raise HTTPException(status_code=409, detail="Endpoint owner is unavailable")
    job, _, _ = _prepare_file_job(
        db,
        version=version,
        user=owner,
        data=data,
        original_filename=file.filename,
        content_type=file.content_type,
        instruction=instruction,
        trigger="api",
        actor=None,
        audit_details={"endpoint_id": endpoint.id},
    )
    job.status = JobStatus.QUEUED
    set_step(
        db,
        job,
        "execute-workflow",
        JobStepStatus.PENDING,
        "API 任务已进入 Linux 沙箱执行队列",
    )
    add_job_event(
        db,
        job,
        "status",
        "API 任务已进入独立沙箱队列",
        "可通过任务状态接口持续读取执行事件",
        status="queued",
    )
    input_storage_path = db.scalar(
        select(JobInputFile.storage_path).where(JobInputFile.job_id == job.id)
    )
    db.add(
        WorkflowEndpointRequest(
            endpoint_id=endpoint.id,
            job_id=job.id,
            idempotency_key_hash=idempotency_hash,
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if input_storage_path:
            try:
                storage.delete(input_storage_path)
            except OSError:
                pass
        if idempotency_hash:
            existing = db.scalar(
                select(WorkflowEndpointRequest).where(
                    WorkflowEndpointRequest.endpoint_id == endpoint.id,
                    WorkflowEndpointRequest.idempotency_key_hash == idempotency_hash,
                )
            )
            if existing is not None:
                replayed_job = _endpoint_job(db, endpoint, existing.job_id)
                response.headers["Location"] = (
                    f"/api/v1/workflow-endpoints/{endpoint.slug}/jobs/{replayed_job.id}"
                )
                response.headers["X-Idempotent-Replay"] = "true"
                return _job_read(replayed_job)
        raise HTTPException(status_code=409, detail="Workflow request could not be created") from exc
    db.refresh(job)
    response.headers["Location"] = (
        f"/api/v1/workflow-endpoints/{endpoint.slug}/jobs/{job.id}"
    )
    return _job_read(job)


@router.get(
    "/workflow-endpoints/{endpoint_slug}/jobs/{job_id}",
    response_model=WorkflowJobRead,
)
def get_workflow_endpoint_job(
    endpoint_slug: str,
    job_id: str,
    endpoint_key: Annotated[str | None, Header(alias="X-SkillGo-Key")] = None,
    db: Session = Depends(get_db),
) -> WorkflowJobRead:
    endpoint = _active_endpoint(db, endpoint_slug, endpoint_key)
    return _job_read(_endpoint_job(db, endpoint, job_id))


@router.post(
    "/workflow-endpoints/{endpoint_slug}/jobs/{job_id}/cancel",
    response_model=Message,
)
def cancel_workflow_endpoint_job(
    endpoint_slug: str,
    job_id: str,
    endpoint_key: Annotated[str | None, Header(alias="X-SkillGo-Key")] = None,
    db: Session = Depends(get_db),
) -> Message:
    endpoint = _active_endpoint(db, endpoint_slug, endpoint_key)
    job = _endpoint_job(db, endpoint, job_id)
    return _cancel_job(
        db,
        job,
        actor=None,
        audit_details={"endpoint_id": endpoint.id},
    )


@router.get(
    "/workflow-endpoints/{endpoint_slug}/jobs/{job_id}/artifacts",
    response_model=list[ArtifactRead],
)
def list_workflow_endpoint_artifacts(
    endpoint_slug: str,
    job_id: str,
    endpoint_key: Annotated[str | None, Header(alias="X-SkillGo-Key")] = None,
    db: Session = Depends(get_db),
) -> list[ArtifactRead]:
    endpoint = _active_endpoint(db, endpoint_slug, endpoint_key)
    job = _endpoint_job(db, endpoint, job_id)
    return [ArtifactRead.model_validate(item) for item in job.artifacts]


@router.get("/workflow-endpoints/{endpoint_slug}/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_workflow_endpoint_artifact(
    endpoint_slug: str,
    job_id: str,
    artifact_id: str,
    endpoint_key: Annotated[str | None, Header(alias="X-SkillGo-Key")] = None,
    db: Session = Depends(get_db),
) -> Response:
    endpoint = _active_endpoint(db, endpoint_slug, endpoint_key)
    job = _endpoint_job(db, endpoint, job_id)
    return _artifact_download(
        db,
        job,
        artifact_id,
        actor=None,
        audit_details={"endpoint_id": endpoint.id},
    )


@router.get("/jobs", response_model=list[WorkflowJobRead])
def list_workflow_jobs(
    skill_id: str | None = None,
    limit: int = 50,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[WorkflowJobRead]:
    statement = select(WorkflowJob).where(WorkflowJob.user_id == user.id)
    if skill_id:
        statement = statement.where(WorkflowJob.skill_id == skill_id)
    jobs = db.scalars(statement.order_by(WorkflowJob.created_at.desc()).limit(min(max(limit, 1), 100))).all()
    return [_job_read(item) for item in jobs]


@router.get("/jobs/{job_id}", response_model=WorkflowJobRead)
def get_workflow_job(
    job_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> WorkflowJobRead:
    return _job_read(_owned_job(db, job_id, user))


@router.patch("/jobs/{job_id}/storage", response_model=WorkflowJobRead)
def update_job_storage_retention(
    job_id: str,
    payload: StoragePinUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> WorkflowJobRead:
    job = _owned_job(db, job_id, user)
    job.storage_pinned = payload.pinned
    add_audit(
        db,
        actor=user,
        action="workflow_job.storage_pin" if payload.pinned else "workflow_job.storage_unpin",
        resource_type="workflow_job",
        resource_id=job.id,
        details={"pinned": payload.pinned},
    )
    db.commit()
    db.refresh(job)
    return _job_read(job)


@router.post("/jobs/{job_id}/cancel", response_model=Message)
def cancel_workflow_job(
    job_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Message:
    job = _owned_job(db, job_id, user)
    return _cancel_job(db, job, actor=user)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow_job(
    job_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    job = _owned_job(db, job_id, user)
    terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.BLOCKED}
    if job.status not in terminal:
        raise HTTPException(status_code=409, detail="运行中的任务不能删除，请先停止任务")

    input_paths = [item.storage_path for item in job.input_files]
    artifact_paths = [item.storage_path for item in job.artifacts]
    linked_message_ids = select(AgentMessage.id).where(AgentMessage.job_id == job.id)
    linked_message_file_paths = list(
        db.scalars(
            select(AgentMessageFile.storage_path).where(
                AgentMessageFile.message_id.in_(linked_message_ids)
            )
        )
    )

    # Endpoint requests are also the idempotency record and do not cascade from
    # workflow_jobs. Agent messages are removed explicitly so a deleted task
    # cannot leave a broken task card in the general workspace conversation.
    db.execute(
        delete(AgentMessageFile).where(
            AgentMessageFile.message_id.in_(linked_message_ids)
        )
    )
    db.execute(delete(AgentMessage).where(AgentMessage.job_id == job.id))
    db.execute(delete(WorkflowEndpointRequest).where(WorkflowEndpointRequest.job_id == job.id))
    add_audit(
        db,
        actor=user,
        action="workflow_job.delete",
        resource_type="workflow_job",
        resource_id=job.id,
        details={
            "status": job.status.value,
            "trigger": job.trigger,
            "input_files_deleted": len(input_paths),
            "artifacts_deleted": len(artifact_paths),
        },
    )
    db.delete(job)
    db.commit()

    for path in [*input_paths, *artifact_paths, *linked_message_file_paths]:
        try:
            storage.delete(path)
        except OSError:
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=WorkflowJobRead,
    status_code=status.HTTP_201_CREATED,
)
async def retry_workflow_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    gateway: OpenAICompatibleGateway = Depends(get_model_gateway),
) -> WorkflowJobRead:
    source = _owned_job(db, job_id, user)
    if source.status not in {JobStatus.FAILED, JobStatus.BLOCKED, JobStatus.CANCELLED}:
        raise HTTPException(status_code=409, detail="只有失败、受阻或已取消的任务可以重试")
    if not source.input_files:
        raise HTTPException(status_code=409, detail="原任务没有可复用的输入文件")
    if any(item.purged_at is not None for item in source.input_files):
        raise HTTPException(status_code=410, detail="原任务输入文件已超过 15 天保留期，无法直接重试")

    version_ids = [item["skill_version_id"] for item in source.selected_skills]
    selected_versions = _resolve_version_ids(db, requested_ids=version_ids, user=user)
    try:
        selected_gateway = gateway.for_model(source.model_name or "")
    except ModelGatewayError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    stored_inputs: list[tuple[bytes, str, str]] = []
    try:
        for item in source.input_files:
            stored_inputs.append(
                (storage.read(item.storage_path), item.filename, item.content_type)
            )
    except OSError as exc:
        raise HTTPException(status_code=409, detail="原任务输入文件已经不可用") from exc

    first_data, first_name, first_type = stored_inputs[0]
    job, first_text, profile = _prepare_file_job(
        db,
        version=selected_versions[0],
        user=user,
        data=first_data,
        original_filename=first_name,
        content_type=first_type,
        instruction=source.instruction,
        trigger="retry",
        actor=user,
        versions=selected_versions,
        model_name=selected_gateway.model_name,
        message_content=source.message_content,
        routing_mode=source.routing_mode,
        audit_details={"retry_of": source.id},
    )
    extracted_texts = [first_text]
    for data, filename, content_type in stored_inputs[1:]:
        _, extracted_text = _add_job_input_file(
            db,
            job=job,
            user=user,
            data=data,
            original_filename=filename,
            content_type=content_type,
        )
        extracted_texts.append(extracted_text)
    if len(job.input_files) > 1:
        set_step(db, job, "prepare-input", JobStepStatus.SUCCEEDED, f"已复用并校验 {len(job.input_files)} 个输入文件")

    source_message = db.scalar(
        select(AgentMessage).where(
            AgentMessage.job_id == source.id,
            AgentMessage.user_id == user.id,
        )
    )
    if source_message is not None:
        conversation = db.get(AgentConversation, source_message.conversation_id)
        if conversation is not None and conversation.user_id == user.id:
            visible_inputs = [
                item
                for item in job.input_files
                if not (source.trigger == "chat_message" and item.filename == "task-request.txt")
            ]
            user_message = AgentMessage(
                conversation_id=conversation.id,
                user_id=user.id,
                role="user",
                kind="text",
                content={
                    "message": source.instruction,
                    "parts": source.message_content,
                    "files": [
                        {
                            "filename": item.filename,
                            "size_bytes": item.size_bytes,
                            "content_type": item.content_type,
                        }
                        for item in visible_inputs
                    ],
                    "retry_of": source.id,
                },
            )
            db.add(user_message)
            db.flush()
            for item in visible_inputs:
                data = storage.read(item.storage_path)
                message_file = AgentMessageFile(
                    conversation_id=conversation.id,
                    message_id=user_message.id,
                    user_id=user.id,
                    filename=item.filename,
                    content_type=item.content_type,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                    storage_path="pending",
                    extracted_text=item.extracted_text,
                )
                db.add(message_file)
                db.flush()
                message_file.storage_path = storage.put(
                    f"agent-files/{user.id[:8]}/{conversation.id[:8]}/{message_file.id}/{item.filename}",
                    data,
                )
            db.add(
                AgentMessage(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    job_id=job.id,
                    role="assistant",
                    kind="workflow",
                    content={"job_id": job.id, "retry_of": source.id},
                    model_name=selected_gateway.model_name,
                )
            )
            conversation.updated_at = utcnow()

    execution_mode = str(profile.get("execution_mode"))
    if not profile.get("runnable"):
        job.status = JobStatus.BLOCKED
        job.error_code = str(profile.get("runtime_status")).upper()
        job.error_message = str(profile.get("block_reason") or "运行环境尚未准备完成")
        set_step(db, job, "execute-workflow", JobStepStatus.BLOCKED, job.error_message)
        add_job_event(db, job, "error", "当前环境暂不可运行", job.error_message, status="blocked")
    elif execution_mode == "sandbox_required":
        job.status = JobStatus.QUEUED
        set_step(db, job, "execute-workflow", JobStepStatus.PENDING, "重试任务已进入 Linux 沙箱执行队列")
        add_job_event(db, job, "status", "已重新进入独立沙箱队列", "原输入文件与 Skill 配置已完整复用", status="queued")
    elif execution_mode != "instruction_only":
        job.status = JobStatus.BLOCKED
        job.error_code = "EXECUTOR_ADAPTER_NOT_CONNECTED"
        job.error_message = "运行环境已声明可用，但对应执行器适配器尚未接入"
        set_step(db, job, "execute-workflow", JobStepStatus.BLOCKED, job.error_message)
        add_job_event(db, job, "error", "执行器尚未接入", job.error_message, status="blocked")
    elif any(item is None for item in extracted_texts):
        job.status = JobStatus.FAILED
        job.error_code = "INPUT_NOT_READABLE"
        job.error_message = "当前可信执行器无法读取其中一个输入文件"
        set_step(db, job, "execute-workflow", JobStepStatus.FAILED, job.error_message)
        for key in ("collect-artifacts", "verify-artifacts"):
            set_step(db, job, key, JobStepStatus.SKIPPED, "输入文件不可读取")
        add_job_event(db, job, "error", "无法读取输入文件", job.error_message, status="failed")
        job.finished_at = utcnow()
    else:
        add_job_event(db, job, "status", "Skill 已重新开始处理", "正在调用模型执行完整指令", status="running")

    db.commit()
    db.refresh(job)
    if profile.get("runnable") and execution_mode == "instruction_only" and all(item is not None for item in extracted_texts):
        background_tasks.add_task(
            execute_instruction_job_background,
            job.id,
            user.id,
            selected_gateway,
        )
    return _job_read(job)


@router.get("/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(
    job_id: str,
    artifact_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    job = _owned_job(db, job_id, user)
    return _artifact_download(db, job, artifact_id, actor=user)
