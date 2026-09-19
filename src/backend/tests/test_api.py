"""
API-level tests using FastAPI's TestClient (no real server/network needed).

These use the `bypass_auth` fixture (see conftest.py) so they can focus on
business logic - trip lifecycle, capacity reservation, recommendation
generation, pre-alert accept/reject, rerouting - without hand-carrying a
real session token through every call. Real login/RBAC has its own tests in
test_auth.py.

Run with:  cd backend && python3 -m pytest tests/ -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import app as app_module  # noqa: E402

client = TestClient(app_module.app)

pytestmark = pytest.mark.usefixtures("bypass_auth")

DEFAULT_INCIDENT = {"lat": 23.03, "lng": 72.56, "label": "Test incident"}


@pytest.fixture(autouse=True)
def reset_state():
    # No more POST /api/reset (removed - a real coordination system should
    # never expose a button that wipes live trip/audit history). Tests get
    # their own clean slate by calling the same reset the old endpoint used
    # to, directly, bypassing HTTP.
    app_module.state.reset(wipe_history=True)
    yield


def create_trip(**kwargs):
    kwargs.setdefault("incident_location", DEFAULT_INCIDENT)
    r = client.post("/api/trips", json=kwargs)
    assert r.status_code == 200, r.text
    return r.json()


def test_health_endpoint():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["hospitals_loaded"] > 0


def test_creating_a_trip_without_incident_location_is_rejected():
    r = client.post("/api/trips", json={})
    assert r.status_code == 422


def test_creating_a_trip_with_patient_details():
    trip = create_trip(patient_age=45, patient_sex="male", patient_notes="chest pain")
    assert trip["patient_age"] == 45
    assert trip["patient_sex"] == "male"
    assert trip["patient_notes"] == "chest pain"
    assert trip["incident_location"]["lat"] == DEFAULT_INCIDENT["lat"]


def test_unknown_hospital_capacity_update_returns_404():
    r = client.post("/api/hospitals/DOES_NOT_EXIST/capacity", json={"icu_beds_free": 0})
    assert r.status_code == 404


def test_unknown_condition_code_returns_400():
    trip = create_trip()
    r = client.post(f"/api/trips/{trip['id']}/case", json={"condition_code": "not_a_real_condition"})
    assert r.status_code == 400


def test_triage_suggest_returns_a_known_condition_code():
    trip = create_trip()
    r = client.post(f"/api/trips/{trip['id']}/triage", json={"note": "chest pain, sweating, crushing pressure"})
    assert r.status_code == 200
    body = r.json()
    assert body["condition_code"] == "cardiac"
    assert body["source"] in ("watsonx", "keyword_fallback")
    assert 0.0 <= body["confidence"] <= 1.0
    # Suggesting a condition must not itself set the trip's condition/status -
    # the dispatcher still has to confirm via POST .../case (see triage_service.py).
    trip_after = client.get(f"/api/trips/{trip['id']}").json()
    assert trip_after["condition_code"] is None
    assert trip_after["status"] == "awaiting_assessment"


def test_triage_suggest_rejects_empty_note():
    trip = create_trip()
    r = client.post(f"/api/trips/{trip['id']}/triage", json={"note": "   "})
    assert r.status_code == 422


def test_triage_suggest_unknown_trip_id_returns_404():
    r = client.post("/api/trips/does-not-exist/triage", json={"note": "chest pain"})
    assert r.status_code == 404


def test_selecting_a_hospital_sends_a_prealert_with_a_handover_note():
    # No WATSONX_API_KEY/WATSONX_PROJECT_ID in the test environment, so this
    # exercises the template_fallback path end-to-end - but the point is the
    # prealert always carries a usable handover_note, live model or not.
    trip = create_trip(patient_age=55, patient_sex="female")
    client.post(f"/api/trips/{trip['id']}/triage", json={"note": "chest pain, sweating, crushing pressure"})
    case_resp = client.post(f"/api/trips/{trip['id']}/case", json={"condition_code": "cardiac"}).json()
    recommended_id = case_resp["recommendation"]["recommended_hospital_id"]

    trip_after = client.post(f"/api/trips/{trip['id']}/select", json={"hospital_id": recommended_id}).json()
    prealert = trip_after["prealert"]
    assert prealert is not None
    assert prealert["handover_note"]
    assert prealert["status"] == "pending"
    assert prealert["handover_source"] in ("watsonx", "template_fallback")
    # The dispatcher's own free-text note AND the patient demographics
    # should be reflected in the fallback template (proves both round-
    # tripped from trip creation / the /triage call through to the prealert,
    # not just the structured condition code).
    assert "chest pain" in prealert["handover_note"]
    assert "55yo" in prealert["handover_note"]


def test_select_before_case_returns_400():
    trip = create_trip()
    r = client.post(f"/api/trips/{trip['id']}/select", json={"hospital_id": "H1"})
    assert r.status_code == 400


def test_unknown_trip_id_returns_404():
    r = client.get("/api/trips/does-not-exist")
    assert r.status_code == 404
    r = client.post("/api/trips/does-not-exist/case", json={"condition_code": "cardiac"})
    assert r.status_code == 404


def test_selecting_a_hospital_reserves_its_capacity():
    trip = create_trip()
    case_resp = client.post(f"/api/trips/{trip['id']}/case", json={"condition_code": "cardiac"}).json()
    recommended_id = case_resp["recommendation"]["recommended_hospital_id"]
    assert recommended_id is not None

    before = client.get("/api/hospitals").json()
    before_h = next(h for h in before if h["id"] == recommended_id)

    client.post(f"/api/trips/{trip['id']}/select", json={"hospital_id": recommended_id})

    after = client.get("/api/hospitals").json()
    after_h = next(h for h in after if h["id"] == recommended_id)

    # Cardiac requires ICU, so selecting should have reserved one ICU bed
    # (or, if ICU wasn't the constraint, at least occupied an ED bay).
    assert after_h["ed_bays_occupied"] == before_h["ed_bays_occupied"] + 1
    if before_h["icu_beds_free"] > 0:
        assert after_h["icu_beds_free"] == before_h["icu_beds_free"] - 1


def test_two_concurrent_trips_are_independent():
    trip_a = create_trip(ambulance_label="AMB-A")
    trip_b = create_trip(ambulance_label="AMB-B")
    assert trip_a["id"] != trip_b["id"]

    client.post(f"/api/trips/{trip_a['id']}/case", json={"condition_code": "cardiac"})

    trip_b_fresh = client.get(f"/api/trips/{trip_b['id']}").json()
    # Assessing trip A must not have changed trip B's state.
    assert trip_b_fresh["status"] == "awaiting_assessment"
    assert trip_b_fresh["condition_code"] is None


def test_deleting_a_trip_releases_its_reservation():
    trip = create_trip()
    case_resp = client.post(f"/api/trips/{trip['id']}/case", json={"condition_code": "cardiac"}).json()
    recommended_id = case_resp["recommendation"]["recommended_hospital_id"]

    before = client.get("/api/hospitals").json()
    before_h = next(h for h in before if h["id"] == recommended_id)

    client.post(f"/api/trips/{trip['id']}/select", json={"hospital_id": recommended_id})
    client.delete(f"/api/trips/{trip['id']}")

    after = client.get("/api/hospitals").json()
    after_h = next(h for h in after if h["id"] == recommended_id)
    assert after_h["ed_bays_occupied"] == before_h["ed_bays_occupied"]
    assert after_h["icu_beds_free"] == before_h["icu_beds_free"]

    # And the trip itself is really gone.
    assert client.get(f"/api/trips/{trip['id']}").status_code == 404


def test_capacity_cannot_be_pushed_past_totals():
    hospitals = client.get("/api/hospitals").json()
    h = hospitals[0]
    r = client.post(f"/api/hospitals/{h['id']}/capacity", json={
        "icu_beds_free": h["icu_beds_total"] + 999,
        "ed_bays_occupied": h["ed_bays_total"] + 999,
    })
    assert r.status_code == 200
    updated = r.json()["hospital"]
    assert updated["icu_beds_free"] == h["icu_beds_total"]
    assert updated["ed_bays_occupied"] == h["ed_bays_total"]


def test_invalid_accepting_status_is_rejected():
    hospitals = client.get("/api/hospitals").json()
    h = hospitals[0]
    r = client.post(f"/api/hospitals/{h['id']}/capacity", json={"accepting_status": "maybe"})
    assert r.status_code == 400


# ------------------------------------------------- pre-alert accept/reject -

def _trip_en_route(condition_code="cardiac"):
    trip = create_trip()
    case_resp = client.post(f"/api/trips/{trip['id']}/case", json={"condition_code": condition_code}).json()
    recommended_id = case_resp["recommendation"]["recommended_hospital_id"]
    trip_after = client.post(f"/api/trips/{trip['id']}/select", json={"hospital_id": recommended_id}).json()
    return trip_after


def test_hospital_accepting_prealert_marks_it_accepted():
    trip = _trip_en_route()
    r = client.post(f"/api/trips/{trip['id']}/prealert/respond", json={"accept": True})
    assert r.status_code == 200
    prealert = r.json()["prealert"]
    assert prealert["status"] == "accepted"
    assert prealert["acknowledged_at"] is not None


def test_hospital_rejecting_prealert_excludes_it_and_triggers_reroute():
    trip = _trip_en_route()
    rejected_hospital_id = trip["dest_hospital_id"]

    r = client.post(f"/api/trips/{trip['id']}/prealert/respond", json={"accept": False, "reason": "no ICU bed"})
    assert r.status_code == 200
    body = r.json()
    assert body["prealert"]["status"] == "rejected"
    assert body["prealert"]["reject_reason"] == "no ICU bed"
    assert rejected_hospital_id in body["rejected_hospital_ids"]
    # A hospital declining its own patient must produce an immediate reroute
    # suggestion, not silently leave the ambulance still heading there.
    assert body["reroute_alert"] is not None
    assert body["reroute_alert"]["snapshot"]["recommended_hospital_id"] != rejected_hospital_id


def test_rejected_hospital_never_recommended_again_for_the_same_trip():
    trip = _trip_en_route()
    rejected_hospital_id = trip["dest_hospital_id"]
    client.post(f"/api/trips/{trip['id']}/prealert/respond", json={"accept": False})

    r = client.post(f"/api/trips/{trip['id']}/reroute-response", json={"accept": True})
    assert r.status_code == 200
    new_trip = r.json()
    assert new_trip["dest_hospital_id"] != rejected_hospital_id

    # And a brand-new recommendation pass for this same trip must keep
    # excluding it (not just the one reroute snapshot at the moment of
    # rejection).
    fresh = compute_fresh_recommendation(new_trip)
    ids = {c["hospital_id"] for c in fresh["candidates"] if c["viable"]}
    assert rejected_hospital_id not in ids


def compute_fresh_recommendation(trip):
    pos = trip["ambulance_pos"]
    return app_module.compute_recommendation(
        pos["lat"], pos["lng"], trip["condition_code"], trip["id"],
        exclude_ids=set(trip.get("rejected_hospital_ids") or []),
    )


def test_prealert_respond_without_active_prealert_returns_400():
    trip = create_trip()
    r = client.post(f"/api/trips/{trip['id']}/prealert/respond", json={"accept": True})
    assert r.status_code == 400


# ------------------------------------------------------- position reports -

def test_reporting_a_device_position_updates_ambulance_pos():
    trip = _trip_en_route()
    r = client.post(f"/api/trips/{trip['id']}/position", json={"lat": 23.05, "lng": 72.58, "accuracy_m": 12.5})
    assert r.status_code == 200

    fresh = client.get(f"/api/trips/{trip['id']}").json()
    assert fresh["position_source"] == "device_gps"
    assert fresh["ambulance_pos"] == {"lat": 23.05, "lng": 72.58}


def test_position_report_rejects_out_of_range_coordinates():
    trip = _trip_en_route()
    r = client.post(f"/api/trips/{trip['id']}/position", json={"lat": 999, "lng": 72.58})
    assert r.status_code == 422


# --------------------------------------------------------- dynamic reroute -

def test_capacity_change_on_current_destination_triggers_reroute_suggestion():
    trip = _trip_en_route()
    dest_id = trip["dest_hospital_id"]

    # The receiving hospital's own ICU capacity drops to zero after
    # selection - a real, live capacity edit (not a canned "demo
    # scenario"), which should make the current destination non-viable and
    # surface a reroute suggestion for the trip already headed there.
    r = client.post(f"/api/hospitals/{dest_id}/capacity", json={"icu_beds_free": 0})
    assert r.status_code == 200

    fresh = client.get(f"/api/trips/{trip['id']}").json()
    assert fresh["reroute_alert"] is not None


def test_accepting_a_reroute_releases_old_and_reserves_new_capacity():
    trip = _trip_en_route()
    old_dest = trip["dest_hospital_id"]
    client.post(f"/api/hospitals/{old_dest}/capacity", json={"icu_beds_free": 0})

    before = client.get("/api/hospitals").json()
    before_old = next(h for h in before if h["id"] == old_dest)

    fresh = client.get(f"/api/trips/{trip['id']}").json()
    assert fresh["reroute_alert"] is not None
    new_dest = fresh["reroute_alert"]["snapshot"]["recommended_hospital_id"]

    r = client.post(f"/api/trips/{trip['id']}/reroute-response", json={"accept": True})
    assert r.status_code == 200
    updated_trip = r.json()
    assert updated_trip["dest_hospital_id"] == new_dest
    assert updated_trip["reroute_alert"] is None

    after = client.get("/api/hospitals").json()
    after_old = next(h for h in after if h["id"] == old_dest)
    # ED bay released at the declined hospital (ICU was already at 0).
    assert after_old["ed_bays_occupied"] == before_old["ed_bays_occupied"] - 1


# -------------------------------------------------------------- persistence

def test_active_trip_survives_a_simulated_restart():
    trip = _trip_en_route()

    # Simulate a process restart: rebuild State from the database only,
    # exactly like module import time does on a real restart.
    app_module.state.reset(wipe_history=False)

    reloaded = app_module.state.trips.get(trip["id"])
    assert reloaded is not None
    assert reloaded["status"] == "en_route"
    assert reloaded["dest_hospital_id"] == trip["dest_hospital_id"]


def test_hospital_capacity_survives_a_simulated_restart():
    hospitals = client.get("/api/hospitals").json()
    h = hospitals[0]
    client.post(f"/api/hospitals/{h['id']}/capacity", json={"icu_beds_free": 1})

    app_module.state.reset(wipe_history=False)

    reloaded = app_module.state.hospitals[h["id"]]
    assert reloaded["icu_beds_free"] == 1
