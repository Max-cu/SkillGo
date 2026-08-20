from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import pytest


test_root = Path(__file__).resolve().parents[1] / ".test-data" / uuid4().hex
test_run_id = uuid4().hex


@lru_cache
def make_email(label: str) -> str:
    return f"{label}-{test_run_id}@example.org"


@lru_cache
def make_password(label: str) -> str:
    del label
    return f"T!{uuid4().hex}{uuid4().hex}"


@lru_cache
def make_credential(label: str) -> str:
    del label
    return f"{uuid4().hex}{uuid4().hex}"


TEST_OWNER_EMAIL = make_email("owner")
TEST_OWNER_PASSWORD = make_password("owner")
TEST_CREATOR_EMAIL = make_email("creator")
TEST_CREATOR_PASSWORD = make_password("creator")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["SKILLGO_ENVIRONMENT"] = "test"
os.environ["SKILLGO_DATABASE_URL"] = f"sqlite:///{(test_root / 'test.db').as_posix()}"
os.environ["SKILLGO_STORAGE_ROOT"] = str(test_root / "storage")
os.environ["SKILLGO_JWT_SECRET"] = f"{uuid4().hex}{uuid4().hex}"
os.environ["SKILLGO_BOOTSTRAP_EMAIL"] = TEST_OWNER_EMAIL
os.environ["SKILLGO_BOOTSTRAP_PASSWORD"] = TEST_OWNER_PASSWORD
os.environ["SKILLGO_BOOTSTRAP_NAME"] = "Test Owner"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.model_gateway import ModelResult, get_model_gateway  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data():
    """Keep repeated local and CI runs from accumulating SQLite workspaces."""

    yield
    engine.dispose()
    shutil.rmtree(test_root, ignore_errors=True)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def test_data_root() -> Path:
    return test_root


class FakeModelGateway:
    configured = True
    model_name = "test-private-model"
    available_models = ("test-private-model", "test-fast-model")

    def __init__(self):
        self.histories: list[list[dict]] = []
        self.chat_modes: list[bool] = []
        self.workspace_files: list[list[dict[str, str]]] = []
        self.analyzed_skills: list[dict] = []
        self.routed_skills: list[dict] = []
        self.requested_models: list[str] = []
        self.chat_messages: list[list[dict[str, str]]] = []

    def for_model(self, model_name):
        selected = (model_name or self.model_name).strip()
        if selected not in self.available_models:
            from app.model_gateway import ModelGatewayError

            raise ModelGatewayError("MODEL_NOT_ALLOWED", "Selected model is not available")
        self.requested_models.append(selected)
        self.model_name = selected
        return self

    async def analyze_skill(self, *, skill_md, package_metadata):
        self.analyzed_skills.append({"skill_md": skill_md, "package_metadata": package_metadata})
        return ModelResult(
            output={
                "name": "智能内容总结",
                "slug": "smart-summary",
                "summary": "自动提取内容重点并生成清晰、可复用的结构化摘要。",
                "description": "适合长文本整理、重点提取和摘要生成；输出前会区分事实与建议。",
                "category": "writing",
            },
            model_name=self.model_name,
            token_usage={"total_tokens": 42},
        )

    async def route_skills(self, *, instruction, filename, candidates):
        self.routed_skills.append(
            {"instruction": instruction, "filename": filename, "candidates": candidates}
        )
        needle = f"{instruction} {filename or ''}".casefold()
        matched = next(
            (
                item
                for item in candidates
                if str(item.get("name", "")).casefold() in needle
                or str(item.get("summary", "")).casefold() in needle
            ),
            candidates[0],
        )
        return ModelResult(
            output={"version_ids": [matched["version_id"]]},
            model_name=self.model_name,
            token_usage={"total_tokens": 8},
        )

    async def chat(self, *, messages):
        self.chat_messages.append(list(messages))
        latest = next(
            (item["content"] for item in reversed(messages) if item.get("role") == "user"),
            "",
        )
        return ModelResult(
            output={"message": f"General reply: {latest}"},
            model_name=self.model_name,
            token_usage={"total_tokens": 6},
            latency_ms=18,
        )

    async def chat_stream(self, *, messages):
        self.chat_messages.append(list(messages))
        latest = next(
            (item["content"] for item in reversed(messages) if item.get("role") == "user"),
            "",
        )
        reply = f"General reply: {latest}"
        midpoint = max(1, len(reply) // 2)
        yield {"type": "delta", "text": reply[:midpoint]}
        yield {"type": "delta", "text": reply[midpoint:]}
        yield {
            "type": "done",
            "model_name": self.model_name,
            "token_usage": {"total_tokens": 6},
            "latency_ms": 18,
        }

    async def execute(self, *, skill_md, input_schema, output_schema, input_data, history=None, chat_mode=False, workspace_files=None):
        self.histories.append(list(history or []))
        self.chat_modes.append(chat_mode)
        self.workspace_files.append(list(workspace_files or []))
        source = input_data.get("content") or input_data.get("message", "")
        return ModelResult(
            output={"summary": f"Summary: {source}"},
            model_name=self.model_name,
            token_usage={"total_tokens": 12},
        )


@pytest.fixture()
def fake_model_gateway():
    gateway = FakeModelGateway()
    app.dependency_overrides[get_model_gateway] = lambda: gateway
    try:
        yield gateway
    finally:
        app.dependency_overrides.pop(get_model_gateway, None)


def login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner_headers(client: TestClient) -> dict[str, str]:
    return login(client, TEST_OWNER_EMAIL, TEST_OWNER_PASSWORD)


@pytest.fixture()
def user_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": TEST_CREATOR_EMAIL,
            "display_name": "Skill Creator",
            "password": TEST_CREATOR_PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
