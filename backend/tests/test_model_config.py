from __future__ import annotations

from app.model_gateway import OpenAICompatibleGateway, get_model_gateway
from conftest import make_credential


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
    }

    forbidden = client.put(
        "/api/v1/super-admin/model/config",
        headers=user_headers,
        json=model_payload(),
    )
    assert forbidden.status_code == 403


def test_model_connection_can_be_tested_before_saving(
    client, owner_headers, monkeypatch
):
    observed = {}

    async def fake_test_connection(self):
        observed["model_name"] = self.model_name
        observed["base_url"] = self.connection.base_url
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
        "has_key": True,
    }


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
