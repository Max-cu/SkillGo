from __future__ import annotations

from datetime import timedelta

from app.database import SessionLocal
from app.models import (
    Artifact,
    JobInputFile,
    JobStatus,
    Skill,
    SkillType,
    SkillVersion,
    User,
    VersionStatus,
    Visibility,
    WorkflowJob,
    utcnow,
)
from app.storage import storage
from app.storage_lifecycle import cleanup_expired_storage


def test_storage_overview_is_visible_only_to_administrators(
    client, owner_headers, user_headers
):
    response = client.get("/api/v1/admin/storage", headers=owner_headers)
    assert response.status_code == 200
    assert response.json()["retention_days"] == 15
    assert response.json()["categories"] == {
        "conversation_attachments": 0,
        "job_inputs": 0,
        "artifacts": 0,
    }

    forbidden = client.get("/api/v1/admin/storage", headers=user_headers)
    assert forbidden.status_code == 403


def test_expired_task_files_are_purged_but_job_record_is_kept(client, owner_headers):
    del owner_headers
    old = utcnow() - timedelta(days=16)
    with SessionLocal() as db:
        owner = db.query(User).first()
        skill = Skill(
            owner_id=owner.id,
            slug="retention-test",
            name="Retention Test",
            summary="Validate managed file retention behavior.",
            visibility=Visibility.PRIVATE,
        )
        db.add(skill)
        db.flush()
        version = SkillVersion(
            skill_id=skill.id,
            created_by_id=owner.id,
            version="1.0.0",
            status=VersionStatus.PUBLISHED,
            skill_type=SkillType.INSTRUCTION,
            package_sha256="0" * 64,
            package_path="skill-packages/retention-test.zip",
            manifest={},
            skill_md="# Retention Test",
            input_schema={},
            output_schema={},
            requested_permissions={},
        )
        db.add(version)
        db.flush()
        job = WorkflowJob(
            user_id=owner.id,
            skill_id=skill.id,
            skill_version_id=version.id,
            status=JobStatus.SUCCEEDED,
            execution_mode="instruction_only",
            finished_at=old,
        )
        db.add(job)
        db.flush()
        input_path = f"job-inputs/{owner.id}/{job.id}/input.txt"
        artifact_path = f"job-artifacts/{owner.id}/{job.id}/result.txt"
        storage.put(input_path, b"input")
        storage.put(artifact_path, b"result")
        input_file = JobInputFile(
            job_id=job.id,
            user_id=owner.id,
            filename="input.txt",
            content_type="text/plain",
            size_bytes=5,
            sha256="1" * 64,
            storage_path=input_path,
            readable=True,
            extracted_text="input",
            created_at=old,
        )
        artifact = Artifact(
            job_id=job.id,
            user_id=owner.id,
            filename="result.txt",
            content_type="text/plain",
            size_bytes=6,
            sha256="2" * 64,
            storage_path=artifact_path,
            verified=True,
            created_at=old,
        )
        db.add_all([input_file, artifact])
        db.commit()
        job_id, input_id, artifact_id = job.id, input_file.id, artifact.id

    result = cleanup_expired_storage(include_orphans=False)

    assert result.files_marked == 2
    assert result.bytes_released == 11
    with SessionLocal() as db:
        assert db.get(WorkflowJob, job_id) is not None
        assert db.get(JobInputFile, input_id).purged_at is not None
        assert db.get(JobInputFile, input_id).extracted_text is None
        assert db.get(Artifact, artifact_id).purged_at is not None
    assert not storage.delete(input_path)
    assert not storage.delete(artifact_path)


def test_pinned_task_files_skip_automatic_cleanup(client, owner_headers):
    del owner_headers
    old = utcnow() - timedelta(days=16)
    with SessionLocal() as db:
        owner = db.query(User).first()
        skill = Skill(
            owner_id=owner.id,
            slug="pinned-retention-test",
            name="Pinned Retention Test",
            summary="Validate pinned task retention behavior.",
            visibility=Visibility.PRIVATE,
        )
        db.add(skill)
        db.flush()
        version = SkillVersion(
            skill_id=skill.id,
            created_by_id=owner.id,
            version="1.0.0",
            status=VersionStatus.PUBLISHED,
            skill_type=SkillType.INSTRUCTION,
            package_sha256="3" * 64,
            package_path="skill-packages/pinned-retention-test.zip",
            manifest={},
            skill_md="# Pinned Retention Test",
            input_schema={},
            output_schema={},
            requested_permissions={},
        )
        db.add(version)
        db.flush()
        job = WorkflowJob(
            user_id=owner.id,
            skill_id=skill.id,
            skill_version_id=version.id,
            status=JobStatus.SUCCEEDED,
            execution_mode="instruction_only",
            finished_at=old,
            storage_pinned=True,
        )
        db.add(job)
        db.flush()
        path = f"job-artifacts/{owner.id}/{job.id}/kept.txt"
        storage.put(path, b"kept")
        artifact = Artifact(
            job_id=job.id,
            user_id=owner.id,
            filename="kept.txt",
            content_type="text/plain",
            size_bytes=4,
            sha256="4" * 64,
            storage_path=path,
            verified=True,
            created_at=old,
        )
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id

    result = cleanup_expired_storage(include_orphans=False)

    assert result.files_marked == 0
    with SessionLocal() as db:
        assert db.get(Artifact, artifact_id).purged_at is None
    assert storage.read(path) == b"kept"
    storage.delete(path)
