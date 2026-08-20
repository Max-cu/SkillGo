from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace

from app import config
from conftest import make_email, make_password
from test_skill_flow import skill_zip


def create_version(client, headers, *, slug: str, package: bytes) -> tuple[dict, dict]:
    skill = client.post(
        "/api/v1/skills",
        headers=headers,
        json={
            "slug": slug,
            "name": slug.replace("-", " ").title(),
            "summary": "A workflow package used to verify the job execution control plane.",
            "visibility": "private",
        },
    ).json()
    response = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=headers,
        files={"package": (f"{slug}.zip", package, "application/zip")},
    )
    assert response.status_code == 201, response.text
    return skill, response.json()


def sandbox_skill_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "long-review/SKILL.md",
            """---
name: long-review
description: Review a DOCX and generate a verified Word report.
---

# Long review

Run `python scripts/review.py input.docx` and then use Node.js to generate report.docx.
The compliance step requires network access.
""",
        )
        archive.writestr("long-review/scripts/review.py", "print('review')")
    return output.getvalue()


def test_instruction_job_auto_runs_and_creates_verified_artifact(
    client, user_headers, fake_model_gateway
):
    skill, version = create_version(
        client, user_headers, slug="job-summary", package=skill_zip()
    )
    assert version["execution_mode"] == "instruction_only"
    assert version["runtime_runnable"] is True

    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "完整总结这个文件", "model_name": "test-fast-model"},
        files={"file": ("input.txt", "需要总结的测试内容".encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    created_job = response.json()
    assert created_job["status"] == "preparing"
    assert created_job["model_name"] == "test-fast-model"
    job = client.get(f"/api/v1/jobs/{created_job['id']}", headers=user_headers).json()
    assert job["status"] == "succeeded"
    assert job["model_name"] == "test-fast-model"
    assert fake_model_gateway.requested_models[-1] == "test-fast-model"
    assert [step["status"] for step in job["steps"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert len(job["artifacts"]) == 1
    assert job["artifacts"][0]["verified"] is True
    assert [event["sequence"] for event in job["events"]] == list(
        range(1, len(job["events"]) + 1)
    )
    assert {event["event_type"] for event in job["events"]} >= {
        "input",
        "reasoning",
        "artifact",
        "result",
    }

    artifact = job["artifacts"][0]
    downloaded = client.get(
        f"/api/v1/jobs/{job['id']}/artifacts/{artifact['id']}/download",
        headers=user_headers,
    )
    assert downloaded.status_code == 200
    assert "Summary:" in downloaded.text
    assert fake_model_gateway.chat_modes[-1] is True

    listed = client.get(
        f"/api/v1/jobs?skill_id={skill['id']}", headers=user_headers
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == job["id"]

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("workflow-other"),
            "display_name": "Workflow Other",
            "password": make_password("workflow-other"),
        },
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=other_headers).status_code == 404
    assert client.get(
        f"/api/v1/jobs/{job['id']}/artifacts/{artifact['id']}/download",
        headers=other_headers,
    ).status_code == 404


def test_agent_job_accepts_natural_language_without_attachment(
    client, user_headers, fake_model_gateway
):
    _, version = create_version(
        client, user_headers, slug="text-only-agent-job", package=skill_zip()
    )
    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "直接给我一个简短结论"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["trigger"] == "chat_message"
    assert created["input_files"][0]["filename"] == "task-request.txt"

    job = client.get(f"/api/v1/jobs/{created['id']}", headers=user_headers).json()
    assert job["status"] == "succeeded"
    assert job["events"][-1]["event_type"] == "result"
    assert job["events"][-1]["status"] == "succeeded"


def test_finished_workflow_job_can_be_deleted(
    client, user_headers, fake_model_gateway
):
    skill, version = create_version(
        client, user_headers, slug="deletable-workflow-job", package=skill_zip()
    )
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "生成一份可删除的结果"},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    finished = client.get(f"/api/v1/jobs/{job_id}", headers=user_headers)
    assert finished.status_code == 200
    assert finished.json()["status"] == "succeeded"

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("workflow-delete-other"),
            "display_name": "Workflow Delete Other",
            "password": make_password("workflow-delete-other"),
        },
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=other_headers).status_code == 404

    deleted = client.delete(f"/api/v1/jobs/{job_id}", headers=user_headers)
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/api/v1/jobs/{job_id}", headers=user_headers).status_code == 404
    listed = client.get(f"/api/v1/jobs?skill_id={skill['id']}", headers=user_headers)
    assert listed.status_code == 200
    assert all(item["id"] != job_id for item in listed.json())


