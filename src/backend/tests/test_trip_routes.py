"""API-level tests for the core coordination workflow (trip_routes.py):
recommendation, hospital request/accept/decline, trip dispatch/lifecycle/
reroute/cancel, and - the important one - that two emergencies competing
for the same hospital's last ICU bed can never both succeed."""
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

import reservation_service
from app import app
from auth_service import create_access_token, hash_password
from db_session import get_db
from models import (
    Ambulance, AmbulanceStatus, Hospital, HospitalAvailability,
    HospitalResources, HospitalSpecialist, HospitalVerificationStatus,
    Notification, Role, User,
)

CARDIAC_SYMPTOMS = "55yo male, crushing chest pain radiating to left arm, sweating"
NON_CRITICAL_SYMPTOMS = "small laceration on the hand, minor cut, mild pain"


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
        return user, create_access_token(user)
    finally:
        db.close()


def _make_hospital(*, icu_total=5, icu_free=5, ed_total=10, ed_occupied=0, cardiologist_on_duty=True) -> Hospital:
    db = next(get_db())
    try:
        hospital = Hospital(
            name=f"Test Hospital {uuid.uuid4().hex[:8]}", lat=23.03, lng=72.58,
            verification_status=HospitalVerificationStatus.VERIFIED,
            availability_status=HospitalAvailability.ONLINE, is_active=True,
        )
        db.add(hospital)
        db.flush()
        db.add(HospitalResources(
            hospital_id=hospital.id, icu_beds_total=icu_total, icu_beds_free=icu_free,
            ward_beds_total=20, ward_beds_free=10, ed_bays_total=ed_total, ed_bays_occupied=ed_occupied,
        ))
        db.add(HospitalSpecialist(hospital_id=hospital.id, type="cardiologist", on_duty=cardiologist_on_duty))
        db.commit()
        db.refresh(hospital)
        return hospital
    finally:
        db.close()


def _make_ambulance(operator_id: str | None = None) -> Ambulance:
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


def _create_emergency(client, token, symptoms=CARDIAC_SYMPTOMS) -> dict:
    resp = client.post(
        "/api/emergencies", json={"symptoms": symptoms, "lat": 23.03, "lng": 72.58}, headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _full_flow_to_hospital_accepted(client, dispatcher_token, hospital_id):
    """Helper: create a cardiac emergency, get recommendation, request the
    given hospital, and have it accept - returns (emergency, hospital_request)."""
    emergency = _create_emergency(client, dispatcher_token)
    rec = client.get(f"/api/emergencies/{emergency['id']}/recommendation", headers=_auth(dispatcher_token))
    assert rec.status_code == 200, rec.text

    req_resp = client.post(
        "/api/hospital-requests", json={"emergency_id": emergency["id"], "hospital_id": hospital_id},
        headers=_auth(dispatcher_token),
    )
    assert req_resp.status_code == 201, req_resp.text
    hospital_request = req_resp.json()

    _hosp_admin, hosp_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital_id)
    accept_resp = client.post(f"/api/hospital-requests/{hospital_request['id']}/accept", headers=_auth(hosp_admin_token))
    assert accept_resp.status_code == 200, accept_resp.text
    return emergency, accept_resp.json()


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------
def test_recommendation_requires_dispatcher_or_admin(client):
    _op, op_token = _make_user(Role.AMBULANCE_OPERATOR)
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    _make_hospital()
    emergency = _create_emergency(client, dispatcher_token)

    resp = client.get(f"/api/emergencies/{emergency['id']}/recommendation", headers=_auth(op_token))
    assert resp.status_code == 403


