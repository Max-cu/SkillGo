from __future__ import annotations

from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import bootstrap
from app.models import Role, User
from app.security import hash_password
from conftest import TEST_CREATOR_EMAIL, TEST_OWNER_EMAIL, make_email, make_password


def test_register_and_role_boundaries(client, user_headers, owner_headers):
    me = client.get("/api/v1/auth/me", headers=user_headers)
    assert me.status_code == 200
    assert me.json()["role"] == "user"

    denied = client.get("/api/v1/admin/users", headers=user_headers)
    assert denied.status_code == 403

    users = client.get("/api/v1/admin/users", headers=owner_headers)
    assert users.status_code == 200
    creator = next(item for item in users.json() if item["email"] == TEST_CREATOR_EMAIL)

    promote = client.patch(
        f"/api/v1/super-admin/users/{creator['id']}/role",
        headers=owner_headers,
        json={"role": "admin"},
    )
    assert promote.status_code == 200
    assert promote.json()["role"] == "admin"


def test_member_registration_is_active_immediately(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("member"),
            "display_name": "New Member",
            "password": make_password("member"),
            "identity": "member",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["access_token"]
    assert body["user"]["role"] == "user"
    assert body["user"]["is_active"] is True


def test_admin_registration_waits_for_super_admin_approval(client, owner_headers):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("admin-request"),
            "display_name": "Admin Applicant",
            "password": make_password("admin-request"),
            "identity": "admin",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending_approval"
    assert body["access_token"] is None
    assert body["user"]["role"] == "admin"
    assert body["user"]["is_active"] is False

    pending_login = client.post(
        "/api/v1/auth/login",
        json={
            "email": make_email("admin-request"),
            "password": make_password("admin-request"),
        },
    )
    assert pending_login.status_code == 403
    assert "等待超级管理员审核" in pending_login.json()["detail"]

    approved = client.post(
        f"/api/v1/super-admin/users/{body['user']['id']}/approve-admin",
        headers=owner_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["is_active"] is True

    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": make_email("admin-request"),
            "password": make_password("admin-request"),
        },
    )
    assert login.status_code == 200, login.text


def test_regular_admin_cannot_approve_an_admin_application(client, user_headers, owner_headers):
    actor = client.get("/api/v1/auth/me", headers=user_headers).json()
    promoted = client.patch(
        f"/api/v1/super-admin/users/{actor['id']}/role",
        headers=owner_headers,
        json={"role": "admin"},
    )
    assert promoted.status_code == 200, promoted.text

    request = client.post(
        "/api/v1/auth/register",
        json={
            "email": make_email("second-admin"),
            "display_name": "Second Admin",
            "password": make_password("second-admin"),
            "identity": "admin",
        },
    )
    target_id = request.json()["user"]["id"]

    denied = client.post(
        f"/api/v1/super-admin/users/{target_id}/approve-admin",
        headers=user_headers,
    )
    assert denied.status_code == 403

    denied_status_change = client.patch(
        f"/api/v1/admin/users/{target_id}",
        headers=user_headers,
        json={"is_active": True},
    )
    assert denied_status_change.status_code == 403


def test_second_super_admin_cannot_be_assigned(client, user_headers, owner_headers):
    target = client.get("/api/v1/auth/me", headers=user_headers).json()
    response = client.patch(
        f"/api/v1/super-admin/users/{target['id']}/role",
        headers=owner_headers,
        json={"role": "super_admin"},
    )

    assert response.status_code == 422


def test_bootstrap_repairs_legacy_duplicate_super_admins(client):
    with SessionLocal() as db:
        db.add(
            User(
                email=make_email("legacy-owner"),
                display_name="Legacy Owner",
                password_hash=hash_password(make_password("legacy-owner")),
                role=Role.SUPER_ADMIN,
            )
        )
        db.commit()

    bootstrap()

    with SessionLocal() as db:
        assert db.scalar(
            select(func.count()).select_from(User).where(User.role == Role.SUPER_ADMIN)
        ) == 1
        owner = db.scalar(select(User).where(User.email == TEST_OWNER_EMAIL))
        legacy = db.scalar(select(User).where(User.email == make_email("legacy-owner")))
        assert owner is not None and owner.role == Role.SUPER_ADMIN
        assert legacy is not None and legacy.role == Role.ADMIN


def test_user_cannot_promote_themselves(client, user_headers):
    me = client.get("/api/v1/auth/me", headers=user_headers).json()
    response = client.patch(
        f"/api/v1/super-admin/users/{me['id']}/role",
        headers=user_headers,
        json={"role": "admin"},
    )
    assert response.status_code == 403


def test_super_admin_can_delete_selected_user_but_not_themselves(client, user_headers, owner_headers):
    creator = client.get("/api/v1/auth/me", headers=user_headers).json()

    denied = client.post(
        "/api/v1/super-admin/users/delete",
        headers=user_headers,
        json={"user_ids": [creator["id"]]},
    )
    assert denied.status_code == 403

    deleted = client.post(
        "/api/v1/super-admin/users/delete",
        headers=owner_headers,
        json={"user_ids": [creator["id"]]},
    )
    assert deleted.status_code == 200, deleted.text
    assert all(
        user["id"] != creator["id"]
        for user in client.get("/api/v1/admin/users", headers=owner_headers).json()
    )

    actor = client.get("/api/v1/auth/me", headers=owner_headers).json()
    self_delete = client.post(
        "/api/v1/super-admin/users/delete",
        headers=owner_headers,
        json={"user_ids": [actor["id"]]},
    )
    assert self_delete.status_code == 409


def test_user_with_owned_skill_must_resolve_skill_before_account_deletion(client, user_headers, owner_headers):
    creator = client.get("/api/v1/auth/me", headers=user_headers).json()
    created = client.post(
        "/api/v1/skills",
        headers=user_headers,
        json={
            "slug": "owned-before-delete",
            "name": "Owned Skill",
            "summary": "This Skill prevents accidental account deletion.",
            "description": "",
            "category": "other",
            "visibility": "private",
            "icon": "sparkles",
        },
    )
    assert created.status_code == 201, created.text

    blocked = client.post(
        "/api/v1/super-admin/users/delete",
        headers=owner_headers,
        json={"user_ids": [creator["id"]]},
    )
    assert blocked.status_code == 409
    assert "Skill" in blocked.json()["detail"]
