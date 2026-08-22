from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import admin_user, super_admin_user
from ..models import AgentConversation, AgentMessage, AgentMessageFile, AgentRun, AgentRunEvent, Artifact, AuditEvent, Conversation, ConversationMessage, Endpoint, Favorite, JobEvent, JobInputFile, JobStatus, JobStep, Role, Run, Skill, SkillVersion, User, VersionStatus, WorkflowEndpointRequest, WorkflowJob, WorkflowJobModel, WorkflowJobPrompt, WorkflowJobSkill, WorkspaceFile, utcnow
from ..schemas import Message, ReviewDecision, StorageCleanupRead, StorageOverview, SystemSummary, UserAdminPatch, UserDeleteRequest, UserRead, UserRolePatch, VersionRead
from ..services import add_audit
from ..storage import storage
from ..storage_lifecycle import cleanup_expired_storage, storage_overview


router = APIRouter(tags=["admin"])


@router.get("/admin/storage", response_model=StorageOverview)
def get_storage_overview(
    _: User = Depends(admin_user), db: Session = Depends(get_db)
) -> dict:
    return storage_overview(db)


@router.post("/admin/storage/cleanup", response_model=StorageCleanupRead)
def run_storage_cleanup(
    actor: User = Depends(admin_user), db: Session = Depends(get_db)
) -> StorageCleanupRead:
    result = cleanup_expired_storage()
    add_audit(
        db,
        actor=actor,
        action="admin.storage.cleanup",
        resource_type="storage",
        resource_id=None,
        details={
            "files_deleted": result.total_files,
            "bytes_released": result.total_bytes,
        },
    )
    db.commit()
    return StorageCleanupRead(
        files_deleted=result.total_files,
        bytes_released=result.total_bytes,
        message="Storage cleanup completed",
    )


@router.get("/admin/reviews", response_model=list[VersionRead])
def review_queue(
    _: User = Depends(admin_user), db: Session = Depends(get_db)
) -> list[SkillVersion]:
    return list(
        db.scalars(
            select(SkillVersion)
            .where(SkillVersion.status.in_([VersionStatus.SUBMITTED, VersionStatus.REVIEWING]))
            .order_by(SkillVersion.created_at.asc())
        ).all()
    )


@router.post("/admin/reviews/{version_id}/approve", response_model=VersionRead)
def approve_version(
    version_id: str,
    payload: ReviewDecision,
    actor: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> SkillVersion:
    version = db.get(SkillVersion, version_id)
    if version is None or version.status not in (VersionStatus.SUBMITTED, VersionStatus.REVIEWING):
        raise HTTPException(status_code=404, detail="Pending version not found")
    version.status = VersionStatus.PUBLISHED
    version.reviewed_by_id = actor.id
    version.review_note = payload.note or None
    version.published_at = utcnow()
    add_audit(
        db,
        actor=actor,
        action="skill.version.approve",
        resource_type="skill_version",
        resource_id=version.id,
        details={"note": payload.note},
    )
    db.commit()
    db.refresh(version)
    return version


@router.post("/admin/reviews/{version_id}/reject", response_model=VersionRead)
def reject_version(
    version_id: str,
    payload: ReviewDecision,
    actor: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> SkillVersion:
    if not payload.note.strip():
        raise HTTPException(status_code=422, detail="Rejection note is required")
    version = db.get(SkillVersion, version_id)
    if version is None or version.status not in (VersionStatus.SUBMITTED, VersionStatus.REVIEWING):
        raise HTTPException(status_code=404, detail="Pending version not found")
    version.status = VersionStatus.REJECTED
    version.reviewed_by_id = actor.id
    version.review_note = payload.note
    add_audit(
        db,
        actor=actor,
        action="skill.version.reject",
        resource_type="skill_version",
        resource_id=version.id,
        details={"note": payload.note},
    )
    db.commit()
    db.refresh(version)
    return version


@router.get("/admin/users", response_model=list[UserRead])
def list_users(
    _: User = Depends(admin_user), db: Session = Depends(get_db)
) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at.desc())).all())