def test_recommendation_recommends_hospital_with_free_icu_over_full_one(client):
    # Assertions only check these two fixture hospitals' own viability/score,
    # never "is the GLOBAL top pick" - the test DB accumulates hospitals
    # across the whole suite run (no per-test isolation, see conftest.py),
    # so other tests' fixture hospitals are legitimate competing candidates
    # too; hospital_service.hospitals_for_recommendation() intentionally has
    # no geographic scoping (matches recommendation_engine.py's own
    # "demo radius = all, small city" design), which is correct in
    # production (every real hospital is a candidate) but means this test
    # can't assume it's the only two hospitals in the database.
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    full_hospital = _make_hospital(icu_total=2, icu_free=0)
    open_hospital = _make_hospital(icu_total=2, icu_free=2)
    emergency = _create_emergency(client, dispatcher_token)  # cardiac -> requires ICU

    resp = client.get(f"/api/emergencies/{emergency['id']}/recommendation", headers=_auth(dispatcher_token))
    assert resp.status_code == 200
    body = resp.json()
    full_candidate = next(c for c in body["candidates"] if c["hospital_id"] == full_hospital.id)
    open_candidate = next(c for c in body["candidates"] if c["hospital_id"] == open_hospital.id)
    assert full_candidate["viable"] is False
    assert open_candidate["viable"] is True


def test_recommendation_404_for_unknown_emergency(client):
    _dispatcher, token = _make_user(Role.DISPATCHER)
    resp = client.get("/api/emergencies/does-not-exist/recommendation", headers=_auth(token))
    assert resp.status_code == 404


