from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent, Endpoint, Run, Skill, SkillVersion, User, VersionStatus
from .runtime_profile import version_runtime_profile
from .schemas import EndpointRead, RunRead, SkillRead


def add_audit(
    db: Session,
    *,
    actor: User | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditEvent(
            actor_id=actor.id if actor else None,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
    )


def latest_version(skill: Skill, published_only: bool = False) -> SkillVersion | None:
    versions = skill.versions
    if published_only:
        versions = [item for item in versions if item.status == VersionStatus.PUBLISHED]
    return versions[-1] if versions else None


def skill_read(skill: Skill) -> SkillRead:
    latest = latest_version(skill, published_only=False)
    return SkillRead(
        id=skill.id,
        owner_id=skill.owner_id,
        owner_name=skill.owner.display_name,
        slug=skill.slug,
        name=skill.name,
        summary=skill.summary,
        description=skill.description,
        category=skill.category,
        visibility=skill.visibility,
        icon=skill.icon,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        favorite_count=len(skill.favorites),
        latest_version=latest.version if latest else None,
        latest_status=latest.status if latest else None,
    )


def get_skill_or_none(db: Session, skill_id: str) -> Skill | None:
    return db.scalar(select(Skill).where(Skill.id == skill_id))


def run_read(run: Run, context_message_count: int = 0) -> RunRead:
    return RunRead(
        id=run.id,
        skill_id=run.skill_id,
        skill_version_id=run.skill_version_id,
        skill_name=run.skill.name,
        version=run.skill_version.version,
        endpoint_id=run.endpoint_id,
        endpoint_slug=run.endpoint.slug if run.endpoint else None,
        status=run.status,
        invocation_type=run.invocation_type,
        input=run.input_data,
        output=run.output_data,
        error_code=run.error_code,
        error_message=run.error_message,
        model_name=run.model_name,
        token_usage=run.token_usage or {},
        latency_ms=run.latency_ms,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        context_message_count=context_message_count,
    )


def endpoint_read(endpoint: Endpoint) -> EndpointRead:
    execution_mode = str(version_runtime_profile(endpoint.skill_version)["execution_mode"])
    return EndpointRead(
        id=endpoint.id,
        owner_id=endpoint.owner_id,
        skill_id=endpoint.skill_id,
        skill_version_id=endpoint.skill_version_id,
        skill_name=endpoint.skill.name,
        version=endpoint.skill_version.version,
        slug=endpoint.slug,
        name=endpoint.name,
        is_active=endpoint.is_active,
        execution_mode=execution_mode,
        invocation_mode="sync" if execution_mode == "instruction_only" else "async",
        api_key_prefix=endpoint.api_key_prefix,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )
