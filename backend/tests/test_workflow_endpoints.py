from __future__ import annotations

import hashlib
from dataclasses import replace

from app import config
from app.database import SessionLocal
from app.models import Artifact
from app.storage import storage
from conftest import make_email, make_password
from test_workflow_jobs import create_version, sandbox_skill_zip


def publish_sandbox_skill(client, headers, owner_headers, *, slug: str) -> tuple[dict, dict]:
    skill, version = create_version(
        client,
        headers,
        slug=slug,
        package=sandbox_skill_zip(),
    )
    submitted = client.post(
        f"/api/v1/skills/{skill['id']}/versions/{version['id']}/submit",
        headers=headers,
    )
    assert submitted.status_code == 200, submitted.text
    approved = client.post(
        f"/api/v1/admin/reviews/{version['id']}/approve",
        headers=owner_headers,
        json={"note": "Approved for workflow Endpoint tests"},
    )
    assert approved.status_code == 200, approved.text
    return skill, approved.json()


def deploy_endpoint(client, headers, version: dict, *, slug: str) -> dict:
    response = client.post(
        "/api/v1/endpoints",
        headers=headers,
        json={
            "version_id": version["id"],
            "slug": slug,
            "name": f"{slug} API",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def register_user(client, *, email: str, display_name: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": display_name,
            "password": make_password(f"workflow-endpoint-{email}"),
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_workflow_endpoint_auth_idempotency_lifecycle_and_isolation(
    client, user_headers, owner_headers, monkeypatch
):
    monkeypatch.setattr(
        config,
        "settings",
        replace(config.settings, sandbox_worker_enabled=True),
    )
    _, version = publish_sandbox_skill(
        client,
        user_headers,
        owner_headers,
        slug="endpoint-long-review",
    )
    endpoint = deploy_endpoint(
        client,
        user_headers,
        version,
        slug="endpoint-long-review-v1",
    )
    assert endpoint["execution_mode"] == "sandbox_required"
    assert endpoint["invocation_mode"] == "async"
    endpoint_key = endpoint["api_key"]
    path = "/api/v1/workflow-endpoints/endpoint-long-review-v1/jobs"

    missing_key = client.post(
        path,
        files={"file": ("document.txt", b"document", "text/plain")},
    )
    assert missing_key.status_code == 401
    wrong_key = client.post(
        path,
        headers={"X-SkillGo-Key": "skg_wrong"},
        files={"file": ("document.txt", b"document", "text/plain")},
    )
    assert wrong_key.status_code == 401

    created = client.post(
        path,
        headers={
            "X-SkillGo-Key": endpoint_key,
            "Idempotency-Key": "customer-request-42",
        },
        data={"instruction": "重点检查日期和金额"},
        files={"file": ("document.txt", b"document content", "text/plain")},
    )
    assert created.status_code == 202, created.text
    job = created.json()
    assert job["status"] == "queued"
    assert job["trigger"] == "api"
    assert job["user_id"] == endpoint["owner_id"]
    assert created.headers["location"].endswith(job["id"])

    replayed = client.post(
        path,
        headers={
            "X-SkillGo-Key": endpoint_key,
            "Idempotency-Key": "customer-request-42",
        },
        files={"file": ("different.txt", b"different content", "text/plain")},
    )
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["id"] == job["id"]
    assert replayed.headers["x-idempotent-replay"] == "true"

    status_path = f"{path}/{job['id']}"
    status_response = client.get(
        status_path,
        headers={"X-SkillGo-Key": endpoint_key},
    )
    assert status_response.status_code == 200
    assert status_response.json()["id"] == job["id"]

    other_headers = register_user(
        client,
        email=make_email("workflow-api-other"),
        display_name="Workflow API Other",
    )
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=other_headers).status_code == 404
    _, other_version = publish_sandbox_skill(
        client,
        other_headers,
        owner_headers,
        slug="other-endpoint-review",
    )
    other_endpoint = deploy_endpoint(
        client,
        other_headers,
        other_version,
        slug="other-endpoint-review-v1",
    )
    cross_endpoint = client.get(
        f"/api/v1/workflow-endpoints/{other_endpoint['slug']}/jobs/{job['id']}",
        headers={"X-SkillGo-Key": other_endpoint["api_key"]},
    )
    assert cross_endpoint.status_code == 404

    cancelled = client.post(
        f"{status_path}/cancel",
        headers={"X-SkillGo-Key": endpoint_key},
    )
    assert cancelled.status_code == 200
    assert client.get(
        status_path,
        headers={"X-SkillGo-Key": endpoint_key},
    ).json()["status"] == "cancelled"

    artifact_data = b"verified workflow artifact"
    with SessionLocal() as db:
        artifact = Artifact(
            job_id=job["id"],
            user_id=endpoint["owner_id"],
            filename="report.txt",
            content_type="text/plain",
            size_bytes=len(artifact_data),
            sha256=hashlib.sha256(artifact_data).hexdigest(),
            storage_path="pending",
            kind="result",
            verified=True,
        )
        db.add(artifact)
        db.flush()
        artifact.storage_path = storage.put(
            f"artifacts/{endpoint['owner_id']}/{job['id']}/{artifact.id}/report.txt",
            artifact_data,
        )
        artifact_id = artifact.id
        db.commit()

    artifacts = client.get(
        f"{status_path}/artifacts",
        headers={"X-SkillGo-Key": endpoint_key},
    )
    assert artifacts.status_code == 200
    assert artifacts.json()[0]["id"] == artifact_id
    downloaded = client.get(
        f"{status_path}/artifacts/{artifact_id}/download",
        headers={"X-SkillGo-Key": endpoint_key},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == artifact_data
    assert client.get(
        f"{status_path}/artifacts/{artifact_id}/download",
        headers={"X-SkillGo-Key": other_endpoint["api_key"]},
    ).status_code == 401


def test_deleting_skill_removes_workflow_endpoint_bindings(
    client, user_headers, owner_headers, monkeypatch
):
    monkeypatch.setattr(
        config,
        "settings",
        replace(config.settings, sandbox_worker_enabled=True),
    )
    skill, version = publish_sandbox_skill(
        client,
        user_headers,
        owner_headers,
        slug="deletable-workflow-endpoint",
    )
    endpoint = deploy_endpoint(
        client,
        user_headers,
        version,
        slug="deletable-workflow-endpoint-v1",
    )
    created = client.post(
        f"/api/v1/workflow-endpoints/{endpoint['slug']}/jobs",
        headers={"X-SkillGo-Key": endpoint["api_key"]},
        files={"file": ("document.txt", b"document content", "text/plain")},
    )
    assert created.status_code == 202, created.text

    deleted = client.delete(f"/api/v1/skills/{skill['id']}", headers=user_headers)
    assert deleted.status_code == 204, deleted.text
    assert client.get("/api/v1/endpoints", headers=user_headers).json() == []
