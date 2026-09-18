"""API-level tests for /api/auth/* (auth_routes.py) via FastAPI's
TestClient - register/login/me/refresh, plus the RBAC dependency
(auth_service.require_role) against a couple of temporary protected
endpoints registered just for this test file.

Each test uses a fresh randomly-generated email so tests stay independent
even though they share the same on-disk sqlite DB as the rest of the suite
(no per-test transaction rollback - consistent with how the rest of this
test suite already isolates itself, see conftest.py)."""
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import app
from auth_service import require_role
from models import Role


def _unique_email() -> str:
    return f"test-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture()
def client():
    return TestClient(app)


def _register(client, role=Role.DISPATCHER, password="hunter2pass"):
    email = _unique_email()
    resp = client.post(
        "/api/auth/register",
        json={"name": "Test User", "email": email, "password": password, "role": role},
    )
    return email, password, resp


def test_register_returns_tokens_and_public_user(client):
    email, _password, resp = _register(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user"]["email"] == email
    assert body["user"]["role"] == Role.DISPATCHER
    assert "password" not in body["user"] and "password_hash" not in body["user"]
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"


def test_register_rejects_admin_role(client):
    resp = client.post(
        "/api/auth/register",
        json={"name": "Sneaky", "email": _unique_email(), "password": "hunter2pass", "role": Role.ADMIN},
    )
    # Pydantic's Literal type rejects it before the handler's own check runs.
    assert resp.status_code == 422


def test_register_rejects_hospital_admin_role(client):
    resp = client.post(
        "/api/auth/register",
        json={"name": "Sneaky", "email": _unique_email(), "password": "hunter2pass", "role": Role.HOSPITAL_ADMIN},
    )
    assert resp.status_code == 422


def test_register_duplicate_email_conflicts(client):
    email, password, first = _register(client)
    assert first.status_code == 201
    second = client.post(
        "/api/auth/register",
        json={"name": "Dupe", "email": email, "password": password, "role": Role.DISPATCHER},
    )
    assert second.status_code == 409


def test_register_rejects_bad_email(client):
    resp = client.post(
        "/api/auth/register",
        json={"name": "Bad Email", "email": "not-an-email", "password": "hunter2pass", "role": Role.DISPATCHER},
    )
    assert resp.status_code == 422


def test_register_rejects_short_password(client):
    resp = client.post(
        "/api/auth/register",
        json={"name": "Short Pw", "email": _unique_email(), "password": "short", "role": Role.DISPATCHER},
    )
    assert resp.status_code == 422


def test_login_succeeds_with_correct_credentials(client):
    email, password, _reg = _register(client)
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == email


def test_login_rejects_wrong_password(client):
    email, _password, _reg = _register(client)
    resp = client.post("/api/auth/login", json={"email": email, "password": "totally-wrong-pw"})
    assert resp.status_code == 401


def test_login_rejects_unknown_email(client):
    resp = client.post("/api/auth/login", json={"email": _unique_email(), "password": "whatever123"})
    assert resp.status_code == 401


def test_me_requires_bearer_token(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_rejects_garbage_token(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401


def test_me_returns_caller_profile_with_valid_token(client):
    email, _password, reg = _register(client)
    token = reg.json()["access_token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == email


def test_refresh_token_cannot_be_used_as_access_token(client):
    _email, _password, reg = _register(client)
    refresh_token = reg.json()["refresh_token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh_token}"})
    assert resp.status_code == 401


def test_refresh_issues_new_working_access_token(client):
    _email, _password, reg = _register(client)
    refresh_token = reg.json()["refresh_token"]
    resp = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    new_access = resp.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200


def test_refresh_rejects_access_token(client):
    _email, _password, reg = _register(client)
    access_token = reg.json()["access_token"]
    resp = client.post("/api/auth/refresh", json={"refresh_token": access_token})
    assert resp.status_code == 401


def test_logout_requires_auth_and_returns_ok(client):
    _email, _password, reg = _register(client)
    token = reg.json()["access_token"]
    resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# RBAC dependency (auth_service.require_role) - exercised against a small
# standalone FastAPI app rather than two ad-hoc routes bolted onto the real
# `app`: `app` ends with `app.mount("/", StaticFiles(...))`, which - because
# Starlette matches routes in registration order and a root Mount matches
# every path - would swallow any route added to `app` after import time
# (exactly what a test-file-local `@app.get(...)` would be) before it ever
# reached our handler. A separate app has no such mount and needs none; it
# shares the real `get_current_user`/`require_role` dependencies (which load
# the user from the same DB via the same engine), so it's testing the real
# RBAC logic, just not routed through the production app object.
# ---------------------------------------------------------------------------
_probe_app = FastAPI()


@_probe_app.get("/admin-only")
def _admin_only_probe(user=Depends(require_role(Role.ADMIN))):
    return {"ok": True, "role": user.role}


@_probe_app.get("/dispatcher-or-admin")
def _dispatcher_or_admin_probe(user=Depends(require_role(Role.ADMIN, Role.DISPATCHER))):
    return {"ok": True, "role": user.role}


@pytest.fixture()
def probe_client():
    return TestClient(_probe_app)


def test_require_role_blocks_wrong_role(client, probe_client):
    _email, _password, reg = _register(client, role=Role.DISPATCHER)
    token = reg.json()["access_token"]
    resp = probe_client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_require_role_allows_matching_role(client, probe_client):
    _email, _password, reg = _register(client, role=Role.DISPATCHER)
    token = reg.json()["access_token"]
    resp = probe_client.get("/dispatcher-or-admin", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_require_role_blocks_missing_token(probe_client):
    resp = probe_client.get("/admin-only")
    assert resp.status_code == 401


def test_inactive_user_is_rejected(client):
    from db_session import get_db
    from models import User

    email, password, reg = _register(client)
    token = reg.json()["access_token"]

    db = next(get_db())
    try:
        user = db.query(User).filter(User.email == email).first()
        user.is_active = False
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401

    login_resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login_resp.status_code == 403
