"""Run a cloud smoke test against the public sandbox workflow API.

This helper is intended to run inside the SkillGo API container.  It rotates
the dedicated smoke Endpoint key in the database, uses the plaintext key only
in memory, and never prints it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select


sys.path.insert(0, "/app")

from app.database import SessionLocal  # noqa: E402
from app.models import Endpoint, SkillVersion, VersionStatus, WorkflowJob  # noqa: E402
from app.runtime_profile import version_runtime_profile  # noqa: E402
from app.security import generate_endpoint_key  # noqa: E402


def endpoint_with_fresh_key(
    db, *, version: SkillVersion, owner_id: str, slug: str
) -> tuple[Endpoint, str]:
    profile = version_runtime_profile(version)
    if version.status != VersionStatus.PUBLISHED:
        raise RuntimeError("The source job version is not published")
    if profile.get("execution_mode") != "sandbox_required" or not profile.get("runnable"):
        raise RuntimeError(f"The source version is not a runnable sandbox workflow: {profile}")
    endpoint = db.scalar(select(Endpoint).where(Endpoint.slug == slug))
    api_key, prefix, key_hash = generate_endpoint_key()
    if endpoint is None:
        endpoint = Endpoint(
            owner_id=owner_id,
            skill_id=version.skill_id,
            skill_version_id=version.id,
            slug=slug,
            name=f"{version.skill.name} API smoke",
            is_active=True,
            api_key_prefix=prefix,
            api_key_hash=key_hash,
        )
        db.add(endpoint)
    else:
        endpoint.owner_id = owner_id
        endpoint.skill_id = version.skill_id
        endpoint.skill_version_id = version.id
        endpoint.name = f"{version.skill.name} API smoke"
        endpoint.is_active = True
        endpoint.api_key_prefix = prefix
        endpoint.api_key_hash = key_hash
    db.commit()
    db.refresh(endpoint)
    return endpoint, api_key


def source_job_by_prefix(db, prefix: str) -> WorkflowJob:
    source_job = db.scalar(
        select(WorkflowJob)
        .where(WorkflowJob.id.like(f"{prefix}%"))
        .order_by(WorkflowJob.created_at.desc())
    )
    if source_job is None:
        raise RuntimeError(f"No source workflow job starts with {prefix!r}")
    return source_job


def selected_version_and_owner(db, args: argparse.Namespace) -> tuple[SkillVersion, str]:
    if args.version_id:
        version = db.get(SkillVersion, args.version_id)
        if version is None:
            raise RuntimeError("Selected Skill version was not found")
        return version, version.skill.owner_id
    source_job = source_job_by_prefix(db, args.source_job_prefix)
    return source_job.skill_version, source_job.user_id


def create_job(args: argparse.Namespace) -> None:
    input_path = Path(args.input).resolve()
    data = input_path.read_bytes()
    with SessionLocal() as db:
        version, owner_id = selected_version_and_owner(db, args)
        endpoint, api_key = endpoint_with_fresh_key(
            db, version=version, owner_id=owner_id, slug=args.slug
        )
        endpoint_url = f"{args.base_url}/api/v1/workflow-endpoints/{endpoint.slug}"
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{endpoint_url}/jobs",
                headers={
                    "X-SkillGo-Key": api_key,
                    "Idempotency-Key": f"smoke-{uuid.uuid4()}",
                },
                data={"instruction": args.instruction},
                files={
                    "file": (
                        input_path.name,
                        data,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
        response.raise_for_status()
        job = response.json()
        print(
            json.dumps(
                {
                    "endpoint_slug": endpoint.slug,
                    "job_id": job["id"],
                    "status": job["status"],
                    "location": response.headers.get("Location"),
                },
                ensure_ascii=False,
            )
        )


def verify_job(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        job = db.get(WorkflowJob, args.job_id)
        if job is None:
            raise RuntimeError("Workflow job not found")
        endpoint, api_key = endpoint_with_fresh_key(
            db,
            version=job.skill_version,
            owner_id=job.user_id,
            slug=args.slug,
        )
        endpoint_url = f"{args.base_url}/api/v1/workflow-endpoints/{endpoint.slug}"
        headers = {"X-SkillGo-Key": api_key}
        with httpx.Client(timeout=120) as client:
            status_response = client.get(f"{endpoint_url}/jobs/{job.id}", headers=headers)
            status_response.raise_for_status()
            payload = status_response.json()
            result = {
                "job_id": payload["id"],
                "status": payload["status"],
                "error_code": payload.get("error_code"),
                "error_message": payload.get("error_message"),
                "steps": [
                    {"key": step["step_key"], "status": step["status"], "detail": step["detail"]}
                    for step in payload["steps"]
                ],
                "artifacts": [],
            }
            if payload["status"] == "succeeded":
                artifacts_response = client.get(
                    f"{endpoint_url}/jobs/{job.id}/artifacts", headers=headers
                )
                artifacts_response.raise_for_status()
                for artifact in artifacts_response.json():
                    download = client.get(
                        f"{endpoint_url}/jobs/{job.id}/artifacts/{artifact['id']}/download",
                        headers=headers,
                    )
                    download.raise_for_status()
                    digest = hashlib.sha256(download.content).hexdigest()
                    result["artifacts"].append(
                        {
                            "filename": artifact["filename"],
                            "size_bytes": len(download.content),
                            "verified": artifact["verified"],
                            "sha256_matches": digest == artifact["sha256"],
                        }
                    )
            print(json.dumps(result, ensure_ascii=False))


def list_sandbox_versions() -> None:
    with SessionLocal() as db:
        versions = []
        for version in db.scalars(select(SkillVersion)).all():
            profile = version_runtime_profile(version)
            if profile.get("execution_mode") == "sandbox_required":
                versions.append(
                    {
                        "id": version.id,
                        "status": version.status.value,
                        "skill_name": version.skill.name,
                        "runnable": bool(profile.get("runnable")),
                        "created_at": version.created_at.isoformat(),
                        "scripts": profile.get("requirements", {}).get("scripts", []),
                    }
                )
        print(json.dumps(versions, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("create", "verify", "list"))
    parser.add_argument("--base-url", default="http://web:8080")
    parser.add_argument("--slug", default="long-doc-review-workflow-smoke")
    parser.add_argument("--source-job-prefix", default="c2b8a759")
    parser.add_argument("--version-id")
    parser.add_argument("--input", default="/tmp/skillgo-test.docx")
    parser.add_argument("--instruction", default="完整审查这个测试文档并生成最终文件")
    parser.add_argument("--job-id")
    args = parser.parse_args()
    if args.mode == "verify" and not args.job_id:
        parser.error("--job-id is required in verify mode")
    return args


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.mode == "create":
        create_job(parsed)
    elif parsed.mode == "verify":
        verify_job(parsed)
    else:
        list_sandbox_versions()
