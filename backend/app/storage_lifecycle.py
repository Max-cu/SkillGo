from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from shutil import disk_usage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .models import (
    AgentMessageFile,
    Artifact,
    JobInputFile,
    JobStatus,
    SkillVersion,
    User,
    WorkflowJob,
    WorkspaceFile,
    utcnow,
)
from .storage import storage
from .services import add_audit


TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.BLOCKED,
}


@dataclass(frozen=True)
class CleanupResult:
    files_marked: int
    bytes_released: int
    orphan_files_deleted: int
    orphan_bytes_released: int

    @property
    def total_files(self) -> int:
        return self.files_marked + self.orphan_files_deleted

    @property
    def total_bytes(self) -> int:
        return self.bytes_released + self.orphan_bytes_released


def _sum_size(db: Session, model, *conditions) -> int:
    return int(
        db.scalar(select(func.coalesce(func.sum(model.size_bytes), 0)).where(*conditions))
        or 0
    )


def storage_overview(db: Session) -> dict:
    categories = {
        "conversation_attachments": _sum_size(
            db, AgentMessageFile, AgentMessageFile.purged_at.is_(None)
        )
        + _sum_size(db, WorkspaceFile, WorkspaceFile.purged_at.is_(None)),
        "job_inputs": _sum_size(db, JobInputFile, JobInputFile.purged_at.is_(None)),
        "artifacts": _sum_size(db, Artifact, Artifact.purged_at.is_(None)),
    }
    users: dict[str, dict] = {}
    for model in (AgentMessageFile, WorkspaceFile, JobInputFile, Artifact):
        rows = db.execute(
            select(model.user_id, func.coalesce(func.sum(model.size_bytes), 0), func.count(model.id))
            .where(model.purged_at.is_(None))
            .group_by(model.user_id)
        ).all()
        for user_id, size_bytes, file_count in rows:
            item = users.setdefault(str(user_id), {"user_id": str(user_id), "size_bytes": 0, "file_count": 0})
            item["size_bytes"] += int(size_bytes or 0)
            item["file_count"] += int(file_count or 0)
    identities = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(users.keys()))).all()
    }
    user_rows = []
    for item in sorted(users.values(), key=lambda item: item["size_bytes"], reverse=True):
        identity = identities.get(item["user_id"])
        user_rows.append(
            {
                **item,
                "display_name": identity.display_name if identity else "已删除用户",
                "email": identity.email if identity else "-",
            }
        )
    stored_objects = storage.objects()
    skillgo_total = sum(item.size_bytes for item in stored_objects)
    disk = disk_usage(storage.root)
    managed_total = sum(categories.values())
    return {
        "retention_days": settings.storage_retention_days,
        "disk_total_bytes": disk.total,
        # Treat filesystem-reserved blocks as used so the displayed capacity
        # always reconciles: used + available == total.
        "disk_used_bytes": disk.total - disk.free,
        "disk_free_bytes": disk.free,
        "skillgo_bytes": skillgo_total,
        "managed_bytes": managed_total,
        "categories": categories,
        "users": user_rows,
    }


def cleanup_expired_storage(*, include_orphans: bool = True) -> CleanupResult:
    now = utcnow()
    cutoff = now - timedelta(days=settings.storage_retention_days)
    paths: list[str] = []
    released = 0

    with SessionLocal() as db:
        conversation_files = [
            *db.scalars(
                select(AgentMessageFile).where(
                    AgentMessageFile.purged_at.is_(None),
                    AgentMessageFile.created_at < cutoff,
                ).with_for_update(skip_locked=True)
            ).all(),
            *db.scalars(
                select(WorkspaceFile).where(
                    WorkspaceFile.purged_at.is_(None),
                    WorkspaceFile.created_at < cutoff,
                ).with_for_update(skip_locked=True)
            ).all(),
        ]
        eligible_jobs = select(WorkflowJob.id).where(
            WorkflowJob.status.in_(TERMINAL_JOB_STATUSES),
            WorkflowJob.storage_pinned.is_(False),
            WorkflowJob.finished_at.is_not(None),
            WorkflowJob.finished_at < cutoff,
        )
        task_files = [
            *db.scalars(
                select(JobInputFile).where(
                    JobInputFile.purged_at.is_(None),
                    JobInputFile.job_id.in_(eligible_jobs),
                ).with_for_update(skip_locked=True)
            ).all(),
            *db.scalars(
                select(Artifact).where(
                    Artifact.purged_at.is_(None),
                    Artifact.job_id.in_(eligible_jobs),
                ).with_for_update(skip_locked=True)
            ).all(),
        ]
        expired = [*conversation_files, *task_files]
        for item in expired:
            item.purged_at = now
            paths.append(item.storage_path)
            released += item.size_bytes
            if hasattr(item, "extracted_text"):
                item.extracted_text = None
        if expired:
            add_audit(
                db,
                actor=None,
                action="system.storage.retention_cleanup",
                resource_type="storage",
                resource_id=None,
                details={
                    "retention_days": settings.storage_retention_days,
                    "files_marked": len(expired),
                    "bytes_marked": released,
                },
            )
        db.commit()

    for path in paths:
        try:
            storage.delete(path)
        except (OSError, ValueError):
            # The database marker is authoritative. Any failed physical delete
            # becomes an orphan and is retried by a later sweep.
            pass

    orphan_count = 0
    orphan_bytes = 0
    if include_orphans:
        grace = now - timedelta(hours=max(1, settings.storage_orphan_grace_hours))
        with SessionLocal() as db:
            referenced = set(db.scalars(select(SkillVersion.package_path)).all())
            for model in (AgentMessageFile, WorkspaceFile, JobInputFile, Artifact):
                referenced.update(
                    db.scalars(
                        select(model.storage_path).where(model.purged_at.is_(None))
                    ).all()
                )
        for item in storage.objects():
            if item.key in referenced or item.modified_at >= grace:
                continue
            try:
                if storage.delete(item.key):
                    orphan_count += 1
                    orphan_bytes += item.size_bytes
            except (OSError, ValueError):
                continue

    return CleanupResult(
        files_marked=len(paths),
        bytes_released=released,
        orphan_files_deleted=orphan_count,
        orphan_bytes_released=orphan_bytes,
    )
