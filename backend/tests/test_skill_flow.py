from __future__ import annotations

import asyncio
import io
import zipfile
from types import SimpleNamespace

from app import skill_analysis
from app.model_gateway import ModelResult
from conftest import login, make_email, make_password


def skill_zip(version: str = "1.0.0", unsafe_name: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "# Summary Writer\n\nTurn the supplied content into a concise summary.\n",
        )
        archive.writestr(
            "manifest.yaml",
            f"""apiVersion: skillgo.io/v1alpha1
kind: Skill
metadata:
  name: summary-writer
  version: {version}
spec:
  type: instruction
  inputSchema:
    type: object
    required: [content]
  outputSchema:
    type: object
    required: [summary]
    properties:
      summary:
        type: string
  permissions:
    tools: []
""",
        )
        if unsafe_name:
            archive.writestr(unsafe_name, "bad")
    return output.getvalue()


def standard_skill_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "summary-writer/SKILL.md",
            """---
name: summary-writer
description: Summarize long content and separate facts from recommendations.
---

# Summary Writer

Read the supplied content, identify the objective, and return a concise summary.
""",
        )
        archive.writestr("summary-writer/references/example.md", "Example reference")
    return output.getvalue()


def nonstandard_skill_zip(*, nested: bool = False) -> bytes:
    output = io.BytesIO()
    skill_path = "repository-main/skills/interview-simulator/SKILL.md" if nested else "SKILL.md"
    with zipfile.ZipFile(output, "w") as archive:
        if nested:
            archive.writestr("repository-main/README.md", "Repository wrapper")
        archive.writestr(
            skill_path,
            "# Interview Simulator\n\nPractice realistic interviews and receive actionable feedback.\n",
        )
    return output.getvalue()


def sandbox_network_skill_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "network-check/SKILL.md",
            """---
name: network-check
description: Call a public endpoint from an approved Python script.
---

# Network check

Run `python scripts/check.py` to call https://example.com and save the result.
The workflow requires internet access.
""",
        )
        archive.writestr(
            "network-check/scripts/check.py",
            "import urllib.request\nprint(urllib.request.urlopen('https://example.com').status)\n",
        )
    return output.getvalue()


