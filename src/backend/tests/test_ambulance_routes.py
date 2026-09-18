"""API-level tests for /api/ambulances/* (ambulance_routes.py): creation
(ADMIN), operator assignment conflicts, status updates, and - the important
one - GPS location updates being restricted to the ambulance's own assigned
operator (never an ADMIN/DISPATCHER standing in, and never another
operator)."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth_service import create_access_token, hash_password
from db_session import get_db
from models import Ambulance, Role, User


@pytest.fixture()
def client():
    return TestClient(app)


def _make_user(role: str) -> tuple[User, str]:
    db = next(get_db())
    try:
        user = User(
            name=f"Test {role}", email=f"{role.lower()}-{uuid.uuid4().hex[:8]}@example-dev.test",
            password_hash=hash_password("irrelevant-not-used"), role=role, is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user, create_access_token(user)
    finally:
        db.close()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _vehicle_number() -> str:
    return f"Test AMB-{uuid.uuid4().hex[:6]}"


def _create_ambulance(client, admin_token, operator_id=None):
    payload = {"vehicle_number": _vehicle_number()}
    if operator_id:
        payload["operator_id"] = operator_id
    resp = client.post("/api/ambulances", json=payload, headers=_auth(admin_token))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def test_create_requires_admin(client):
    _user, dispatcher_token = _make_user(Role.DISPATCHER)
    resp = client.post("/api/ambulances", json={"vehicle_number": _vehicle_number()}, headers=_auth(dispatcher_token))
    assert resp.status_code == 403


def test_create_without_operator(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    ambulance = _create_ambulance(client, admin_token)
    assert ambulance["operator_id"] is None
    assert ambulance["status"] == "OFFLINE"
    assert ambulance["is_online"] is False


def test_create_rejects_non_operator_as_operator_id(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    _dispatcher, _t = _make_user(Role.DISPATCHER)
    resp = client.post(
        "/api/ambulances", json={"vehicle_number": _vehicle_number(), "operator_id": _dispatcher.id},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400


def test_create_rejects_operator_already_assigned(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, _t = _make_user(Role.AMBULANCE_OPERATOR)
    _create_ambulance(client, admin_token, operator_id=operator.id)
    resp = client.post(
        "/api/ambulances", json={"vehicle_number": _vehicle_number(), "operator_id": operator.id},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 409


def test_create_duplicate_vehicle_number_conflicts(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    vehicle_number = _vehicle_number()
    first = client.post("/api/ambulances", json={"vehicle_number": vehicle_number}, headers=_auth(admin_token))
    assert first.status_code == 201
    second = client.post("/api/ambulances", json={"vehicle_number": vehicle_number}, headers=_auth(admin_token))
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------
def test_list_requires_admin_or_dispatcher(client):
    operator, token = _make_user(Role.AMBULANCE_OPERATOR)
    resp = client.get("/api/ambulances", headers=_auth(token))
    assert resp.status_code == 403


def test_operator_can_get_own_ambulance(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, operator_token = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _create_ambulance(client, admin_token, operator_id=operator.id)

    resp = client.get(f"/api/ambulances/{ambulance['id']}", headers=_auth(operator_token))
    assert resp.status_code == 200


def test_operator_cannot_get_other_ambulance(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    ambulance = _create_ambulance(client, admin_token)  # unassigned
    other_operator, other_token = _make_user(Role.AMBULANCE_OPERATOR)

    resp = client.get(f"/api/ambulances/{ambulance['id']}", headers=_auth(other_token))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def test_status_update_by_own_operator(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, operator_token = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _create_ambulance(client, admin_token, operator_id=operator.id)

    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/status", json={"status": "AVAILABLE"}, headers=_auth(operator_token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "AVAILABLE"


def test_status_update_blocked_for_other_operator(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, _t = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _create_ambulance(client, admin_token, operator_id=operator.id)
    _other, other_token = _make_user(Role.AMBULANCE_OPERATOR)

    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/status", json={"status": "AVAILABLE"}, headers=_auth(other_token),
    )
    assert resp.status_code == 403


def test_status_update_rejects_invalid_value(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    ambulance = _create_ambulance(client, admin_token)
    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/status", json={"status": "ON_FIRE"}, headers=_auth(admin_token),
    )
    assert resp.status_code == 422


def test_dispatcher_can_update_status(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    ambulance = _create_ambulance(client, admin_token)
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/status", json={"status": "DISPATCHED"}, headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GPS location - the important RBAC boundary
# ---------------------------------------------------------------------------
def test_location_update_by_own_operator_succeeds(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, operator_token = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _create_ambulance(client, admin_token, operator_id=operator.id)

    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/location",
        json={"lat": 23.03, "lng": 72.58, "speed": 40, "heading": 90},
        headers=_auth(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lat"] == 23.03
    assert body["is_online"] is True
    assert body["last_location_update"] is not None


def test_location_update_blocked_for_other_operator(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, _t = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _create_ambulance(client, admin_token, operator_id=operator.id)
    _other, other_token = _make_user(Role.AMBULANCE_OPERATOR)

    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/location", json={"lat": 1.0, "lng": 1.0}, headers=_auth(other_token),
    )
    assert resp.status_code == 403


def test_location_update_blocked_for_admin_impersonating_operator(client):
    """Even an ADMIN cannot fake an operator's GPS - real GPS means the
    device itself is the only writer (Rule: no fake GPS in production)."""
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, _t = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _create_ambulance(client, admin_token, operator_id=operator.id)

    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/location", json={"lat": 1.0, "lng": 1.0}, headers=_auth(admin_token),
    )
    assert resp.status_code == 403


def test_location_update_blocked_for_unassigned_ambulance(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    ambulance = _create_ambulance(client, admin_token)  # no operator
    operator, operator_token = _make_user(Role.AMBULANCE_OPERATOR)

    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/location", json={"lat": 1.0, "lng": 1.0}, headers=_auth(operator_token),
    )
    assert resp.status_code == 403


def test_location_update_rejects_out_of_range_coordinates(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, operator_token = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _create_ambulance(client, admin_token, operator_id=operator.id)

    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/location", json={"lat": 200.0, "lng": 72.58}, headers=_auth(operator_token),
    )
    assert resp.status_code == 422


def test_location_update_rejects_null_island(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, operator_token = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _create_ambulance(client, admin_token, operator_id=operator.id)

    resp = client.post(
        f"/api/ambulances/{ambulance['id']}/location", json={"lat": 0.0, "lng": 0.0}, headers=_auth(operator_token),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Reassignment
# ---------------------------------------------------------------------------
def test_admin_can_reassign_operator(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    ambulance = _create_ambulance(client, admin_token)
    new_operator, _t = _make_user(Role.AMBULANCE_OPERATOR)

    resp = client.patch(
        f"/api/ambulances/{ambulance['id']}", json={"operator_id": new_operator.id}, headers=_auth(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["operator_id"] == new_operator.id


def test_reassign_rejects_operator_already_assigned_elsewhere(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    operator, _t = _make_user(Role.AMBULANCE_OPERATOR)
    _create_ambulance(client, admin_token, operator_id=operator.id)
    other_ambulance = _create_ambulance(client, admin_token)

    resp = client.patch(
        f"/api/ambulances/{other_ambulance['id']}", json={"operator_id": operator.id}, headers=_auth(admin_token),
    )
    assert resp.status_code == 409


def test_reassign_requires_admin(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    ambulance = _create_ambulance(client, admin_token)
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    resp = client.patch(
        f"/api/ambulances/{ambulance['id']}", json={"vehicle_number": "Test AMB-999"}, headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 403
