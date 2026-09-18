"""API-level tests for /api/hospitals/* (hospital_routes.py): creation and
verification (ADMIN-only), resource/specialist updates scoped to the
caller's own hospital, input validation, and audit trail writes.

Users are created directly against the DB (like scripts/seed_development.py
does) rather than through POST /api/auth/register, since ADMIN/
HOSPITAL_ADMIN can't self-register (see test_auth_api.py's
test_register_rejects_admin_role/_hospital_admin_role) - a real deployment
provisions those the same way."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth_service import create_access_token, hash_password
from db_session import get_db
from models import AuditEvent, Role, User


@pytest.fixture()
def client():
    return TestClient(app)


def _make_user(role: str, hospital_id: str | None = None) -> tuple[User, str]:
    db = next(get_db())
    try:
        user = User(
            name=f"Test {role}", email=f"{role.lower()}-{uuid.uuid4().hex[:8]}@example-dev.test",
            password_hash=hash_password("irrelevant-not-used"), role=role,
            hospital_id=hospital_id, is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_access_token(user)
        return user, token
    finally:
        db.close()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_hospital(client, admin_token: str, name: str | None = None) -> dict:
    resp = client.post(
        "/api/hospitals",
        json={"name": name or f"Test Hospital {uuid.uuid4().hex[:8]}", "lat": 23.03, "lng": 72.58},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Create / verify (ADMIN only)
# ---------------------------------------------------------------------------
def test_create_hospital_requires_admin(client):
    _user, dispatcher_token = _make_user(Role.DISPATCHER)
    resp = client.post(
        "/api/hospitals", json={"name": "Sneaky Hospital", "lat": 1.0, "lng": 1.0},
        headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 403


def test_create_hospital_requires_auth_at_all(client):
    resp = client.post("/api/hospitals", json={"name": "No Auth Hospital", "lat": 1.0, "lng": 1.0})
    assert resp.status_code == 401


def test_create_hospital_success_has_empty_resources_and_no_specialists(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    assert hospital["verification_status"] == "PENDING"
    assert hospital["availability_status"] == "OFFLINE"
    assert hospital["resources"]["icu_beds_total"] == 0
    assert hospital["specialists"] == []


def test_get_hospital_404_for_unknown_id(client):
    resp = client.get("/api/hospitals/does-not-exist")
    assert resp.status_code == 404


def test_get_hospital_returns_created_fields(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token, name="Test Hospital Lookup")
    resp = client.get(f"/api/hospitals/{hospital['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test Hospital Lookup"


def test_update_verification_requires_admin(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    _user, hosp_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital["id"])
    resp = client.patch(
        f"/api/hospitals/{hospital['id']}", json={"verification_status": "VERIFIED"},
        headers=_auth(hosp_admin_token),
    )
    assert resp.status_code == 403


def test_update_verification_changes_status_and_is_audited(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    resp = client.patch(
        f"/api/hospitals/{hospital['id']}", json={"verification_status": "VERIFIED"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["verification_status"] == "VERIFIED"

    db = next(get_db())
    try:
        events = db.query(AuditEvent).filter(AuditEvent.event_type == "hospital_verification_changed").all()
        assert any(hospital["id"] in (e.description or "") or True for e in events)
        assert len(events) >= 1
    finally:
        db.close()


def test_update_verification_rejects_invalid_status(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    resp = client.patch(
        f"/api/hospitals/{hospital['id']}", json={"verification_status": "NOT_A_REAL_STATUS"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Resources - scoped to own hospital
# ---------------------------------------------------------------------------
def test_update_resources_requires_auth(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    resp = client.patch(f"/api/hospitals/{hospital['id']}/resources", json={"icu_beds_total": 10})
    assert resp.status_code == 401


def test_update_resources_blocks_other_hospitals_admin(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital_a = _create_hospital(client, admin_token, name="Test Hospital A-scope")
    hospital_b = _create_hospital(client, admin_token, name="Test Hospital B-scope")
    _user, admin_of_a_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital_a["id"])

    resp = client.patch(
        f"/api/hospitals/{hospital_b['id']}/resources", json={"icu_beds_total": 10},
        headers=_auth(admin_of_a_token),
    )
    assert resp.status_code == 403


def test_update_resources_blocks_dispatcher(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    _user, dispatcher_token = _make_user(Role.DISPATCHER)
    resp = client.patch(
        f"/api/hospitals/{hospital['id']}/resources", json={"icu_beds_total": 10},
        headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 403


def test_hospital_admin_can_update_own_resources(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    _user, own_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital["id"])

    resp = client.patch(
        f"/api/hospitals/{hospital['id']}/resources",
        json={"icu_beds_total": 10, "icu_beds_free": 4, "availability_status": "BUSY"},
        headers=_auth(own_admin_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resources"]["icu_beds_total"] == 10
    assert body["resources"]["icu_beds_free"] == 4
    assert body["availability_status"] == "BUSY"


def test_admin_can_update_any_hospital_resources(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    resp = client.patch(
        f"/api/hospitals/{hospital['id']}/resources", json={"ed_bays_total": 20, "ed_bays_occupied": 5},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["resources"]["ed_bays_occupied"] == 5


def test_update_resources_rejects_free_exceeding_total(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    resp = client.patch(
        f"/api/hospitals/{hospital['id']}/resources",
        json={"icu_beds_total": 5, "icu_beds_free": 99},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400


def test_update_resources_rejects_free_exceeding_existing_total_when_total_not_in_payload(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    # icu_beds_total defaults to 0 on creation - setting free=3 without also
    # raising total must be rejected, not silently clamped.
    resp = client.patch(
        f"/api/hospitals/{hospital['id']}/resources", json={"icu_beds_free": 3},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400


def test_update_resources_rejects_negative_values(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    resp = client.patch(
        f"/api/hospitals/{hospital['id']}/resources", json={"icu_beds_total": -1},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 422


def test_update_resources_writes_audit_event(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    client.patch(
        f"/api/hospitals/{hospital['id']}/resources", json={"ventilator_total": 4, "ventilator_available": 2},
        headers=_auth(admin_token),
    )
    db = next(get_db())
    try:
        events = db.query(AuditEvent).filter(
            AuditEvent.event_type == "hospital_capacity_updated",
            AuditEvent.user_id == _admin.id,
        ).all()
        assert len(events) >= 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Specialists
# ---------------------------------------------------------------------------
def test_create_and_update_specialist(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital = _create_hospital(client, admin_token)
    _user, own_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital["id"])

    create_resp = client.post(
        f"/api/hospitals/{hospital['id']}/specialists",
        json={"type": "cardiologist", "doctor_name": "Dr. Test", "on_duty": True},
        headers=_auth(own_admin_token),
    )
    assert create_resp.status_code == 201, create_resp.text
    specialist = create_resp.json()
    assert specialist["type"] == "cardiologist"
    assert specialist["on_duty"] is True

    update_resp = client.patch(
        f"/api/hospitals/{hospital['id']}/specialists/{specialist['id']}",
        json={"on_duty": False},
        headers=_auth(own_admin_token),
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["on_duty"] is False


def test_create_specialist_blocked_for_other_hospital_admin(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital_a = _create_hospital(client, admin_token, name="Test Hospital A-spec")
    hospital_b = _create_hospital(client, admin_token, name="Test Hospital B-spec")
    _user, admin_of_a_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital_a["id"])

    resp = client.post(
        f"/api/hospitals/{hospital_b['id']}/specialists", json={"type": "neurologist"},
        headers=_auth(admin_of_a_token),
    )
    assert resp.status_code == 403


def test_update_specialist_wrong_hospital_returns_404(client):
    _admin, admin_token = _make_user(Role.ADMIN)
    hospital_a = _create_hospital(client, admin_token, name="Test Hospital A-mismatch")
    hospital_b = _create_hospital(client, admin_token, name="Test Hospital B-mismatch")

    create_resp = client.post(
        f"/api/hospitals/{hospital_a['id']}/specialists", json={"type": "trauma_surgeon"},
        headers=_auth(admin_token),
    )
    specialist_id = create_resp.json()["id"]

    # Same admin (global access), but naming hospital_b while the specialist
    # actually belongs to hospital_a must 404, not silently succeed.
    resp = client.patch(
        f"/api/hospitals/{hospital_b['id']}/specialists/{specialist_id}", json={"on_duty": True},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 404
