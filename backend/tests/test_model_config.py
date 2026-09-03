from __future__ import annotations

from app.model_gateway import OpenAICompatibleGateway, get_model_gateway
from conftest import login, make_credential, make_email, make_password


def model_payload(**overrides):
    payload = {
        "base_url": "https://models.example.com/v1",
        "api_key": make_credential("server-model"),
        "clear_api_key": False,
        "models": ["review-pro", "review-fast"],
        "default_model": "review-pro",
        "timeout_seconds": 90,
        "temperature": 0.1,
        "json_mode": True,
        "native_tools": True,
        "tls_verify": True,
    }
    payload.update(overrides)
    return payload


def test_model_config_is_editable_without_exposing_secret(
    client, owner_headers, user_headers
):
    saved = client.put(
        "/api/v1/super-admin/model/config",
        headers=owner_headers,
        json=model_payload(),
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["models"] == ["review-pro", "review-fast"]
    assert body["default_model"] == "review-pro"
    assert body["api_key_configured"] is True
    assert body["source"] == "database"
    assert "api_key" not in body

    available = client.get("/api/v1/models/available", headers=user_headers)
    assert available.status_code == 200
    assert available.json() == {
        "configured": True,
        "models": ["review-pro", "review-fast"],
        "default_model": "review-pro",
        "vision_configured": False,
        "default_vision_model": None,
        "ocr_configured": False,
        "default_ocr_model": None,
    }

    forbidden = client.put(
        "/api/v1/super-admin/model/config",
        headers=user_headers,
        json=model_payload(),
    )
    assert forbidden.status_code == 403


def test_approved_admin_can_manage_models(client, owner_headers, user_headers):
    admin_email = make_email("model-admin")
    admin_password = make_password("model-admin")
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": admin_email,
            "display_name": "Model Admin",
            "password": admin_password,
            "identity": "admin",
        },
    )
    assert registered.status_code == 201, registered.text
    approved = client.post(
        f"/api/v1/super-admin/users/{registered.json()['user']['id']}/approve-admin",
        headers=owner_headers,
    )
    assert approved.status_code == 200, approved.text
    admin_headers = login(client, admin_email, admin_password)

    catalog = client.get("/api/v1/super-admin/models", headers=admin_headers)
    assert catalog.status_code == 200, catalog.text
    created = client.post(
        "/api/v1/super-admin/models",
        headers=admin_headers,
        json=connection_payload("admin-model", "https://admin.example.com/v1", is_default=True),
    )
    assert created.status_code == 201, created.text
    assert client.get("/api/v1/super-admin/models", headers=user_headers).status_code == 403


def test_model_connection_can_be_tested_before_saving(
    client, owner_headers, monkeypatch
):
    observed = {}

    async def fake_test_connection(self):
        observed["model_name"] = self.model_name
        observed["base_url"] = self.connection.base_url
        observed["api_format"] = self.connection.api_format
        observed["has_key"] = bool(self.connection.api_key)
        return {"model_name": self.model_name, "latency_ms": 37}

    monkeypatch.setattr(OpenAICompatibleGateway, "test_connection", fake_test_connection)
    response = client.post(
        "/api/v1/super-admin/model/test",
        headers=owner_headers,
        json=model_payload(default_model="review-fast"),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "model_name": "review-fast",
        "latency_ms": 37,
        "message": "模型连接正常",
    }
    assert observed == {
        "model_name": "review-fast",
        "base_url": "https://models.example.com/v1",
        "api_format": "openai",
        "has_key": True,
    }

    saved = client.post(
        "/api/v1/super-admin/models",
        headers=owner_headers,
        json=connection_payload("saved-model", "https://saved.example.com/v1"),
    )
    assert saved.status_code == 201, saved.text
    tested_without_key = client.post(
        "/api/v1/super-admin/model/test",
        headers=owner_headers,
        json=model_payload(api_key=None, base_url="https://new.example.com/v1"),
    )
    assert tested_without_key.status_code == 200, tested_without_key.text
    assert observed["has_key"] is False


def test_mineru_connection_is_ocr_only_and_preserves_api_format(client, owner_headers):
    created = client.post(
        "/api/v1/super-admin/models",
        headers=owner_headers,
        json=connection_payload(
            "MinerU OCR",
            "http://10.2.98.237:8511",
            api_key=None,
            api_format="mineru",
            capabilities=["ocr"],
            native_tools=False,
            json_mode=False,
            is_default_ocr=True,
        ),
    )
    assert created.status_code == 201, created.text
    assert created.json()["api_format"] == "mineru"
    assert created.json()["capabilities"] == ["ocr"]
    assert created.json()["is_default_ocr"] is True

    invalid = client.post(
        "/api/v1/super-admin/models",
        headers=owner_headers,
        json=connection_payload(
            "Bad MinerU",
            "http://mineru.example.com",
            api_format="mineru",
            capabilities=["vision", "ocr"],
        ),
    )
    assert invalid.status_code == 422, invalid.text


