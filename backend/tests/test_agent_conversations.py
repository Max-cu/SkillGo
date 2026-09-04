import json
import base64
from dataclasses import replace
from types import SimpleNamespace

from app.routers import agent as agent_router

from test_skill_flow import skill_zip
from test_workflow_jobs import create_version


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_workspace_model_history_honors_message_and_character_limits(monkeypatch):
    monkeypatch.setattr(
        agent_router,
        "settings",
        replace(
            agent_router.settings,
            context_max_messages=3,
            context_max_chars=30,
        ),
    )
    messages = [
        SimpleNamespace(
            kind="text",
            role="user",
            content={"message": f"message-{index}"},
            files=[],
        )
        for index in range(5)
    ]

    history = agent_router._model_history(SimpleNamespace(messages=messages))

    assert [item["content"] for item in history] == ["message-3", "message-4"]
    assert sum(len(item["role"]) + len(item["content"]) for item in history) <= 30


def test_workspace_model_history_truncates_one_oversized_latest_message(monkeypatch):
    monkeypatch.setattr(
        agent_router,
        "settings",
        replace(
            agent_router.settings,
            context_max_messages=20,
            context_max_chars=40,
        ),
    )
    conversation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                kind="text",
                role="user",
                content={"message": "x" * 400},
                files=[],
            )
        ]
    )

    history = agent_router._model_history(conversation)

    assert len(history) == 1
    assert "上下文已截断" in history[0]["content"]
    assert len(history[0]["role"]) + len(history[0]["content"]) <= 40


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


def test_workspace_image_uses_ocr_then_vision_when_enabled(
    client, user_headers, fake_model_gateway
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()

    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "这张图说明了什么？", "ocr_enabled": "true"},
        files={"file": ("figure.png", ONE_PIXEL_PNG, "image/png")},
    )

    assert response.status_code == 200, response.text
    stored = response.json()["messages"][0]["files"][0]
    assert stored["analysis_mode"] == "vision_with_ocr"
    assert stored["analysis_status"] == "ready"
    assert stored["analysis_model"] == "test-vision-model"
    assert stored["ocr_model"] == "test-ocr-model"
    assert fake_model_gateway.selected_capabilities[-2:] == ["ocr", "vision"]
    assert [item["purpose"] for item in fake_model_gateway.attachment_analyses[-2:]] == [
        "ocr",
        "vision",
    ]
    assert "OCR extracted text" in fake_model_gateway.attachment_analyses[-1]["prompt"]
    assert "Vision understood image" in fake_model_gateway.chat_messages[-1][-1]["content"]
    assert "OCR extracted text" in fake_model_gateway.chat_messages[-1][-1]["content"]


def test_workspace_image_uses_vision_only_by_default(
    client, user_headers, fake_model_gateway
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "描述图片"},
        files={"file": ("photo.jpg", b"\xff\xd8\xff" + b"image", "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    stored = response.json()["messages"][0]["files"][0]
    assert stored["analysis_mode"] == "vision"
    assert stored["analysis_model"] == "test-vision-model"
    assert stored["ocr_model"] is None
    assert fake_model_gateway.selected_capabilities == ["vision"]
    assert [item["purpose"] for item in fake_model_gateway.attachment_analyses] == [
        "vision"
    ]


def test_workspace_pdf_uses_ocr_without_vision_when_enabled(
    client, user_headers, fake_model_gateway
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "识别这份扫描件", "ocr_enabled": "true"},
        files={"file": ("scan.pdf", b"%PDF-1.4\nscan", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    stored = response.json()["messages"][0]["files"][0]
    assert stored["analysis_mode"] == "ocr"
    assert stored["analysis_status"] == "ready"
    assert stored["analysis_model"] is None
    assert stored["ocr_model"] == "test-ocr-model"
    assert fake_model_gateway.selected_capabilities == ["ocr"]
    assert fake_model_gateway.attachment_analyses[0]["media_type"] == "application/pdf"
    assert "OCR extracted text" in fake_model_gateway.chat_messages[-1][-1]["content"]


def test_workspace_scanned_pdf_explains_that_ocr_is_required(
    client, user_headers
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "读一下"},
        files={"file": ("scan.pdf", b"%PDF-1.4\nscan", "application/pdf")},
    )
    assert response.status_code == 422
    assert "开启 OCR" in response.text


def test_workspace_image_rejects_extension_signature_mismatch(
    client, user_headers, fake_model_gateway
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "看看图片"},
        files={"file": ("fake.png", b"not an image", "image/png")},
    )
    assert response.status_code == 422
    assert "扩展名与实际文件格式不一致" in response.text