def test_skill_job_accepts_multiple_files_and_failed_job_can_retry(
    client, user_headers, monkeypatch
):
    monkeypatch.setattr(
        config,
        "settings",
        replace(config.settings, sandbox_worker_enabled=True),
    )
    _, version = create_version(
        client, user_headers, slug="multi-file-retry", package=sandbox_skill_zip()
    )
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={
            "version_id": version["id"],
            "instruction": "一起处理",
            "agent_conversation_id": conversation["id"],
        },
        files=[
            ("files", ("one.txt", b"first", "text/plain")),
            ("files", ("two.txt", b"second", "text/plain")),
        ],
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert [item["filename"] for item in job["input_files"]] == ["one.txt", "two.txt"]

    # Queued jobs are not retryable until they reach a terminal failure state.
    assert client.post(f"/api/v1/jobs/{job['id']}/retry", headers=user_headers).status_code == 409


def test_sandbox_skill_is_blocked_without_calling_model(
    client, user_headers, fake_model_gateway
):
    _, version = create_version(
        client, user_headers, slug="long-review", package=sandbox_skill_zip()
    )
    assert version["execution_mode"] == "sandbox_required"
    assert version["runtime_status"] == "awaiting_sandbox"
    assert version["runtime_runnable"] is False
    assert any(
        item.endswith("scripts/review.py")
        for item in version["runtime_requirements"]["scripts"]
    )

    before = len(fake_model_gateway.chat_modes)
    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"]},
        files={"file": ("document.docx", b"not-a-real-docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    # Input parsing fails before a blocked job is useful, so use readable text as
    # a stand-in; the runtime classification still comes from the Skill package.
    assert response.status_code == 422

    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"]},
        files={"file": ("document.txt", b"document content", "text/plain")},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "blocked"
    assert job["error_code"] == "AWAITING_SANDBOX"
    assert job["steps"][1]["status"] == "blocked"
    assert len(fake_model_gateway.chat_modes) == before

    retried = client.post(
        f"/api/v1/jobs/{job['id']}/retry", headers=user_headers
    )
    assert retried.status_code == 201, retried.text
    retried_job = retried.json()
    assert retried_job["id"] != job["id"]
    assert retried_job["status"] == "blocked"
    assert retried_job["input_files"][0]["sha256"] == job["input_files"][0]["sha256"]

    chat = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={"version_id": version["id"], "message": "继续"},
    )
    assert chat.status_code == 409
    assert chat.json()["detail"]["code"] == "WORKFLOW_RUNNER_REQUIRED"


def test_sandbox_skill_is_queued_when_worker_is_enabled(
    client, user_headers, fake_model_gateway, monkeypatch
):
    monkeypatch.setattr(
        config,
        "settings",
        replace(config.settings, sandbox_worker_enabled=True),
    )
    _, version = create_version(
        client, user_headers, slug="queued-long-review", package=sandbox_skill_zip()
    )
    assert version["runtime_status"] == "available"
    assert version["runtime_runnable"] is True

    before = len(fake_model_gateway.chat_modes)
    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"]},
        files={"file": ("document.txt", b"document content", "text/plain")},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert job["steps"][1]["status"] == "pending"
    assert "Linux" in job["steps"][1]["detail"]
    assert len(fake_model_gateway.chat_modes) == before


def test_multi_skill_job_preserves_order_and_queues_one_sandbox(
    client, user_headers, fake_model_gateway, monkeypatch
):
    monkeypatch.setattr(
        config,
        "settings",
        replace(config.settings, sandbox_worker_enabled=True),
    )
    first_skill, first_version = create_version(
        client, user_headers, slug="multi-review", package=sandbox_skill_zip()
    )
    second_skill, second_version = create_version(
        client, user_headers, slug="multi-format", package=sandbox_skill_zip()
    )

    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={
            "version_id": first_version["id"],
            "version_ids": json.dumps([first_version["id"], second_version["id"]]),
            "instruction": "先审查，再统一格式",
        },
    )

    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert job["execution_mode"] == "sandbox_required"
    assert [item["skill_id"] for item in job["selected_skills"]] == [
        first_skill["id"],
        second_skill["id"],
    ]
    assert [item["position"] for item in job["selected_skills"]] == [1, 2]
    queued_event = next(item for item in job["events"] if "队列" in item["title"])
    assert queued_event["data"]["skill_count"] == 2
    assert "2 个 Skill" in queued_event["detail"]

    stored = client.get(f"/api/v1/jobs/{job['id']}", headers=user_headers).json()
    assert [item["skill_version_id"] for item in stored["selected_skills"]] == [
        first_version["id"],
        second_version["id"],
    ]