def connection_payload(name, url, **overrides):
    payload = {
        "model_name": name,
        "base_url": url,
        "api_key": make_credential(f"model-{name}"),
        "timeout_seconds": 120,
        "temperature": 0.2,
        "json_mode": True,
        "native_tools": True,
        "tls_verify": True,
        "is_default": False,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_models_are_managed_as_independent_connections(client, owner_headers, user_headers):
    first = client.post(
        "/api/v1/super-admin/models",
        headers=owner_headers,
        json=connection_payload("model-one", "https://one.example.com/v1", is_default=True),
    )
    second = client.post(
        "/api/v1/super-admin/models",
        headers=owner_headers,
        json=connection_payload("model-two", "https://two.example.com/v1", timeout_seconds=45),
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    catalog = client.get("/api/v1/super-admin/models", headers=owner_headers)
    assert catalog.status_code == 200, catalog.text
    items = {item["model_name"]: item for item in catalog.json()["items"]}
    assert set(items) == {"model-one", "model-two"}
    assert items["model-one"]["base_url"] == "https://one.example.com/v1"
    assert items["model-two"]["base_url"] == "https://two.example.com/v1"
    assert items["model-two"]["timeout_seconds"] == 45
    assert items["model-one"]["api_key_configured"] is True
    assert "api_key" not in items["model-one"]

    available = client.get("/api/v1/models/available", headers=user_headers)
    assert available.json() == {
        "configured": True,
        "models": ["model-one", "model-two"],
        "default_model": "model-one",
        "vision_configured": False,
        "default_vision_model": None,
        "ocr_configured": False,
        "default_ocr_model": None,
    }

    gateway = get_model_gateway()
    assert gateway.for_model("model-one").connection.base_url == "https://one.example.com/v1"
    assert gateway.for_model("model-two").connection.base_url == "https://two.example.com/v1"

    second_id = second.json()["id"]
    made_default = client.post(
        f"/api/v1/super-admin/models/{second_id}/default",
        headers=owner_headers,
    )
    assert made_default.status_code == 200, made_default.text
    assert made_default.json()["is_default"] is True
    assert client.get("/api/v1/models/available", headers=user_headers).json()["default_model"] == "model-two"

    deleted = client.delete(
        f"/api/v1/super-admin/models/{second_id}",
        headers=owner_headers,
    )
    assert deleted.status_code == 204, deleted.text
    remaining = client.get("/api/v1/super-admin/models", headers=owner_headers).json()
    assert remaining["default_model"] == "model-one"
    assert [item["model_name"] for item in remaining["items"]] == ["model-one"]


def test_visual_and_ocr_models_have_independent_defaults(
    client, owner_headers, user_headers
):
    chat = client.post(
        "/api/v1/super-admin/models",
        headers=owner_headers,
        json=connection_payload("chat-only", "https://chat.example.com/v1"),
    )
    vision = client.post(
        "/api/v1/super-admin/models",
        headers=owner_headers,
        json=connection_payload(
            "vision-private",
            "https://vision.example.com/v1",
            capabilities=["vision"],
            native_tools=False,
        ),
    )
    ocr = client.post(
        "/api/v1/super-admin/models",
        headers=owner_headers,
        json=connection_payload(
            "ocr-private",
            "https://ocr.example.com/v1",
            capabilities=["ocr"],
            native_tools=False,
        ),
    )
    assert chat.status_code == 201, chat.text
    assert vision.status_code == 201, vision.text
    assert ocr.status_code == 201, ocr.text
    assert vision.json()["is_default_vision"] is True
    assert ocr.json()["is_default_ocr"] is True

    available = client.get("/api/v1/models/available", headers=user_headers).json()
    assert available == {
        "configured": True,
        "models": ["chat-only"],
        "default_model": "chat-only",
        "vision_configured": True,
        "default_vision_model": "vision-private",
        "ocr_configured": True,
        "default_ocr_model": "ocr-private",
    }

    gateway = get_model_gateway()
    assert gateway.for_capability("vision").model_name == "vision-private"
    assert gateway.for_capability("ocr").model_name == "ocr-private"
    assert client.post(
        f"/api/v1/super-admin/models/{vision.json()['id']}/default",
        headers=owner_headers,
    ).status_code == 404