def test_recommendation_ignores_unverified_hospitals(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    db = next(get_db())
    try:
        pending_hospital = Hospital(
            name="Test Unverified Hospital", lat=23.03, lng=72.58,
            verification_status=HospitalVerificationStatus.PENDING, is_active=True,
        )
        db.add(pending_hospital)
        db.flush()
        db.add(HospitalResources(hospital_id=pending_hospital.id, icu_beds_total=5, icu_beds_free=5, ed_bays_total=5))
        db.commit()
    finally:
        db.close()

    emergency = _create_emergency(client, dispatcher_token)
    resp = client.get(f"/api/emergencies/{emergency['id']}/recommendation", headers=_auth(dispatcher_token))
    # No verified hospitals exist yet in this test's isolated set of fixtures
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        ids = [c["hospital_id"] for c in resp.json()["candidates"]]
        assert pending_hospital.id not in ids


# ---------------------------------------------------------------------------
# Hospital request / accept / decline
# ---------------------------------------------------------------------------
def test_create_hospital_request_requires_awaiting_hospital_status(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    emergency, _accepted = _full_flow_to_hospital_accepted(client, dispatcher_token, hospital.id)

    # Already HOSPITAL_ACCEPTED - requesting again should be rejected
    resp = client.post(
        "/api/hospital-requests", json={"emergency_id": emergency["id"], "hospital_id": hospital.id},
        headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 409


def test_create_hospital_request_rejects_unverified_hospital(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    db = next(get_db())
    try:
        hospital = Hospital(name="Test Pending", lat=1, lng=1, verification_status=HospitalVerificationStatus.PENDING, is_active=True)
        db.add(hospital)
        db.commit()
        db.refresh(hospital)
    finally:
        db.close()
    emergency = _create_emergency(client, dispatcher_token)

    resp = client.post(
        "/api/hospital-requests", json={"emergency_id": emergency["id"], "hospital_id": hospital.id},
        headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 400


def test_accept_requires_own_hospital_admin_or_admin(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    other_hospital = _make_hospital()
    emergency = _create_emergency(client, dispatcher_token)
    req_resp = client.post(
        "/api/hospital-requests", json={"emergency_id": emergency["id"], "hospital_id": hospital.id},
        headers=_auth(dispatcher_token),
    )
    hospital_request = req_resp.json()

    _other_admin, other_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=other_hospital.id)
    resp = client.post(f"/api/hospital-requests/{hospital_request['id']}/accept", headers=_auth(other_admin_token))
    assert resp.status_code == 403


def test_accept_reserves_icu_and_ed(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital(icu_total=3, icu_free=3, ed_total=5, ed_occupied=0)
    _emergency, accepted = _full_flow_to_hospital_accepted(client, dispatcher_token, hospital.id)
    assert accepted["status"] == "ACCEPTED"

    hosp = client.get(f"/api/hospitals/{hospital.id}").json()
    assert hosp["resources"]["icu_beds_free"] == 2  # 3 -> 2
    assert hosp["resources"]["ed_bays_occupied"] == 1  # 0 -> 1


def test_accept_fails_with_409_when_no_icu_capacity(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital(icu_total=1, icu_free=0)  # no ICU free
    emergency = _create_emergency(client, dispatcher_token)  # cardiac -> requires ICU
    req_resp = client.post(
        "/api/hospital-requests", json={"emergency_id": emergency["id"], "hospital_id": hospital.id},
        headers=_auth(dispatcher_token),
    )
    hospital_request = req_resp.json()
    _hosp_admin, hosp_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital.id)

    resp = client.post(f"/api/hospital-requests/{hospital_request['id']}/accept", headers=_auth(hosp_admin_token))
    assert resp.status_code == 409


def test_accept_twice_is_rejected(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    emergency = _create_emergency(client, dispatcher_token)
    req_resp = client.post(
        "/api/hospital-requests", json={"emergency_id": emergency["id"], "hospital_id": hospital.id},
        headers=_auth(dispatcher_token),
    )
    hospital_request = req_resp.json()
    _hosp_admin, hosp_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital.id)
    client.post(f"/api/hospital-requests/{hospital_request['id']}/accept", headers=_auth(hosp_admin_token))

    resp = client.post(f"/api/hospital-requests/{hospital_request['id']}/accept", headers=_auth(hosp_admin_token))
    assert resp.status_code == 409


def test_decline_returns_emergency_to_awaiting_hospital(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    emergency = _create_emergency(client, dispatcher_token)
    req_resp = client.post(
        "/api/hospital-requests", json={"emergency_id": emergency["id"], "hospital_id": hospital.id},
        headers=_auth(dispatcher_token),
    )
    hospital_request = req_resp.json()
    _hosp_admin, hosp_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital.id)

    resp = client.post(
        f"/api/hospital-requests/{hospital_request['id']}/decline", json={"reason": "no beds"}, headers=_auth(hosp_admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "DECLINED"

    emergency_after = client.get(f"/api/emergencies/{emergency['id']}", headers=_auth(dispatcher_token)).json()
    assert emergency_after["status"] == "AWAITING_HOSPITAL"


def test_dispatcher_gets_notified_on_accept(client):
    dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    _full_flow_to_hospital_accepted(client, dispatcher_token, hospital.id)

    resp = client.get("/api/notifications", headers=_auth(dispatcher_token))
    assert resp.status_code == 200
    assert any(n["type"] == "HOSPITAL_ACCEPTED" for n in resp.json())


# ---------------------------------------------------------------------------
# Trip dispatch + lifecycle
# ---------------------------------------------------------------------------
def test_create_trip_requires_hospital_accepted(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    emergency = _create_emergency(client, dispatcher_token)  # not yet requested/accepted
    ambulance = _make_ambulance()

    resp = client.post(
        "/api/trips", json={"emergency_id": emergency["id"], "ambulance_id": ambulance.id}, headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 409


def test_create_trip_requires_available_ambulance(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    emergency, _accepted = _full_flow_to_hospital_accepted(client, dispatcher_token, hospital.id)
    ambulance = _make_ambulance()
    ambulance_db = next(get_db())
    try:
        a = ambulance_db.get(Ambulance, ambulance.id)
        a.status = AmbulanceStatus.OFFLINE
        ambulance_db.commit()
    finally:
        ambulance_db.close()

    resp = client.post(
        "/api/trips", json={"emergency_id": emergency["id"], "ambulance_id": ambulance.id}, headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 409


def _dispatch_trip(client, dispatcher_token, hospital_id):
    emergency, _accepted = _full_flow_to_hospital_accepted(client, dispatcher_token, hospital_id)
    operator, operator_token = _make_user(Role.AMBULANCE_OPERATOR)
    ambulance = _make_ambulance(operator_id=operator.id)
    trip_resp = client.post(
        "/api/trips", json={"emergency_id": emergency["id"], "ambulance_id": ambulance.id}, headers=_auth(dispatcher_token),
    )
    assert trip_resp.status_code == 201, trip_resp.text
    return emergency, trip_resp.json(), operator_token, ambulance


def test_full_lifecycle_start_arrive_complete(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    emergency, trip, operator_token, ambulance = _dispatch_trip(client, dispatcher_token, hospital.id)
    assert trip["status"] == "DISPATCHED"
    assert trip["dest_hospital_id"] == hospital.id

    start_resp = client.post(f"/api/trips/{trip['id']}/start", headers=_auth(operator_token))
    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "EN_ROUTE"

    arrive_resp = client.post(f"/api/trips/{trip['id']}/arrive", headers=_auth(operator_token))
    assert arrive_resp.status_code == 200
    assert arrive_resp.json()["status"] == "ARRIVED"

    _hosp_admin, hosp_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital.id)
    complete_resp = client.post(f"/api/trips/{trip['id']}/complete", headers=_auth(hosp_admin_token))
    assert complete_resp.status_code == 200
    assert complete_resp.json()["status"] == "COMPLETED"

    ambulance_after = client.get(f"/api/ambulances/{ambulance.id}", headers=_auth(dispatcher_token)).json()
    assert ambulance_after["status"] == "AVAILABLE"

    emergency_after = client.get(f"/api/emergencies/{emergency['id']}", headers=_auth(dispatcher_token)).json()
    assert emergency_after["status"] == "COMPLETED"


def test_start_blocked_for_other_operator(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    _emergency, trip, _operator_token, _ambulance = _dispatch_trip(client, dispatcher_token, hospital.id)
    _other_op, other_token = _make_user(Role.AMBULANCE_OPERATOR)

    resp = client.post(f"/api/trips/{trip['id']}/start", headers=_auth(other_token))
    assert resp.status_code == 403


def test_complete_requires_destination_hospital_admin(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    other_hospital = _make_hospital()
    _emergency, trip, operator_token, _ambulance = _dispatch_trip(client, dispatcher_token, hospital.id)
    client.post(f"/api/trips/{trip['id']}/start", headers=_auth(operator_token))
    client.post(f"/api/trips/{trip['id']}/arrive", headers=_auth(operator_token))

    _wrong_admin, wrong_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=other_hospital.id)
    resp = client.post(f"/api/trips/{trip['id']}/complete", headers=_auth(wrong_admin_token))
    assert resp.status_code == 403


def test_complete_before_arrived_is_rejected(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    _emergency, trip, _operator_token, _ambulance = _dispatch_trip(client, dispatcher_token, hospital.id)
    _hosp_admin, hosp_admin_token = _make_user(Role.HOSPITAL_ADMIN, hospital_id=hospital.id)

    resp = client.post(f"/api/trips/{trip['id']}/complete", headers=_auth(hosp_admin_token))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Reroute
# ---------------------------------------------------------------------------
def test_reroute_moves_reservation_to_new_hospital(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    old_hospital = _make_hospital(icu_total=2, icu_free=2)
    new_hospital = _make_hospital(icu_total=2, icu_free=2)
    _emergency, trip, operator_token, _ambulance = _dispatch_trip(client, dispatcher_token, old_hospital.id)
    client.post(f"/api/trips/{trip['id']}/start", headers=_auth(operator_token))

    resp = client.post(
        f"/api/trips/{trip['id']}/reroute", json={"new_hospital_id": new_hospital.id}, headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dest_hospital_id"] == new_hospital.id

    old = client.get(f"/api/hospitals/{old_hospital.id}").json()
    new = client.get(f"/api/hospitals/{new_hospital.id}").json()
    assert old["resources"]["icu_beds_free"] == 2  # released back
    assert new["resources"]["icu_beds_free"] == 1  # now reserved


def test_reroute_fails_if_new_hospital_has_no_capacity_and_keeps_old_reservation(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    old_hospital = _make_hospital(icu_total=2, icu_free=2)
    full_hospital = _make_hospital(icu_total=1, icu_free=0)
    _emergency, trip, operator_token, _ambulance = _dispatch_trip(client, dispatcher_token, old_hospital.id)
    client.post(f"/api/trips/{trip['id']}/start", headers=_auth(operator_token))

    resp = client.post(
        f"/api/trips/{trip['id']}/reroute", json={"new_hospital_id": full_hospital.id}, headers=_auth(dispatcher_token),
    )
    assert resp.status_code == 409

    old = client.get(f"/api/hospitals/{old_hospital.id}").json()
    assert old["resources"]["icu_beds_free"] == 1  # still held, untouched


def test_reroute_requires_dispatcher_or_admin(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    old_hospital = _make_hospital()
    new_hospital = _make_hospital()
    _emergency, trip, operator_token, _ambulance = _dispatch_trip(client, dispatcher_token, old_hospital.id)
    client.post(f"/api/trips/{trip['id']}/start", headers=_auth(operator_token))

    resp = client.post(
        f"/api/trips/{trip['id']}/reroute", json={"new_hospital_id": new_hospital.id}, headers=_auth(operator_token),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
def test_cancel_releases_reservation_and_frees_ambulance(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital(icu_total=2, icu_free=2)
    _emergency, trip, _operator_token, ambulance = _dispatch_trip(client, dispatcher_token, hospital.id)

    resp = client.post(f"/api/trips/{trip['id']}/cancel", headers=_auth(dispatcher_token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"

    hosp = client.get(f"/api/hospitals/{hospital.id}").json()
    assert hosp["resources"]["icu_beds_free"] == 2  # released back

    ambulance_after = client.get(f"/api/ambulances/{ambulance.id}", headers=_auth(dispatcher_token)).json()
    assert ambulance_after["status"] == "AVAILABLE"


def test_cancel_after_arrived_is_rejected(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital()
    _emergency, trip, operator_token, _ambulance = _dispatch_trip(client, dispatcher_token, hospital.id)
    client.post(f"/api/trips/{trip['id']}/start", headers=_auth(operator_token))
    client.post(f"/api/trips/{trip['id']}/arrive", headers=_auth(operator_token))

    resp = client.post(f"/api/trips/{trip['id']}/cancel", headers=_auth(dispatcher_token))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# THE concurrency requirement: last ICU bed, two competing emergencies
# ---------------------------------------------------------------------------
def test_two_concurrent_reservations_for_the_last_icu_bed_only_one_succeeds(client):
    _dispatcher, dispatcher_token = _make_user(Role.DISPATCHER)
    hospital = _make_hospital(icu_total=1, icu_free=1, ed_total=10, ed_occupied=0)
    emergency_a = _create_emergency(client, dispatcher_token)
    emergency_b = _create_emergency(client, dispatcher_token)

    results = [None, None]

    def attempt(index, emergency_id):
        db = next(get_db())
        try:
            results[index] = reservation_service.reserve(db, hospital.id, emergency_id, requires_icu=True)
        finally:
            db.close()

    t1 = threading.Thread(target=attempt, args=(0, emergency_a["id"]))
    t2 = threading.Thread(target=attempt, args=(1, emergency_b["id"]))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    successes = [r for r in results if r is not None]
    failures = [r for r in results if r is None]
    assert len(successes) == 1, f"expected exactly one reservation to succeed, got: {results}"
    assert len(failures) == 1

    hosp = client.get(f"/api/hospitals/{hospital.id}").json()
    assert hosp["resources"]["icu_beds_free"] == 0  # never went negative, never double-booked