def test_analyze_standard_package_with_configured_model(
    client, user_headers, fake_model_gateway
):
    response = client.post(
        "/api/v1/skills/analyze-package",
        headers=user_headers,
        files={"package": ("summary-writer.zip", standard_skill_zip(), "application/zip")},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source"] == "ai"
    assert result["package_format"] == "agent-skill"
    assert result["name"] == "智能内容总结"
    assert result["slug"] == "summary-writer"
    assert result["category"] == "writing"
    assert result["version"] == "0.1.0"
    assert len(fake_model_gateway.analyzed_skills) == 1


def test_analyze_package_replaces_invalid_ai_slug_with_safe_fallback(
    client, user_headers, fake_model_gateway
):
    async def analyze_skill(*, skill_md, package_metadata):
        del skill_md, package_metadata
        return ModelResult(
            output={
                "name": "孔板计算",
                "slug": "孔板计算",
                "summary": "根据输入参数完成孔板流量与尺寸计算。",
                "description": "读取输入参数并生成孔板计算结果。",
                "category": "productivity",
            },
            model_name="test-private-model",
            token_usage={"total_tokens": 12},
        )

    fake_model_gateway.analyze_skill = analyze_skill
    response = client.post(
        "/api/v1/skills/analyze-package",
        headers=user_headers,
        files={"package": ("孔板计算_skill.zip", standard_skill_zip(), "application/zip")},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["name"] == "孔板计算"
    assert result["slug"] == "summary-writer"


def test_analyze_nonstandard_package_infers_missing_frontmatter(
    client, user_headers, fake_model_gateway
):
    fake_model_gateway.configured = False
    response = client.post(
        "/api/v1/skills/analyze-package",
        headers=user_headers,
        files={"package": ("interview-simulator.zip", nonstandard_skill_zip(), "application/zip")},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["name"] == "Interview Simulator"
    assert result["slug"] == "interview-simulator"
    assert any("缺少标准" in warning for warning in result["warnings"])


def test_analyze_finds_single_skill_inside_repository_wrapper(
    client, user_headers, fake_model_gateway
):
    fake_model_gateway.configured = False
    response = client.post(
        "/api/v1/skills/analyze-package",
        headers=user_headers,
        files={"package": ("nested.zip", nonstandard_skill_zip(nested=True), "application/zip")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["slug"] == "interview-simulator"


def test_analyze_rejects_ambiguous_multi_skill_package(client, user_headers):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("skills/first/SKILL.md", "# First\n\nFirst workflow description.\n")
        archive.writestr("skills/second/SKILL.md", "# Second\n\nSecond workflow description.\n")

    response = client.post(
        "/api/v1/skills/analyze-package",
        headers=user_headers,
        files={"package": ("plugin.zip", output.getvalue(), "application/zip")},
    )

    assert response.status_code == 422
    assert "multiple SKILL.md" in response.json()["detail"]


def test_analyze_timeout_falls_back_without_blocking_import(
    client, user_headers, fake_model_gateway, monkeypatch
):
    async def slow_analysis(**kwargs):
        del kwargs
        await asyncio.sleep(0.2)
        raise AssertionError("cancelled analysis must not finish")

    fake_model_gateway.analyze_skill = slow_analysis
    monkeypatch.setattr(
        skill_analysis,
        "settings",
        SimpleNamespace(skill_analysis_timeout_seconds=0.01),
    )
    response = client.post(
        "/api/v1/skills/analyze-package",
        headers=user_headers,
        files={"package": ("summary.zip", standard_skill_zip(), "application/zip")},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source"] == "package"
    assert result["slug"] == "summary-writer"
    assert any("AI 资料预填超时" in warning for warning in result["warnings"])


def test_analyze_suggests_available_slug_when_package_slug_is_taken(
    client, user_headers, fake_model_gateway
):
    created = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "summary-writer",
            "name": "Existing Summary Writer",
            "summary": "An existing Skill used to verify identifier conflict handling.",
        },
    )
    assert created.status_code == 201, created.text

    response = client.post(
        "/api/v1/skills/analyze-package",
        headers=user_headers,
        files={"package": ("summary.zip", standard_skill_zip(), "application/zip")},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["slug"] == "summary-writer-2"
    assert any("已被占用" in warning for warning in result["warnings"])
    assert any("上传新版本" in warning for warning in result["warnings"])


def test_create_skill_conflict_returns_chinese_message_and_suggestion(client, user_headers):
    payload = {
        "slug": "duplicate-skill",
        "name": "Duplicate Skill",
        "summary": "A duplicate Skill used to verify clear conflict messages.",
    }
    assert client.post("/api/v1/skills", headers=user_headers, json=payload).status_code == 201

    response = client.post("/api/v1/skills", headers=user_headers, json=payload)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "SKILL_SLUG_CONFLICT",
        "message": "这个唯一标识已被使用，请更换后重试",
        "suggested_slug": "duplicate-skill-2",
    }


def test_standard_package_upload_gets_platform_versions(client, user_headers):
    skill = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "portable-summary",
            "name": "Portable Summary",
            "summary": "A portable standard Agent Skill used for summary generation.",
            "visibility": "private",
        },
    ).json()
    first = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={"package": ("standard.zip", standard_skill_zip(), "application/zip")},
    )
    second = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={"package": ("standard.zip", standard_skill_zip(), "application/zip")},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["version"] == "0.1.0"
    assert second.json()["version"] == "0.1.1"


def test_upload_submit_approve_and_community(client, user_headers, owner_headers):
    created = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "summary-writer",
            "name": "Summary Writer",
            "summary": "Turn long content into a concise structured summary.",
            "description": "A safe instruction-only starter skill.",
            "category": "writing",
            "visibility": "public",
            "icon": "wand",
        },
    )
    assert created.status_code == 201, created.text
    skill_id = created.json()["id"]

    uploaded = client.post(
        f"/api/v1/skills/{skill_id}/versions",
        headers=user_headers,
        files={"package": ("summary-writer.zip", skill_zip(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["id"]
    assert uploaded.json()["status"] == "ready"

    hidden = client.get("/api/v1/community/skills")
    assert hidden.json() == []

    submitted = client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/submit",
        headers=user_headers,
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"

    queue = client.get("/api/v1/admin/reviews", headers=owner_headers)
    assert queue.status_code == 200
    assert len(queue.json()) == 1

    approved = client.post(
        f"/api/v1/admin/reviews/{version_id}/approve",
        headers=owner_headers,
        json={"note": "Safe instruction-only Skill"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "published"
    assert approved.json()["network_enabled"] is False

    public = client.get("/api/v1/community/skills")
    assert public.status_code == 200
    assert public.json()[0]["slug"] == "summary-writer"

    viewer = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("community-runner"),
            "display_name": "Community Runner",
            "password": make_password("community-runner"),
        },
    ).json()
    viewer_headers = {"Authorization": f"Bearer {viewer['access_token']}"}
    runnable = client.get(f"/api/v1/skills/{skill_id}", headers=viewer_headers)
    assert runnable.status_code == 200
    assert [item["status"] for item in runnable.json()["versions"]] == ["published"]


def test_admin_controls_network_access_before_and_after_sandbox_version_submission(
    client, user_headers, owner_headers
):
    admin_email = make_email("network-admin")
    admin_password = make_password("network-admin")
    registered_admin = client.post(
        "/api/v1/auth/register",
        json={
            "email": admin_email,
            "display_name": "Network Admin",
            "password": admin_password,
            "identity": "admin",
        },
    )
    assert registered_admin.status_code == 201, registered_admin.text
    approved_admin = client.post(
        f"/api/v1/super-admin/users/{registered_admin.json()['user']['id']}/approve-admin",
        headers=owner_headers,
    )
    assert approved_admin.status_code == 200, approved_admin.text
    admin_headers = login(client, admin_email, admin_password)

    skill = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "network-check",
            "name": "Network Check",
            "summary": "A sandbox Skill whose runtime network access requires approval.",
            "visibility": "private",
        },
    ).json()
    uploaded = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={"package": ("network-check.zip", sandbox_network_skill_zip(), "application/zip")},
    ).json()
    assert uploaded["execution_mode"] == "sandbox_required"
    assert uploaded["runtime_requirements"]["network"] is True
    assert uploaded["network_enabled"] is False

    forbidden = client.patch(
        f"/api/v1/admin/skill-versions/{uploaded['id']}/network-access",
        headers=user_headers,
        json={"enabled": True},
    )
    assert forbidden.status_code == 403

    enabled_before_submission = client.patch(
        f"/api/v1/admin/skill-versions/{uploaded['id']}/network-access",
        headers=admin_headers,
        json={"enabled": True},
    )
    assert enabled_before_submission.status_code == 200, enabled_before_submission.text
    assert enabled_before_submission.json()["status"] == "ready"
    assert enabled_before_submission.json()["network_enabled"] is True

    client.post(
        f"/api/v1/skills/{skill['id']}/versions/{uploaded['id']}/submit",
        headers=user_headers,
    )
    approved = client.post(
        f"/api/v1/admin/reviews/{uploaded['id']}/approve",
        headers=admin_headers,
        json={"note": "Approved with preauthorized runtime network"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["network_enabled"] is True
    assert approved.json()["skill_name"] == skill["name"]

    published = client.get(
        "/api/v1/admin/skill-versions/published", headers=admin_headers
    )
    assert published.status_code == 200
    assert published.json()[0]["id"] == uploaded["id"]
    assert published.json()[0]["network_enabled"] is True

    disabled = client.patch(
        f"/api/v1/admin/skill-versions/{uploaded['id']}/network-access",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["network_enabled"] is False

    enabled = client.patch(
        f"/api/v1/admin/skill-versions/{uploaded['id']}/network-access",
        headers=owner_headers,
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["network_enabled"] is True


def test_non_sandbox_version_cannot_receive_runtime_network_access(
    client, user_headers, owner_headers
):
    skill = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "offline-summary",
            "name": "Offline Summary",
            "summary": "An instruction-only Skill that does not execute inside the sandbox.",
            "visibility": "private",
        },
    ).json()
    uploaded = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={"package": ("offline-summary.zip", skill_zip(), "application/zip")},
    ).json()
    client.post(
        f"/api/v1/skills/{skill['id']}/versions/{uploaded['id']}/submit",
        headers=user_headers,
    )
    approved = client.post(
        f"/api/v1/admin/reviews/{uploaded['id']}/approve",
        headers=owner_headers,
        json={"network_enabled": True},
    )
    assert approved.status_code == 200
    assert approved.json()["network_enabled"] is False

    rejected = client.patch(
        f"/api/v1/admin/skill-versions/{uploaded['id']}/network-access",
        headers=owner_headers,
        json={"enabled": True},
    )
    assert rejected.status_code == 409


def test_approved_private_skill_can_be_published_and_unpublished(
    client, user_headers, owner_headers
):
    skill = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "private-to-community",
            "name": "Private To Community",
            "summary": "An approved private Skill that can be explicitly shared.",
            "visibility": "private",
        },
    ).json()

    premature = client.patch(
        f"/api/v1/skills/{skill['id']}/visibility",
        headers=user_headers,
        json={"visibility": "public"},
    )
    assert premature.status_code == 409
    assert premature.json()["detail"]["code"] == "PUBLISHED_VERSION_REQUIRED"

    version = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={"package": ("private-to-community.zip", skill_zip(), "application/zip")},
    ).json()
    client.post(
        f"/api/v1/skills/{skill['id']}/versions/{version['id']}/submit",
        headers=user_headers,
    )
    approved = client.post(
        f"/api/v1/admin/reviews/{version['id']}/approve",
        headers=owner_headers,
        json={"note": "Approved but still private until the owner publishes it."},
    )
    assert approved.status_code == 200
    assert client.get("/api/v1/community/skills").json() == []

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("visibility-other"),
            "display_name": "Visibility Other",
            "password": make_password("visibility-other"),
        },
    ).json()
    denied = client.patch(
        f"/api/v1/skills/{skill['id']}/visibility",
        headers={"Authorization": f"Bearer {other['access_token']}"},
        json={"visibility": "public"},
    )
    assert denied.status_code == 404

    published = client.patch(
        f"/api/v1/skills/{skill['id']}/visibility",
        headers=user_headers,
        json={"visibility": "public"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["visibility"] == "public"
    assert client.get("/api/v1/community/skills").json()[0]["slug"] == skill["slug"]

    unpublished = client.patch(
        f"/api/v1/skills/{skill['id']}/visibility",
        headers=user_headers,
        json={"visibility": "private"},
    )
    assert unpublished.status_code == 200
    assert unpublished.json()["visibility"] == "private"
    assert client.get("/api/v1/community/skills").json() == []


def test_zip_slip_is_rejected(client, user_headers):
    skill = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "unsafe-package",
            "name": "Unsafe Package",
            "summary": "This package should be rejected by archive validation.",
            "visibility": "private",
        },
    ).json()
    response = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={"package": ("unsafe.zip", skill_zip(unsafe_name="../escape.txt"), "application/zip")},
    )
    assert response.status_code == 422
    assert "unsafe archive path" in response.json()["detail"]


