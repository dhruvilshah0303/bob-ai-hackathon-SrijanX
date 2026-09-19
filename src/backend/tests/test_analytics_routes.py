"""API-level tests for GET /api/analytics (analytics_routes.py): real
counts derived from actual HospitalRequest/Trip/TripEvent rows, RBAC, and
that the numbers genuinely move when the underlying data does."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth_service import create_access_token, hash_password
from db_session import get_db
from models import (
    Ambulance, AmbulanceStatus, Hospital, HospitalAvailability,
    HospitalResources, HospitalVerificationStatus, Role, User,
)

CARDIAC_SYMPTOMS = "55yo male, crushing chest pain radiating to left arm, sweating"


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


def _make_hospital(icu_free=5) -> Hospital:
    db = next(get_db())
    try:
        hospital = Hospital(
            name=f"Test Hospital {uuid.uuid4().hex[:8]}", lat=23.03, lng=72.58,
            verification_status=HospitalVerificationStatus.VERIFIED, availability_status=HospitalAvailability.ONLINE, is_active=True,
        )
        db.add(hospital)
        db.flush()
        db.add(HospitalResources(hospital_id=hospital.id, icu_beds_total=5, icu_beds_free=icu_free, ed_bays_total=10))
        db.commit()
        db.refresh(hospital)
        return hospital
    finally:
        db.close()


def _make_ambulance(operator_id=None) -> Ambulance:
    db = next(get_db())
    try:
        ambulance = Ambulance(vehicle_number=f"Test AMB-{uuid.uuid4().hex[:6]}", operator_id=operator_id, status=AmbulanceStatus.AVAILABLE)
        db.add(ambulance)
        db.commit()
        db.refresh(ambulance)
        return ambulance
    finally:
        db.close()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_analytics_requires_dispatcher_or_admin(client):
    _op, op_token = _make_user(Role.AMBULANCE_OPERATOR)
    resp = client.get("/api/analytics", headers=_auth(op_token))
    assert resp.status_code == 403


def test_analytics_returns_well_formed_response(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    resp = client.get("/api/analytics", headers=_auth(dispatcher_token))
    assert resp.status_code == 200
    body = resp.json()
    assert "hospital_acceptance_rate" in body
    assert "total_trips" in body
    assert 0.0 <= body["hospital_acceptance_rate"] <= 1.0


def test_completed_trip_increments_completed_count_and_reroute_count(client):
    dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    other_hospital = _make_hospital()

    before = client.get("/api/analytics", headers=_auth(dispatcher_token)).json()

    emergency = client.post(
        "/api/emergencies", json={"symptoms": CARDIAC_SYMPTOMS, "lat": 23.03, "lng": 72.58},
        headers=_auth(dispatcher_token),
    ).json()
    hr = client.post(
        "/api/hospital-requests", json={"emergency_id": emergency["id"], "hospital_id": hospital.id},
        headers=_auth(dispatcher_token),
    ).json()
    _hosp_admin, hosp_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital.id)
    client.post(f"/api/hospital-requests/{hr['id']}/accept", headers=_auth(hosp_admin_token))

    operator, operator_token = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _make_ambulance(operator_id=operator.id)
    trip = client.post(
        "/api/trips", json={"emergency_id": emergency["id"], "ambulance_id": ambulance.id},
        headers=_auth(dispatcher_token),
    ).json()
    client.post(f"/api/trips/{trip['id']}/start", headers=_auth(operator_token))

    # Reroute once, then complete at the new hospital.
    client.post(f"/api/trips/{trip['id']}/reroute", json={"new_hospital_id": other_hospital.id}, headers=_auth(dispatcher_token))
    client.post(f"/api/trips/{trip['id']}/arrive", headers=_auth(operator_token))
    _other_admin, other_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=other_hospital.id)
    client.post(f"/api/trips/{trip['id']}/complete", headers=_auth(other_admin_token))

    after = client.get("/api/analytics", headers=_auth(dispatcher_token)).json()
    assert after["completed_trips"] == before["completed_trips"] + 1
    assert after["reroute_count"] == before["reroute_count"] + 1
    assert after["hospital_requests_accepted"] == before["hospital_requests_accepted"] + 1
    assert after["average_dispatch_to_arrival_minutes"] is not None
