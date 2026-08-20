from __future__ import annotations

from conftest import make_email, make_password
from test_skill_flow import skill_zip


def publish_skill(client, user_headers, owner_headers):
    skill = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "runtime-summary",
            "name": "Runtime Summary",
            "summary": "Run a private model through an approved Skill version.",
            "visibility": "public",
        },
    ).json()
    uploaded = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={"package": ("runtime.zip", skill_zip(), "application/zip")},
    ).json()
    client.post(
        f"/api/v1/skills/{skill['id']}/versions/{uploaded['id']}/submit",
        headers=user_headers,
    )
    approved = client.post(
        f"/api/v1/admin/reviews/{uploaded['id']}/approve",
        headers=owner_headers,
        json={"note": "Approved for runtime test"},
    )
    assert approved.status_code == 200, approved.text
    return skill, uploaded


def test_console_run_and_endpoint_invocation(
    client, user_headers, owner_headers, fake_model_gateway
):
    skill, version = publish_skill(client, user_headers, owner_headers)

    invalid = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={"version_id": version["id"], "input": {}},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "INPUT_SCHEMA_MISMATCH"

    executed = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={"version_id": version["id"], "input": {"content": "hello"}},
    )
    assert executed.status_code == 201, executed.text
    assert executed.json()["status"] == "succeeded"
    assert executed.json()["output"] == {"summary": "Summary: hello"}
    assert executed.json()["model_name"] == "test-private-model"

    deployed = client.post(
        "/api/v1/endpoints",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "slug": "runtime-summary-v1",
            "name": "Runtime Summary API",
        },
    )
    assert deployed.status_code == 201, deployed.text
    endpoint = deployed.json()
    api_key = endpoint["api_key"]
    assert api_key.startswith("skg_")
    assert "api_key_hash" not in endpoint

    denied = client.post(
        "/api/v1/invoke/runtime-summary-v1",
        json={"input": {"content": "api"}},
    )
    assert denied.status_code == 401

    invoked = client.post(
        "/api/v1/invoke/runtime-summary-v1",
        headers={"X-SkillGo-Key": api_key},
        json={"input": {"content": "api"}},
    )
    assert invoked.status_code == 200, invoked.text
    assert invoked.json()["output"] == {"summary": "Summary: api"}

    rotated = client.post(
        f"/api/v1/endpoints/{endpoint['id']}/rotate-key", headers=user_headers
    )
    assert rotated.status_code == 200
    new_key = rotated.json()["api_key"]
    assert new_key != api_key

    old_key = client.post(
        "/api/v1/invoke/runtime-summary-v1",
        headers={"X-SkillGo-Key": api_key},
        json={"input": {"content": "old"}},
    )
    assert old_key.status_code == 401

    runs = client.get("/api/v1/runs", headers=user_headers)
    assert runs.status_code == 200
    assert {item["invocation_type"] for item in runs.json()} == {"console", "api"}
    assert all(item["skill_id"] == skill["id"] for item in runs.json())


def test_console_run_uses_selected_model(
    client, user_headers, owner_headers, fake_model_gateway
):
    _, version = publish_skill(client, user_headers, owner_headers)
    response = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "input": {"content": "use the fast model"},
            "model_name": "test-fast-model",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["model_name"] == "test-fast-model"
    assert fake_model_gateway.requested_models[-1] == "test-fast-model"


def test_model_status_does_not_expose_key(client, owner_headers):
    response = client.get("/api/v1/super-admin/model/status", headers=owner_headers)
    assert response.status_code == 200
    assert "api_key" not in response.json()


def test_delete_skill_removes_runs_and_endpoints(
    client, user_headers, owner_headers, fake_model_gateway
):
    skill, version = publish_skill(client, user_headers, owner_headers)
    executed = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={"version_id": version["id"], "input": {"content": "temporary"}},
    )
    assert executed.status_code == 201, executed.text
    deployed = client.post(
        "/api/v1/endpoints",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "slug": "delete-runtime-v1",
            "name": "Temporary Runtime API",
        },
    )
    assert deployed.status_code == 201, deployed.text

    deleted = client.delete(f"/api/v1/skills/{skill['id']}", headers=user_headers)
    assert deleted.status_code == 204, deleted.text
    assert client.get("/api/v1/runs", headers=user_headers).json() == []
    assert client.get("/api/v1/endpoints", headers=user_headers).json() == []
    invocation = client.post(
        "/api/v1/invoke/delete-runtime-v1",
        headers={"X-SkillGo-Key": deployed.json()["api_key"]},
        json={"input": {"content": "after deletion"}},
    )
    assert invocation.status_code == 404


