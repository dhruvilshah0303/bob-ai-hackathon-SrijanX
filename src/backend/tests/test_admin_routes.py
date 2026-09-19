"""API-level tests for /api/admin/* (admin_routes.py): ADMIN-only user
provisioning for roles that can't self-register (HOSPITAL_ADMIN/ADMIN),
listing, and activate/deactivate."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth_service import create_access_token, hash_password
from db_session import get_db
from models import Hospital, HospitalVerificationStatus, Role, User


@pytest.fixture()
def client():
    return TestClient(app)


def _make_user(role: str, hospital_id: str | None = None) -> tuple[User, str]:
    db = next(get_db())
    try:
        user = User(
            name=f"Test {role}", email=f"{role.lower()}-{uuid.uuid4().hex[:8]}@example-dev.test",
            password_hash=hash_password("irrelevant-not-used"), role=role, hospital_id=hospital_id, is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user, create_access_token(user)
    finally:
        db.close()


def _make_hospital() -> Hospital:
    db = next(get_db())
    try:
        hospital = Hospital(name=f"Test Hospital {uuid.uuid4().hex[:8]}", lat=1, lng=1, verification_status=HospitalVerificationStatus.VERIFIED, is_active=True)
        db.add(hospital)
        db.commit()
        db.refresh(hospital)
        return hospital
    finally:
        db.close()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_create_user_requires_admin(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    resp = client.post(
        "/api/admin/users",
        json={"name": "New Admin", "email": f"{uuid.uuid4().hex}@example-dev.test", "password": "hunter2pass", "role": Role.ADMIN},
        headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 403


def test_admin_can_create_admin_account(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    resp = client.post(
        "/api/admin/users",
        json={"name": "New Admin", "email": f"{uuid.uuid4().hex}@example-dev.test", "password": "hunter2pass", "role": Role.ADMIN},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == Role.ADMIN


def test_admin_can_create_hospital_admin_with_hospital_id(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _make_hospital()
    resp = client.post(
        "/api/admin/users",
        json={
            "name": "New Hospital Admin", "email": f"{uuid.uuid4().hex}@example-dev.test",
            "password": "hunter2pass", "role": Role.HOSPITAL_ADMIN, "hospital_id": hospital.id,
        },
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["hospital_id"] == hospital.id


def test_hospital_admin_without_hospital_id_is_rejected(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    resp = client.post(
        "/api/admin/users",
        json={"name": "Bad", "email": f"{uuid.uuid4().hex}@example-dev.test", "password": "hunter2pass", "role": Role.HOSPITAL_ADMIN},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400


def test_hospital_id_rejected_for_non_hospital_admin_role(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _make_hospital()
    resp = client.post(
        "/api/admin/users",
        json={
            "name": "Bad", "email": f"{uuid.uuid4().hex}@example-dev.test", "password": "hunter2pass",
            "role": Role.DISPATCHER, "hospital_id": hospital.id,
        },
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400


def test_create_user_rejects_unknown_hospital(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    resp = client.post(
        "/api/admin/users",
        json={
            "name": "Bad", "email": f"{uuid.uuid4().hex}@example-dev.test", "password": "hunter2pass",
            "role": Role.HOSPITAL_ADMIN, "hospital_id": "does-not-exist",
        },
        headers=_auth(admin_token),
    )
    assert resp.status_code == 404


def test_create_user_duplicate_email_conflicts(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    email = f"{uuid.uuid4().hex}@example-dev.test"
    payload = {"name": "Dupe", "email": email, "password": "hunter2pass", "role": Role.DISPATCHER}
    first = client.post("/api/admin/users", json=payload, headers=_auth(admin_token))
    assert first.status_code == 201
    second = client.post("/api/admin/users", json=payload, headers=_auth(admin_token))
    assert second.status_code == 409


def test_list_users_requires_admin(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    resp = client.get("/api/admin/users", headers=_auth(dispatcher_token))
    assert resp.status_code == 403


def test_list_users_includes_created_account(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    created = client.post(
        "/api/admin/users",
        json={"name": "Listed User", "email": f"{uuid.uuid4().hex}@example-dev.test", "password": "hunter2pass", "role": Role.DISPATCHER},
        headers=_auth(admin_token),
    ).json()
    resp = client.get("/api/admin/users", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert any(u["id"] == created["id"] for u in resp.json())


def test_deactivate_user(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    _target, target_token = _make_user(Role.DISPATCHER)
    resp = client.patch(f"/api/admin/users/{_target.id}", json={"is_active": False}, headers=_auth(admin_token))
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # Deactivated user's existing token is now rejected by get_current_user.
    me = client.get("/api/auth/me", headers=_auth(target_token))
    assert me.status_code == 401


def test_update_user_requires_admin(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    _target, _t = _make_user(Role.DISPATCHER)
    resp = client.patch(f"/api/admin/users/{_target.id}", json={"is_active": False}, headers=_auth(dispatcher_token))
    assert resp.status_code == 403


def test_update_user_404_for_unknown_id(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    resp = client.patch("/api/admin/users/does-not-exist", json={"is_active": False}, headers=_auth(admin_token))
    assert resp.status_code == 404


def test_reassign_hospital_id_rejected_for_non_hospital_admin(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _make_hospital()
    _dispatcher, _t = _make_user(Role.DISPATCHER)
    resp = client.patch(f"/api/admin/users/{_dispatcher.id}", json={"hospital_id": hospital.id}, headers=_auth(admin_token))
    assert resp.status_code == 400
