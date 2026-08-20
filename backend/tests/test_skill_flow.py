from __future__ import annotations

import io
import zipfile

from conftest import make_email, make_password


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
    assert result["slug"] == "smart-summary"
    assert result["category"] == "writing"
    assert result["version"] == "0.1.0"
    assert len(fake_model_gateway.analyzed_skills) == 1


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
