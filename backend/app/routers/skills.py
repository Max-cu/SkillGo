from __future__ import annotations

from copy import deepcopy
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import current_user
from ..model_gateway import OpenAICompatibleGateway, get_model_gateway
from ..models import Artifact, Conversation, ConversationMessage, Endpoint, Favorite, JobEvent, JobInputFile, JobStep, Role, Run, Skill, SkillVersion, User, VersionStatus, Visibility, WorkflowEndpointRequest, WorkflowJob, WorkspaceFile, utcnow
from ..schemas import Message, SkillCreate, SkillDetail, SkillPackageAnalysis, SkillRead, SkillVisibilityUpdate, VersionRead
from ..services import add_audit, get_skill_or_none, latest_version, skill_read
from ..skill_analysis import analyze_package
from ..skill_package import PackageValidationError, validate_skill_package
from ..runtime_profile import detect_runtime_profile
from ..storage import storage


router = APIRouter(tags=["skills"])


def _can_manage(skill: Skill, user: User) -> bool:
    return skill.owner_id == user.id or user.role in (Role.ADMIN, Role.SUPER_ADMIN)


@router.get("/community/skills", response_model=list[SkillRead])
def community_skills(
    query: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
) -> list[SkillRead]:
    skills = db.scalars(
        select(Skill).where(Skill.visibility == Visibility.PUBLIC).order_by(Skill.updated_at.desc())
    ).all()
    result = []
    for skill in skills:
        if not latest_version(skill, published_only=True):
            continue
        if category and skill.category != category:
            continue
        if query:
            needle = query.casefold()
            if needle not in f"{skill.name} {skill.summary} {skill.description}".casefold():
                continue
        result.append(skill_read(skill))
    return result


@router.get("/community/skills/{slug}", response_model=SkillDetail)
def community_skill(slug: str, db: Session = Depends(get_db)) -> SkillDetail:
    skill = db.scalar(
        select(Skill).where(Skill.slug == slug, Skill.visibility == Visibility.PUBLIC)
    )
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    versions = [item for item in skill.versions if item.status == VersionStatus.PUBLISHED]
    if not versions:
        raise HTTPException(status_code=404, detail="Skill not found")
    return SkillDetail(**skill_read(skill).model_dump(), versions=versions)