@router.patch("/admin/users/{user_id}", response_model=UserRead)
def update_user_status(
    user_id: str,
    payload: UserAdminPatch,
    actor: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role in (Role.ADMIN, Role.SUPER_ADMIN) and actor.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only the super administrator can manage administrator accounts")
    if target.id == actor.id and not payload.is_active:
        raise HTTPException(status_code=409, detail="Cannot disable your own account")
    target.is_active = payload.is_active
    add_audit(
        db,
        actor=actor,
        action="admin.user.status",
        resource_type="user",
        resource_id=target.id,
        details={"is_active": payload.is_active},
    )
    db.commit()
    db.refresh(target)
    return target


@router.patch("/super-admin/users/{user_id}/role", response_model=UserRead)
def update_user_role(
    user_id: str,
    payload: UserRolePatch,
    actor: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.role == Role.SUPER_ADMIN:
        raise HTTPException(status_code=409, detail="SkillGo can have only one super administrator")
    if target.id == actor.id and payload.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=409, detail="Cannot remove your own super administrator role")
    previous = target.role
    target.role = payload.role
    add_audit(
        db,
        actor=actor,
        action="super_admin.user.role",
        resource_type="user",
        resource_id=target.id,
        details={"from": previous.value, "to": payload.role.value},
    )
    db.commit()
    db.refresh(target)
    return target


@router.post("/super-admin/users/{user_id}/approve-admin", response_model=UserRead)
def approve_admin_application(
    user_id: str,
    actor: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role != Role.ADMIN or target.is_active:
        raise HTTPException(status_code=409, detail="This account has no pending administrator application")
    target.is_active = True
    add_audit(
        db,
        actor=actor,
        action="super_admin.admin_application.approve",
        resource_type="user",
        resource_id=target.id,
    )
    db.commit()
    db.refresh(target)
    return target


@router.post("/super-admin/users/delete", response_model=Message)
def delete_users(
    payload: UserDeleteRequest,
    actor: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> Message:
    targets = list(db.scalars(select(User).where(User.id.in_(payload.user_ids))).all())
    if len(targets) != len(payload.user_ids):
        raise HTTPException(status_code=404, detail="One or more users were not found")
    if any(target.id == actor.id for target in targets):
        raise HTTPException(status_code=409, detail="Cannot delete your own account")

    target_ids = [target.id for target in targets]
    owned_skills = list(
        db.execute(
            select(User.id, User.display_name, func.count(Skill.id))
            .join(Skill, Skill.owner_id == User.id)
            .where(User.id.in_(target_ids))
            .group_by(User.id, User.display_name)
        ).all()
    )
    if owned_skills:
        names = "、".join(f"{name}（{count} 个 Skill）" for _, name, count in owned_skills)
        raise HTTPException(status_code=409, detail=f"请先转移或删除这些账号拥有的 Skill：{names}")

    created_versions = db.scalar(
        select(func.count()).select_from(SkillVersion).where(SkillVersion.created_by_id.in_(target_ids))
    ) or 0
    if created_versions:
        raise HTTPException(status_code=409, detail="所选账号仍关联 Skill 版本，暂不能删除")

    running_jobs = db.scalar(
        select(func.count()).select_from(WorkflowJob).where(
            WorkflowJob.user_id.in_(target_ids),
            WorkflowJob.status.in_([
                JobStatus.CREATED,
                JobStatus.PREPARING,
                JobStatus.QUEUED,
                JobStatus.RUNNING,
                JobStatus.PRODUCING_ARTIFACTS,
                JobStatus.VERIFYING,
            ]),
        )
    ) or 0
    if running_jobs:
        raise HTTPException(status_code=409, detail="所选账号仍有运行中的任务，请等待任务结束后再删除")

    endpoint_ids = select(Endpoint.id).where(Endpoint.owner_id.in_(target_ids))
    job_ids = select(WorkflowJob.id).where(WorkflowJob.user_id.in_(target_ids))
    agent_conversation_ids = select(AgentConversation.id).where(AgentConversation.user_id.in_(target_ids))
    agent_message_ids = select(AgentMessage.id).where(
        or_(
            AgentMessage.user_id.in_(target_ids),
            AgentMessage.conversation_id.in_(agent_conversation_ids),
            AgentMessage.job_id.in_(job_ids),
        )
    )
    agent_run_ids = select(AgentRun.id).where(
        or_(
            AgentRun.user_id.in_(target_ids),
            AgentRun.conversation_id.in_(agent_conversation_ids),
            AgentRun.workflow_job_id.in_(job_ids),
        )
    )
    conversation_ids = select(Conversation.id).where(Conversation.user_id.in_(target_ids))
    run_ids = select(Run.id).where(
        or_(Run.user_id.in_(target_ids), Run.endpoint_id.in_(endpoint_ids))
    )

    storage_paths = set(
        db.scalars(
            select(AgentMessageFile.storage_path).where(
                or_(
                    AgentMessageFile.user_id.in_(target_ids),
                    AgentMessageFile.conversation_id.in_(agent_conversation_ids),
                    AgentMessageFile.message_id.in_(agent_message_ids),
                )
            )
        ).all()
    )
    storage_paths.update(
        db.scalars(
            select(WorkspaceFile.storage_path).where(
                or_(WorkspaceFile.user_id.in_(target_ids), WorkspaceFile.conversation_id.in_(conversation_ids))
            )
        ).all()
    )
    storage_paths.update(
        db.scalars(select(JobInputFile.storage_path).where(or_(JobInputFile.user_id.in_(target_ids), JobInputFile.job_id.in_(job_ids)))).all()
    )
    storage_paths.update(
        db.scalars(select(Artifact.storage_path).where(or_(Artifact.user_id.in_(target_ids), Artifact.job_id.in_(job_ids)))).all()
    )

    db.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id.in_(agent_run_ids)))
    db.execute(delete(AgentRun).where(AgentRun.id.in_(agent_run_ids)))
    db.execute(delete(AgentMessageFile).where(or_(AgentMessageFile.user_id.in_(target_ids), AgentMessageFile.conversation_id.in_(agent_conversation_ids), AgentMessageFile.message_id.in_(agent_message_ids))))
    db.execute(delete(AgentMessage).where(or_(AgentMessage.user_id.in_(target_ids), AgentMessage.conversation_id.in_(agent_conversation_ids), AgentMessage.job_id.in_(job_ids))))
    db.execute(delete(AgentConversation).where(AgentConversation.user_id.in_(target_ids)))

    db.execute(delete(WorkflowEndpointRequest).where(or_(WorkflowEndpointRequest.job_id.in_(job_ids), WorkflowEndpointRequest.endpoint_id.in_(endpoint_ids))))
    db.execute(delete(WorkflowJobSkill).where(WorkflowJobSkill.job_id.in_(job_ids)))
    db.execute(delete(WorkflowJobModel).where(WorkflowJobModel.job_id.in_(job_ids)))
    db.execute(delete(WorkflowJobPrompt).where(WorkflowJobPrompt.job_id.in_(job_ids)))
    db.execute(delete(Artifact).where(or_(Artifact.user_id.in_(target_ids), Artifact.job_id.in_(job_ids))))
    db.execute(delete(JobInputFile).where(or_(JobInputFile.user_id.in_(target_ids), JobInputFile.job_id.in_(job_ids))))
    db.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
    db.execute(delete(JobStep).where(JobStep.job_id.in_(job_ids)))
    db.execute(delete(WorkflowJob).where(WorkflowJob.user_id.in_(target_ids)))

    db.execute(delete(ConversationMessage).where(or_(ConversationMessage.conversation_id.in_(conversation_ids), ConversationMessage.run_id.in_(run_ids))))
    db.execute(delete(WorkspaceFile).where(or_(WorkspaceFile.user_id.in_(target_ids), WorkspaceFile.conversation_id.in_(conversation_ids))))
    db.execute(delete(Conversation).where(Conversation.user_id.in_(target_ids)))
    db.execute(delete(Run).where(or_(Run.user_id.in_(target_ids), Run.endpoint_id.in_(endpoint_ids))))
    db.execute(delete(Endpoint).where(Endpoint.owner_id.in_(target_ids)))
    db.execute(delete(Favorite).where(Favorite.user_id.in_(target_ids)))
    db.execute(update(AuditEvent).where(AuditEvent.actor_id.in_(target_ids)).values(actor_id=None))
    db.execute(update(SkillVersion).where(SkillVersion.reviewed_by_id.in_(target_ids)).values(reviewed_by_id=None))
    db.execute(delete(User).where(User.id.in_(target_ids)))
    add_audit(
        db,
        actor=actor,
        action="super_admin.user.delete",
        resource_type="user",
        resource_id=None,
        details={"deleted_user_ids": target_ids, "count": len(target_ids)},
    )
    db.commit()
    for path in storage_paths:
        try:
            storage.delete(path)
        except (OSError, ValueError):
            pass
    return Message(message=f"Deleted {len(target_ids)} user account(s)")


@router.get("/super-admin/system/summary", response_model=SystemSummary)
def system_summary(
    _: User = Depends(super_admin_user), db: Session = Depends(get_db)
) -> SystemSummary:
    return SystemSummary(
        users=db.scalar(select(func.count()).select_from(User)) or 0,
        admins=db.scalar(
            select(func.count()).select_from(User).where(User.role.in_([Role.ADMIN, Role.SUPER_ADMIN]))
        )
        or 0,
        skills=db.scalar(select(func.count()).select_from(Skill)) or 0,
        published_versions=db.scalar(
            select(func.count()).select_from(SkillVersion).where(
                SkillVersion.status == VersionStatus.PUBLISHED
            )
        )
        or 0,
        pending_reviews=db.scalar(
            select(func.count()).select_from(SkillVersion).where(
                SkillVersion.status.in_([VersionStatus.SUBMITTED, VersionStatus.REVIEWING])
            )
        )
        or 0,
        runs=db.scalar(select(func.count()).select_from(Run)) or 0,
        endpoints=db.scalar(select(func.count()).select_from(Endpoint)) or 0,
    )