def test_windows_zip_separator_is_normalized(client, user_headers):
    skill = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "windows-zip",
            "name": "Windows ZIP",
            "summary": "A valid package created with Windows path separators.",
            "visibility": "private",
        },
    ).json()
    response = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={
            "package": (
                "windows.zip",
                skill_zip(unsafe_name=r"agents\openai.yaml"),
                "application/zip",
            )
        },
    )
    assert response.status_code == 201, response.text


def test_owner_can_delete_skill_and_package(client, user_headers, test_data_root):
    skill = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "delete-me",
            "name": "Delete Me",
            "summary": "A temporary Skill that should be removable by its owner.",
            "visibility": "private",
        },
    ).json()
    uploaded = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        headers=user_headers,
        files={"package": ("delete-me.zip", skill_zip(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    skill_storage = test_data_root / "storage" / "skill-packages" / skill["id"]
    stored_packages = list(skill_storage.rglob("*.zip"))
    assert len(stored_packages) == 1

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("other"),
            "display_name": "Other User",
            "password": make_password("other"),
        },
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    forbidden = client.delete(f"/api/v1/skills/{skill['id']}", headers=other_headers)
    assert forbidden.status_code == 404

    deleted = client.delete(f"/api/v1/skills/{skill['id']}", headers=user_headers)
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/api/v1/skills/{skill['id']}", headers=user_headers).status_code == 404
    assert client.get("/api/v1/skills/mine", headers=user_headers).json() == []
    assert list(skill_storage.rglob("*.zip")) == []
