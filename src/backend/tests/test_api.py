"""
API-level tests using FastAPI's TestClient (no real server/network needed).

These cover the two things the code review flagged as actual bugs, so they
also act as regression tests:
  - error paths must return real HTTP status codes (not a 200 with an
    "error" key buried in the body - see app.py's history before the fix)
  - selecting a hospital must reserve its capacity, so two ambulances can't
    both be told the same last ICU bed is free

Run with:  cd backend && python3 -m pytest tests/ -v
Note: this exercises the same coordinator.db the dev server uses and calls
/api/reset before each test for isolation - don't run it against a database
you care about keeping (a demo prototype's db is disposable by design).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import app as app_module  # noqa: E402

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def reset_state():
    client.post("/api/reset")
    yield


def create_trip(**kwargs):
    r = client.post("/api/trips", json=kwargs)
    assert r.status_code == 200
    return r.json()


def test_health_endpoint():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["hospitals_loaded"] > 0


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
    trip = create_trip()
    client.post(f"/api/trips/{trip['id']}/triage", json={"note": "chest pain, sweating, crushing pressure"})
    case_resp = client.post(f"/api/trips/{trip['id']}/case", json={"condition_code": "cardiac"}).json()
    recommended_id = case_resp["recommendation"]["recommended_hospital_id"]

    trip_after = client.post(f"/api/trips/{trip['id']}/select", json={"hospital_id": recommended_id}).json()
    prealert = trip_after["prealert"]
    assert prealert is not None
    assert prealert["handover_note"]
    assert prealert["handover_source"] in ("watsonx", "template_fallback")
    # The dispatcher's own free-text note should be reflected in the
    # fallback template (proves the note round-tripped from the /triage
    # call through to the prealert, not just the structured condition).
    assert "chest pain" in prealert["handover_note"]


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


@pytest.mark.asyncio
async def test_autopilot_trip_progresses_without_any_client_action():
    # Exercises _run_autopilot directly in this test's own event loop rather
    # than through TestClient's request/response cycle: TestClient's sync
    # portal only spins the event loop for the duration of each call, so a
    # background asyncio.create_task() from a previous request isn't
    # guaranteed to run between separate synchronous client calls - that's
    # a test-harness quirk, not how it behaves under a real running server
    # (verified manually against a live uvicorn instance).
    trip = create_trip(autopilot=True)
    await app_module._run_autopilot(trip["id"])
    current = client.get(f"/api/trips/{trip['id']}").json()
    assert current["status"] == "en_route"
    assert current["dest_hospital_id"] is not None