def test_conversation_context_is_isolated_and_clearable(
    client, user_headers, owner_headers, fake_model_gateway
):
    skill, version = publish_skill(client, user_headers, owner_headers)
    created = client.post(
        "/api/v1/conversations",
        headers=user_headers,
        json={"version_id": version["id"]},
    )
    assert created.status_code == 201, created.text
    conversation = created.json()
    assert conversation["title"] == "会话 1"
    assert conversation["message_count"] == 0

    second_conversation = client.post(
        "/api/v1/conversations",
        headers=user_headers,
        json={"version_id": version["id"]},
    )
    assert second_conversation.status_code == 201, second_conversation.text
    assert second_conversation.json()["title"] == "会话 2"

    renamed = client.patch(
        f"/api/v1/conversations/{conversation['id']}",
        headers=user_headers,
        json={"title": "项目周报"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "项目周报"
    assert client.patch(
        f"/api/v1/conversations/{conversation['id']}",
        headers=user_headers,
        json={"title": "   "},
    ).status_code == 422

    first = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "conversation_id": conversation["id"],
            "input": {"content": "first turn"},
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["context_message_count"] == 0
    assert fake_model_gateway.histories[-1] == []
    second = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "conversation_id": conversation["id"],
            "input": {"content": "second turn"},
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["context_message_count"] == 2
    assert fake_model_gateway.histories[-1] == [
        {"role": "user", "content": {"content": "first turn"}},
        {"role": "assistant", "content": {"summary": "Summary: first turn"}},
    ]

    detail = client.get(
        f"/api/v1/conversations/{conversation['id']}", headers=user_headers
    )
    assert detail.status_code == 200
    assert detail.json()["message_count"] == 4
    assert [item["role"] for item in detail.json()["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("context-other"),
            "display_name": "Context Other",
            "password": make_password("context-other"),
        },
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert client.get(
        f"/api/v1/conversations/{conversation['id']}", headers=other_headers
    ).status_code == 404
    assert client.patch(
        f"/api/v1/conversations/{conversation['id']}",
        headers=other_headers,
        json={"title": "Cannot rename"},
    ).status_code == 404

    cleared = client.delete(
        f"/api/v1/conversations/{conversation['id']}/messages", headers=user_headers
    )
    assert cleared.status_code == 200
    assert client.get(
        f"/api/v1/conversations/{conversation['id']}", headers=user_headers
    ).json()["message_count"] == 0

    third = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "conversation_id": conversation["id"],
            "input": {"content": "fresh turn"},
        },
    )
    assert third.status_code == 201, third.text
    assert fake_model_gateway.histories[-1] == []


def test_console_chat_accepts_natural_language_and_persists_history(
    client, user_headers, owner_headers, fake_model_gateway
):
    _, version = publish_skill(client, user_headers, owner_headers)
    created = client.post(
        "/api/v1/conversations",
        headers=user_headers,
        json={"version_id": version["id"]},
    )
    assert created.status_code == 201, created.text
    conversation = created.json()

    first = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "conversation_id": conversation["id"],
            "message": "请用一句话总结今天的进展",
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["input"] == {"message": "请用一句话总结今天的进展"}
    assert first.json()["output"] == {"summary": "Summary: 请用一句话总结今天的进展"}
    assert fake_model_gateway.chat_modes[-1] is True
    assert fake_model_gateway.histories[-1] == []

    second = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "conversation_id": conversation["id"],
            "message": "再精简一点",
        },
    )
    assert second.status_code == 201, second.text
    assert fake_model_gateway.histories[-1] == [
        {"role": "user", "content": {"message": "请用一句话总结今天的进展"}},
        {"role": "assistant", "content": {"summary": "Summary: 请用一句话总结今天的进展"}},
    ]

    detail = client.get(
        f"/api/v1/conversations/{conversation['id']}", headers=user_headers
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["message_count"] == 4
    assert detail.json()["messages"][0]["content"] == {
        "message": "请用一句话总结今天的进展"
    }

    both_modes = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "input": {"content": "structured"},
            "message": "chat",
        },
    )
    assert both_modes.status_code == 422


def test_conversation_workspace_files_are_isolated_and_added_to_model_context(
    client, user_headers, owner_headers, fake_model_gateway
):
    _, version = publish_skill(client, user_headers, owner_headers)
    created = client.post(
        "/api/v1/conversations",
        headers=user_headers,
        json={"version_id": version["id"]},
    )
    conversation = created.json()

    uploaded = client.post(
        f"/api/v1/conversations/{conversation['id']}/files",
        headers=user_headers,
        files={"file": ("notes.txt", "项目状态：文件工作区已经完成。".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    workspace_file = uploaded.json()
    assert workspace_file["filename"] == "notes.txt"
    assert workspace_file["readable"] is True
    assert workspace_file["source"] == "upload"

    blocked = client.post(
        f"/api/v1/conversations/{conversation['id']}/files",
        headers=user_headers,
        files={"file": ("danger.ps1", b"Remove-Item C:\\\\*", "text/plain")},
    )
    assert blocked.status_code == 422

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("workspace-other"),
            "display_name": "Workspace Other",
            "password": make_password("workspace-other"),
        },
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert client.get(
        f"/api/v1/conversations/{conversation['id']}/files",
        headers=other_headers,
    ).status_code == 404
    assert client.get(
        f"/api/v1/conversations/{conversation['id']}/files/{workspace_file['id']}/download",
        headers=other_headers,
    ).status_code == 404

    run = client.post(
        "/api/v1/runs",
        headers=user_headers,
        json={
            "version_id": version["id"],
            "conversation_id": conversation["id"],
            "message": "请读取 notes.txt 并总结",
        },
    )
    assert run.status_code == 201, run.text
    assert fake_model_gateway.workspace_files[-1] == [
        {"filename": "notes.txt", "content": "项目状态：文件工作区已经完成。"}
    ]

    artifact = client.post(
        f"/api/v1/conversations/{conversation['id']}/artifacts",
        headers=user_headers,
        json={"filename": "answer.txt", "content": "这是可下载的 Skill 产物。"},
    )
    assert artifact.status_code == 201, artifact.text
    artifact_file = artifact.json()
    assert artifact_file["source"] == "generated"
    downloaded = client.get(
        f"/api/v1/conversations/{conversation['id']}/files/{artifact_file['id']}/download",
        headers=user_headers,
    )
    assert downloaded.status_code == 200
    assert downloaded.content.decode() == "这是可下载的 Skill 产物。"

    deleted = client.delete(
        f"/api/v1/conversations/{conversation['id']}/files/{workspace_file['id']}",
        headers=user_headers,
    )
    assert deleted.status_code == 200
    remaining = client.get(
        f"/api/v1/conversations/{conversation['id']}/files",
        headers=user_headers,
    ).json()
    assert [item["id"] for item in remaining] == [artifact_file["id"]]
