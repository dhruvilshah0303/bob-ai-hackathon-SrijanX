"""API-level tests for /api/emergencies/* (emergency_routes.py): creation +
synchronous AI triage (keyword-fallback path, since no WATSONX_API_KEY is
set in the test environment - see triage_service.py), vitals, cancellation,
RBAC, and the AiDecision/AuditEvent trail each write leaves behind."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth_service import create_access_token, hash_password
from db_session import get_db
from models import AiDecision, AuditEvent, Role, User


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


CARDIAC_SYMPTOMS = "55yo male, crushing chest pain radiating to left arm, sweating"


def _create_emergency(client, token, **overrides):
    payload = {
        "symptoms": CARDIAC_SYMPTOMS,
        "lat": 23.03, "lng": 72.58,
        "emergency_type": "medical",
    }
    payload.update(overrides)
    return client.post("/api/emergencies", json=payload, headers=_auth(token))


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
def test_create_requires_auth(client):
    resp = client.post("/api/emergencies", json={"symptoms": CARDIAC_SYMPTOMS, "lat": 1.0, "lng": 1.0})
    assert resp.status_code == 401


def test_create_blocks_ambulance_operator(client):
    _user, token = _make_user(Role.AMBULANCE_OPERATOR)
    resp = _create_emergency(client, token)
    assert resp.status_code == 403


def test_create_blocks_hospital_admin(client):
    _user, token = _make_user(Role.HOSPITAL_ADMIN)
    resp = _create_emergency(client, token)
    assert resp.status_code == 403


def test_dispatcher_can_create(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = _create_emergency(client, token)
    assert resp.status_code == 201, resp.text


def test_admin_can_create(client):
    _user, token = _make_user(Role.ADMIN)
    resp = _create_emergency(client, token)
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Creation + triage content
# ---------------------------------------------------------------------------
def test_create_rejects_empty_symptoms(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = _create_emergency(client, token, symptoms="   ")
    assert resp.status_code == 422


def test_cardiac_symptoms_classify_as_cardiac_with_icu_and_cardiologist(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = _create_emergency(client, token, symptoms=CARDIAC_SYMPTOMS)
    body = resp.json()
    assert body["condition_code"] == "cardiac"
    assert body["triage"]["requires_icu"] is True
    assert body["triage"]["requires_ed"] is True
    assert "cardiologist" in body["triage"]["required_specializations"]
    assert body["triage"]["source"] == "keyword_fallback"  # no watsonx configured in tests
    assert body["status"] == "AWAITING_HOSPITAL"


def test_non_critical_symptoms_do_not_require_icu(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = _create_emergency(client, token, symptoms="small laceration on the hand, minor cut, mild pain")
    body = resp.json()
    assert body["condition_code"] == "non_critical"
    assert body["triage"]["requires_icu"] is False


def test_create_with_patient_info_returns_patient(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = _create_emergency(client, token, patient={"name": "Jane Doe", "age": 61, "gender": "female"})
    body = resp.json()
    assert body["patient"]["name"] == "Jane Doe"
    assert body["patient"]["age"] == 61


def test_create_with_vitals_returns_vitals(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = _create_emergency(client, token, vitals={"heart_rate": 130, "spo2": 91, "conscious": True})
    body = resp.json()
    assert len(body["vitals"]) == 1
    assert body["vitals"][0]["heart_rate"] == 130


def test_create_rejects_out_of_range_vitals(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = _create_emergency(client, token, vitals={"spo2": 250})
    assert resp.status_code == 422


def test_create_writes_ai_decision_and_audit_event(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = _create_emergency(client, token)
    emergency_id = resp.json()["id"]

    db = next(get_db())
    try:
        decisions = db.query(AiDecision).filter(AiDecision.emergency_id == emergency_id).all()
        assert len(decisions) == 1
        assert decisions[0].decision_type == "triage"

        events = db.query(AuditEvent).filter(
            AuditEvent.emergency_id == emergency_id, AuditEvent.event_type == "emergency_created",
        ).all()
        assert len(events) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Read / list
# ---------------------------------------------------------------------------
def test_get_unknown_emergency_404(client):
    _user, token = _make_user(Role.DISPATCHER)
    resp = client.get("/api/emergencies/does-not-exist", headers=_auth(token))
    assert resp.status_code == 404


def test_get_returns_created_emergency(client):
    _user, token = _make_user(Role.DISPATCHER)
    created = _create_emergency(client, token).json()
    resp = client.get(f"/api/emergencies/{created['id']}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_list_requires_dispatcher_or_admin(client):
    _user, token = _make_user(Role.AMBULANCE_OPERATOR)
    resp = client.get("/api/emergencies", headers=_auth(token))
    assert resp.status_code == 403


def test_list_includes_created_emergency(client):
    _user, token = _make_user(Role.DISPATCHER)
    created = _create_emergency(client, token).json()
    resp = client.get("/api/emergencies", headers=_auth(token))
    assert resp.status_code == 200
    assert any(e["id"] == created["id"] for e in resp.json())


# ---------------------------------------------------------------------------
# Vitals follow-up
# ---------------------------------------------------------------------------
def test_add_vitals_appends_a_second_reading(client):
    _user, token = _make_user(Role.DISPATCHER)
    created = _create_emergency(client, token, vitals={"heart_rate": 100}).json()
    resp = client.post(
        f"/api/emergencies/{created['id']}/vitals", json={"heart_rate": 140, "spo2": 88},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert len(resp.json()["vitals"]) == 2
    assert resp.json()["vitals"][-1]["heart_rate"] == 140


def test_cannot_add_vitals_to_cancelled_emergency(client):
    _user, token = _make_user(Role.DISPATCHER)
    created = _create_emergency(client, token).json()
    client.post(f"/api/emergencies/{created['id']}/cancel", headers=_auth(token))
    resp = client.post(f"/api/emergencies/{created['id']}/vitals", json={"heart_rate": 90}, headers=_auth(token))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def test_cancel_transitions_to_cancelled(client):
    _user, token = _make_user(Role.DISPATCHER)
    created = _create_emergency(client, token).json()
    resp = client.post(f"/api/emergencies/{created['id']}/cancel", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


def test_cancel_twice_is_rejected(client):
    _user, token = _make_user(Role.DISPATCHER)
    created = _create_emergency(client, token).json()
    client.post(f"/api/emergencies/{created['id']}/cancel", headers=_auth(token))
    resp = client.post(f"/api/emergencies/{created['id']}/cancel", headers=_auth(token))
    assert resp.status_code == 409


def test_cancel_requires_dispatcher_or_admin(client):
    _user, token = _make_user(Role.DISPATCHER)
    created = _create_emergency(client, token).json()
    _other_user, operator_token = _make_user(Role.AMBULANCE_OPERATOR)
    resp = client.post(f"/api/emergencies/{created['id']}/cancel", headers=_auth(operator_token))
    assert resp.status_code == 403