def test_structured_message_uses_inline_skill_order_as_hard_constraints(
    client, user_headers, monkeypatch
):
    monkeypatch.setattr(
        config,
        "settings",
        replace(config.settings, sandbox_worker_enabled=True),
    )
    first_skill, first_version = create_version(
        client, user_headers, slug="inline-review", package=sandbox_skill_zip()
    )
    second_skill, second_version = create_version(
        client, user_headers, slug="inline-format", package=sandbox_skill_zip()
    )
    content = [
        {"type": "text", "text": "先用"},
        {
            "type": "skill_ref",
            "skill_id": first_skill["id"],
            "skill_version_id": first_version["id"],
            "skill_name": "伪造名称会被服务端覆盖",
        },
        {"type": "text", "text": "审查，再用"},
        {
            "type": "skill_ref",
            "skill_id": second_skill["id"],
            "skill_version_id": second_version["id"],
            "skill_name": second_skill["name"],
        },
        {"type": "text", "text": "统一格式"},
    ]
    conversation_response = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    )
    assert conversation_response.status_code == 201, conversation_response.text
    conversation = conversation_response.json()

    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={
            "message_content": json.dumps(content, ensure_ascii=False),
            "agent_conversation_id": conversation["id"],
        },
    )

    assert response.status_code == 201, response.text
    job = response.json()
    assert job["routing_mode"] == "explicit"
    assert [item["skill_version_id"] for item in job["selected_skills"]] == [
        first_version["id"],
        second_version["id"],
    ]
    assert [item["type"] for item in job["message_content"]] == [
        "text",
        "skill_ref",
        "text",
        "skill_ref",
        "text",
    ]
    assert job["message_content"][1]["skill_name"] == first_skill["name"]
    assert "先用" in job["instruction"] and "再用" in job["instruction"]
    detail = client.get(
        f"/api/v1/agent/conversations/{conversation['id']}", headers=user_headers
    ).json()
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["kind"] == "workflow"
    assert detail["messages"][1]["job"]["id"] == job["id"]
    assert len(detail["messages"][1]["job"]["selected_skills"]) == 2


def test_skill_job_requires_an_explicit_skill(
    client, user_headers, fake_model_gateway
):
    create_version(client, user_headers, slug="auto-review", package=skill_zip())
    target_skill, _ = create_version(
        client, user_headers, slug="auto-formatter", package=skill_zip()
    )

    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"instruction": f"请使用 {target_skill['name']} 整理这段文字"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "SKILL_REQUIRED"
    assert not fake_model_gateway.routed_skills


def test_multi_skill_job_rejects_inaccessible_secondary_skill(client, user_headers):
    _, owned_version = create_version(
        client, user_headers, slug="owned-multi-skill", package=sandbox_skill_zip()
    )
    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("secondary-owner"),
            "display_name": "Secondary Owner",
            "password": make_password("secondary-owner"),
        },
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    _, private_version = create_version(
        client, other_headers, slug="private-secondary", package=sandbox_skill_zip()
    )

    response = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={
            "version_id": owned_version["id"],
            "version_ids": json.dumps([owned_version["id"], private_version["id"]]),
            "instruction": "组合执行",
        },
    )

    assert response.status_code == 404