@router.get("/skills/mine", response_model=list[SkillRead])
def my_skills(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[SkillRead]:
    skills = db.scalars(
        select(Skill).where(Skill.owner_id == user.id).order_by(Skill.updated_at.desc())
    ).all()
    return [skill_read(item) for item in skills]


@router.post("/skills", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def create_skill(
    payload: SkillCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SkillRead:
    if db.scalar(select(Skill).where(Skill.slug == payload.slug)):
        raise HTTPException(status_code=409, detail="Skill slug is already in use")
    skill = Skill(owner_id=user.id, **payload.model_dump())
    db.add(skill)
    db.flush()
    add_audit(
        db,
        actor=user,
        action="skill.create",
        resource_type="skill",
        resource_id=skill.id,
        details={"slug": skill.slug, "visibility": skill.visibility.value},
    )
    db.commit()
    db.refresh(skill)
    return skill_read(skill)


@router.post("/skills/analyze-package", response_model=SkillPackageAnalysis)
async def analyze_skill_package(
    package: UploadFile = File(...),
    user: User = Depends(current_user),
    gateway: OpenAICompatibleGateway = Depends(get_model_gateway),
) -> SkillPackageAnalysis:
    del user  # Authentication is required; package analysis does not persist user data.
    data = await package.read(settings.max_upload_bytes + 1)
    try:
        validated = validate_skill_package(data)
    except PackageValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    suggestion = await analyze_package(validated, gateway)
    return SkillPackageAnalysis(
        **suggestion,
        version=validated.version,
        skill_type=validated.skill_type,
        package_format=validated.package_format,
    )


@router.get("/skills/{skill_id}", response_model=SkillDetail)
def get_owned_skill(
    skill_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SkillDetail:
    skill = get_skill_or_none(db, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if _can_manage(skill, user):
        versions = skill.versions
    elif skill.visibility == Visibility.PUBLIC:
        versions = [item for item in skill.versions if item.status == VersionStatus.PUBLISHED]
        if not versions:
            raise HTTPException(status_code=404, detail="Skill not found")
    else:
        raise HTTPException(status_code=404, detail="Skill not found")
    return SkillDetail(**skill_read(skill).model_dump(), versions=versions)


@router.patch("/skills/{skill_id}/visibility", response_model=SkillRead)
def update_skill_visibility(
    skill_id: str,
    payload: SkillVisibilityUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SkillRead:
    skill = get_skill_or_none(db, skill_id)
    if skill is None or not _can_manage(skill, user):
        raise HTTPException(status_code=404, detail="Skill not found")
    if payload.visibility == Visibility.PUBLIC and not latest_version(skill, published_only=True):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PUBLISHED_VERSION_REQUIRED",
                "message": "至少需要一个审核通过的版本才能发布到社区",
            },
        )
    previous = skill.visibility
    skill.visibility = payload.visibility
    add_audit(
        db,
        actor=user,
        action="skill.community.publish" if payload.visibility == Visibility.PUBLIC else "skill.visibility.update",
        resource_type="skill",
        resource_id=skill.id,
        details={"from": previous.value, "to": payload.visibility.value},
    )
    db.commit()
    db.refresh(skill)
    return skill_read(skill)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    skill_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    skill = get_skill_or_none(db, skill_id)
    if skill is None or (skill.owner_id != user.id and user.role != Role.SUPER_ADMIN):
        raise HTTPException(status_code=404, detail="Skill not found")

    package_paths = list(
        db.scalars(select(SkillVersion.package_path).where(SkillVersion.skill_id == skill.id))
    )
    version_count = len(package_paths)
    run_count = len(list(db.scalars(select(Run.id).where(Run.skill_id == skill.id))))
    endpoint_count = len(
        list(db.scalars(select(Endpoint.id).where(Endpoint.skill_id == skill.id)))
    )

    conversation_ids = select(Conversation.id).where(Conversation.skill_id == skill.id)
    workspace_paths = list(
        db.scalars(
            select(WorkspaceFile.storage_path).where(
                WorkspaceFile.conversation_id.in_(conversation_ids)
            )
        )
    )
    workflow_job_ids = select(WorkflowJob.id).where(WorkflowJob.skill_id == skill.id)
    endpoint_ids = select(Endpoint.id).where(Endpoint.skill_id == skill.id)
    job_input_paths = list(
        db.scalars(select(JobInputFile.storage_path).where(JobInputFile.job_id.in_(workflow_job_ids)))
    )
    artifact_paths = list(
        db.scalars(select(Artifact.storage_path).where(Artifact.job_id.in_(workflow_job_ids)))
    )
    workflow_job_count = len(
        list(db.scalars(select(WorkflowJob.id).where(WorkflowJob.skill_id == skill.id)))
    )
    db.execute(
        delete(ConversationMessage).where(
            ConversationMessage.conversation_id.in_(conversation_ids)
        )
    )
    db.execute(
        delete(WorkspaceFile).where(
            WorkspaceFile.conversation_id.in_(conversation_ids)
        )
    )
    db.execute(
        delete(WorkflowEndpointRequest).where(
            WorkflowEndpointRequest.endpoint_id.in_(endpoint_ids)
        )
    )
    db.execute(delete(Run).where(Run.skill_id == skill.id))
    db.execute(delete(Endpoint).where(Endpoint.skill_id == skill.id))
    db.execute(delete(Artifact).where(Artifact.job_id.in_(workflow_job_ids)))
    db.execute(delete(JobInputFile).where(JobInputFile.job_id.in_(workflow_job_ids)))
    db.execute(delete(JobEvent).where(JobEvent.job_id.in_(workflow_job_ids)))
    db.execute(delete(JobStep).where(JobStep.job_id.in_(workflow_job_ids)))
    db.execute(delete(WorkflowJob).where(WorkflowJob.skill_id == skill.id))
    db.execute(delete(Conversation).where(Conversation.skill_id == skill.id))
    db.execute(delete(Favorite).where(Favorite.skill_id == skill.id))
    db.execute(delete(SkillVersion).where(SkillVersion.skill_id == skill.id))
    db.execute(delete(Skill).where(Skill.id == skill.id))
    add_audit(
        db,
        actor=user,
        action="skill.delete",
        resource_type="skill",
        resource_id=skill.id,
        details={
            "slug": skill.slug,
            "versions_deleted": version_count,
            "runs_deleted": run_count,
            "endpoints_deleted": endpoint_count,
            "workflow_jobs_deleted": workflow_job_count,
        },
    )
    db.commit()

    for package_path in [*package_paths, *workspace_paths, *job_input_paths, *artifact_paths]:
        try:
            storage.delete(package_path)
        except OSError:
            # The database remains authoritative; an orphaned local blob can be cleaned later.
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/skills/{skill_id}/versions",
    response_model=VersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_version(
    skill_id: str,
    package: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SkillVersion:
    skill = get_skill_or_none(db, skill_id)
    if skill is None or not _can_manage(skill, user):
        raise HTTPException(status_code=404, detail="Skill not found")
    data = await package.read(settings.max_upload_bytes + 1)
    try:
        validated = validate_skill_package(data)
    except PackageValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    resolved_version = validated.version
    if not validated.version_explicit:
        existing_versions = list(
            db.scalars(select(SkillVersion.version).where(SkillVersion.skill_id == skill.id))
        )
        parsed_versions = []
        for item in existing_versions:
            core = item.split("-", 1)[0].split("+", 1)[0]
            try:
                parsed_versions.append(tuple(int(part) for part in core.split(".")))
            except ValueError:
                continue
        if parsed_versions:
            major, minor, patch = max(parsed_versions)
            resolved_version = f"{major}.{minor}.{patch + 1}"

    existing = db.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill.id,
            SkillVersion.version == resolved_version,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="Version already exists")
    stored_manifest = deepcopy(validated.manifest)
    stored_manifest.setdefault("metadata", {})["version"] = resolved_version
    extension = stored_manifest.get("x-skillgo")
    if not isinstance(extension, dict):
        extension = {}
        stored_manifest["x-skillgo"] = extension
    extension["runtime"] = detect_runtime_profile(
        skill_md=validated.skill_md,
        manifest=stored_manifest,
        file_names=validated.file_names,
    )
    version = SkillVersion(
        skill_id=skill.id,
        created_by_id=user.id,
        version=resolved_version,
        status=VersionStatus.READY,
        skill_type=validated.skill_type,
        package_sha256=validated.sha256,
        package_path="pending",
        manifest=stored_manifest,
        skill_md=validated.skill_md,
        input_schema=validated.input_schema,
        output_schema=validated.output_schema,
        requested_permissions=validated.permissions,
    )
    db.add(version)
    db.flush()
    key = f"skill-packages/{skill.id}/{version.id}/{validated.sha256}.zip"
    version.package_path = storage.put(key, data)
    add_audit(
        db,
        actor=user,
        action="skill.version.upload",
        resource_type="skill_version",
        resource_id=version.id,
        details={
            "sha256": validated.sha256,
            "version": resolved_version,
            "package_format": validated.package_format,
        },
    )
    db.commit()
    db.refresh(version)
    return version


@router.post("/skills/{skill_id}/versions/{version_id}/submit", response_model=VersionRead)
def submit_version(
    skill_id: str,
    version_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SkillVersion:
    skill = get_skill_or_none(db, skill_id)
    if skill is None or not _can_manage(skill, user):
        raise HTTPException(status_code=404, detail="Skill not found")
    version = db.get(SkillVersion, version_id)
    if version is None or version.skill_id != skill.id:
        raise HTTPException(status_code=404, detail="Version not found")
    if version.status not in (VersionStatus.READY, VersionStatus.REJECTED):
        raise HTTPException(status_code=409, detail="Version cannot be submitted")
    version.status = VersionStatus.SUBMITTED
    version.review_note = None
    add_audit(
        db,
        actor=user,
        action="skill.version.submit",
        resource_type="skill_version",
        resource_id=version.id,
    )
    db.commit()
    db.refresh(version)
    return version


@router.post("/skills/{skill_id}/favorite", response_model=Message)
def favorite_skill(
    skill_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Message:
    skill = get_skill_or_none(db, skill_id)
    if skill is None or skill.visibility != Visibility.PUBLIC or not latest_version(skill, True):
        raise HTTPException(status_code=404, detail="Skill not found")
    existing = db.scalar(
        select(Favorite).where(Favorite.user_id == user.id, Favorite.skill_id == skill.id)
    )
    if existing is None:
        db.add(Favorite(user_id=user.id, skill_id=skill.id))
        add_audit(
            db,
            actor=user,
            action="skill.favorite",
            resource_type="skill",
            resource_id=skill.id,
        )
        db.commit()
        return Message(message="Skill saved")
    return Message(message="Skill already saved")


@router.get("/skills/{skill_id}/versions/{version_id}/download")
def download_version(
    skill_id: str,
    version_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    skill = get_skill_or_none(db, skill_id)
    version = db.get(SkillVersion, version_id)
    if skill is None or version is None or version.skill_id != skill.id:
        raise HTTPException(status_code=404, detail="Version not found")
    allowed = _can_manage(skill, user) or (
        skill.visibility == Visibility.PUBLIC and version.status == VersionStatus.PUBLISHED
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Version not found")
    data = storage.read(version.package_path)
    add_audit(
        db,
        actor=user,
        action="skill.version.download",
        resource_type="skill_version",
        resource_id=version.id,
    )
    db.commit()
    filename = f"{skill.slug}-{version.version}.zip"
    return StreamingResponse(
        BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
