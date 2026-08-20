import json

from test_skill_flow import skill_zip
from test_workflow_jobs import create_version


def test_general_workspace_message_does_not_create_or_route_a_skill_job(
    client, user_headers, fake_model_gateway
):
    created = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    )
    assert created.status_code == 201, created.text
    conversation = created.json()
    assert conversation["title"] == "会话 1"

    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "你好", "model_name": "test-fast-model"},
    )

    assert response.status_code == 200, response.text
    detail = response.json()
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert [item["kind"] for item in detail["messages"]] == ["text", "text"]
    assert detail["messages"][0]["content"]["message"] == "你好"
    assert detail["messages"][1]["content"]["message"].startswith("General reply:")
    assert detail["messages"][1]["content"]["latency_ms"] == 18
    assert detail["messages"][1]["model_name"] == "test-fast-model"
    assert all(item["job"] is None for item in detail["messages"])
    assert client.get("/api/v1/jobs", headers=user_headers).json() == []
    assert fake_model_gateway.chat_messages
    assert not fake_model_gateway.routed_skills


def test_general_workspace_message_streams_and_persists_the_completed_turn(
    client, user_headers, fake_model_gateway
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()

    with client.stream(
        "POST",
        f"/api/v1/agent/conversations/{conversation['id']}/messages/stream",
        headers=user_headers,
        data={"message": "你好", "model_name": "test-fast-model"},
    ) as response:
        assert response.status_code == 200, response.text
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert [event["type"] for event in events] == ["delta", "delta", "done", "persisted"]
    assert "".join(event["text"] for event in events if event["type"] == "delta").startswith(
        "General reply:"
    )
    detail = client.get(
        f"/api/v1/agent/conversations/{conversation['id']}", headers=user_headers
    ).json()
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"]["latency_ms"] == 18
    assert detail["messages"][1]["model_name"] == "test-fast-model"


def test_general_reply_treats_completed_skill_job_as_verified_platform_history(
    client, user_headers, fake_model_gateway
):
    skill, version = create_version(
        client, user_headers, slug="verified-history-review", package=skill_zip()
    )
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={
            "version_id": version["id"],
            "instruction": "校审并生成交付文件",
            "agent_conversation_id": conversation["id"],
        },
        files={"file": ("fil.txt", "需要校审的内容".encode(), "text/plain")},
    )
    assert created.status_code == 201, created.text
    job = client.get(
        f"/api/v1/jobs/{created.json()['id']}", headers=user_headers
    ).json()
    assert job["status"] == "succeeded"
    assert job["artifacts"]

    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "做得真棒"},
    )
    assert response.status_code == 200, response.text

    model_messages = fake_model_gateway.chat_messages[-1]
    assert "权威执行记录" in model_messages[0]["content"]
    workflow_history = next(
        message["content"]
        for message in model_messages
        if message["content"].startswith("[SkillGo 已验证的 Skill 执行记录]")
    )
    assert f"任务：{job['id']}" in workflow_history
    assert f"Skill：{skill['name']}" in workflow_history
    assert "状态：已完成" in workflow_history
    assert "执行结果：" in workflow_history
    assert job["artifacts"][0]["filename"] in workflow_history


def test_workspace_conversations_are_isolated_by_user(
    client, user_headers, owner_headers
):
    created = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()

    assert client.get(
        f"/api/v1/agent/conversations/{created['id']}", headers=owner_headers
    ).status_code == 404
    assert client.get(
        "/api/v1/agent/conversations", headers=owner_headers
    ).json() == []


def test_workspace_message_accepts_multiple_files_and_reuses_them(
    client, user_headers, fake_model_gateway
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "比较这两份材料"},
        files=[
            ("files", ("one.txt", b"first", "text/plain")),
            ("files", ("two.txt", b"second", "text/plain")),
        ],
    )
    assert response.status_code == 200, response.text
    stored_files = response.json()["messages"][0]["files"]
    assert [item["filename"] for item in stored_files] == ["one.txt", "two.txt"]

    reused = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={
            "message": "继续比较第一份",
            "existing_file_ids": json.dumps([stored_files[0]["id"]]),
        },
    )
    assert reused.status_code == 200, reused.text
    assert reused.json()["messages"][-2]["files"][0]["sha256"] == stored_files[0]["sha256"]
