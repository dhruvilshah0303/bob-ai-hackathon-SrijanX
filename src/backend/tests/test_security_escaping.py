"""
Backend defense-in-depth tests for the stored-XSS finding in
HARDENING_RECOMMENDATIONS.md: free-text fields (ambulance_label,
override_reason) that get broadcast live to every connected browser.

The actual XSS fix lives in the frontend (frontend/app.js's escapeHtml(),
applied everywhere these fields are rendered) since that's where the
unescaped `.innerHTML` / `bindPopup()` / `JSON.stringify()`-into-innerHTML
vectors actually were - see PROJECT_STATUS.md / HARDENING_RECOMMENDATIONS.md
for the full vector list and app.js for escapeHtml's call sites. There is no
Python test runner for that JS in this backend-only test suite; it's
covered instead by a live-browser Playwright regression test
(test_frontend_smoke.py::test_stored_xss_payloads_render_inert), which
actually loads the payload into a real Leaflet popup / innerHTML sink and
asserts it never executes - a stronger guarantee than asserting a Python
string got escaped somewhere.

What DOES belong here, at the API layer: the backend-side hardening added
alongside the frontend fix (app.py's Field(max_length=...) + the
_strip_control_chars validators) - a length cap so nobody can broadcast a
multi-megabyte payload to every connected client, and control-character
stripping as a second layer of defense against terminal/log injection in
anything that later prints these fields raw (audit logs, server console).
These are real, independently-testable behaviors of the API regardless of
what the frontend does with the result.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import app as app_module  # noqa: E402

client = TestClient(app_module.app)

SCRIPT_PAYLOAD = "<script>alert(1)</script>"
IMG_PAYLOAD = "<img src=x onerror=alert(1)>"


@pytest.fixture(autouse=True)
def reset_state():
    client.post("/api/reset")
    yield


def _create_trip(**kwargs):
    return client.post("/api/trips", json=kwargs)


# ------------------------------------------------- ambulance_label -----

def test_ambulance_label_accepts_a_normal_value():
    r = _create_trip(ambulance_label="AMB-42")
    assert r.status_code == 200
    assert r.json()["ambulance_label"] == "AMB-42"


def test_ambulance_label_html_payload_is_stored_but_not_expanded():
    # The backend's job here is NOT to sanitize HTML out of the string (that
    # would surprise a legitimate label containing "<" or "&") - it's the
    # frontend's job to render it safely. This just documents that the raw
    # string round-trips unchanged rather than being (wrongly) "fixed up"
    # into something that silently drops the attacker's payload without
    # anyone noticing the field was ever hostile.
    r = _create_trip(ambulance_label=SCRIPT_PAYLOAD)
    assert r.status_code == 200
    assert r.json()["ambulance_label"] == SCRIPT_PAYLOAD


def test_ambulance_label_over_max_length_is_rejected():
    r = _create_trip(ambulance_label="x" * 65)
    assert r.status_code == 422
    assert "ambulance_label" in r.text


def test_ambulance_label_at_max_length_is_accepted():
    r = _create_trip(ambulance_label="x" * 64)
    assert r.status_code == 200


def test_ambulance_label_control_characters_are_stripped():
    r = _create_trip(ambulance_label="AMB\x00\x07-42")
    assert r.status_code == 200
    assert r.json()["ambulance_label"] == "AMB-42"


def test_ambulance_label_blank_after_stripping_becomes_none():
    r = _create_trip(ambulance_label="   ")
    assert r.status_code == 200
    # None -> the backend generates its own default label instead.
    assert r.json()["ambulance_label"]
    assert r.json()["ambulance_label"] != "   "


# ------------------------------------------------- override_reason -----

def _trip_ready_for_selection():
    trip = _create_trip().json()
    case_resp = client.post(f"/api/trips/{trip['id']}/case", json={"condition_code": "cardiac"}).json()
    return trip["id"], case_resp["recommendation"]["recommended_hospital_id"]


def test_override_reason_discarded_when_accepting_the_recommendation():
    # A reason is only meaningful for an override; accepting the AI's own
    # recommendation isn't an override, so a reason sent along with it is
    # intentionally discarded server-side rather than stored/broadcast.
    trip_id, recommended_id = _trip_ready_for_selection()
    r = client.post(f"/api/trips/{trip_id}/select", json={"hospital_id": recommended_id, "override_reason": IMG_PAYLOAD})
    assert r.status_code == 200
    trip = client.get(f"/api/trips/{trip_id}").json()
    assert trip["selection_type"] == "accepted"
    assert trip["override_reason"] is None


def test_override_reason_on_a_true_override_is_stored_verbatim():
    trip_id, recommended_id = _trip_ready_for_selection()
    hospitals = client.get("/api/hospitals").json()
    other = next(
        h for h in hospitals
        if h["id"] != recommended_id
        and h["accepting_status"] in ("yes", "limited")
        and h["icu_beds_free"] > 0
        and h["ed_bays_occupied"] < h["ed_bays_total"]
    )
    r = client.post(f"/api/trips/{trip_id}/select", json={"hospital_id": other["id"], "override_reason": IMG_PAYLOAD})
    assert r.status_code == 200
    trip = client.get(f"/api/trips/{trip_id}").json()
    assert trip["override_reason"] == IMG_PAYLOAD
    assert trip["selection_type"] == "overridden"


def test_override_reason_over_max_length_is_rejected():
    trip_id, recommended_id = _trip_ready_for_selection()
    hospitals = client.get("/api/hospitals").json()
    other = next(
        h for h in hospitals
        if h["id"] != recommended_id
        and h["accepting_status"] in ("yes", "limited")
        and h["icu_beds_free"] > 0
        and h["ed_bays_occupied"] < h["ed_bays_total"]
    )
    r = client.post(f"/api/trips/{trip_id}/select", json={"hospital_id": other["id"], "override_reason": "x" * 201})
    assert r.status_code == 422


def test_override_reason_control_characters_are_stripped():
    trip_id, recommended_id = _trip_ready_for_selection()
    hospitals = client.get("/api/hospitals").json()
    other = next(
        h for h in hospitals
        if h["id"] != recommended_id
        and h["accepting_status"] in ("yes", "limited")
        and h["icu_beds_free"] > 0
        and h["ed_bays_occupied"] < h["ed_bays_total"]
    )
    r = client.post(f"/api/trips/{trip_id}/select", json={
        "hospital_id": other["id"], "override_reason": "closer\x00to\x01family",
    })
    assert r.status_code == 200
    trip = client.get(f"/api/trips/{trip_id}").json()
    assert trip["override_reason"] == "closertofamily"
