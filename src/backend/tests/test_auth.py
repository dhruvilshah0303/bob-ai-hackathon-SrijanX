"""
Tests for auth.py: real per-user login (username/password -> session
token), role-based access control, and the legacy APP_ACCESS_TOKEN
shared-secret fallback.

These run against the real app (app_module.app) via TestClient. Each test
gets its own clean slate via the `reset_state` fixture below, which also
re-creates a known admin account (state.reset() only wipes trip/audit/
hospital data, not user accounts - see db.reset_operational_state - so
tests manage their own user rows directly).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import app as app_module  # noqa: E402
import auth  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402

client = TestClient(app_module.app)

DEFAULT_INCIDENT = {"lat": 23.03, "lng": 72.56}


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    # Most tests in this file are specifically about login/RBAC being
    # enforced, so force REQUIRE_LOGIN on here regardless of its (off) real
    # default - the tests further down that specifically cover the
    # open-access/REQUIRE_LOGIN=False behavior flip it back off themselves.
    monkeypatch.setattr(config, "REQUIRE_LOGIN", True)
    app_module.state.reset(wipe_history=True)
    with db.engine.begin() as conn:
        conn.execute(db.sessions_table.delete())
        conn.execute(db.users_table.delete())
    auth.create_user("admin", "adminpass123", role="admin")
    yield


# --------------------------------------------------------- unit-level -----

def test_password_hash_roundtrip():
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", hashed) is True
    assert auth.verify_password("wrong password", hashed) is False


def test_password_hash_uses_a_random_salt_per_call():
    a = auth.hash_password("same-password")
    b = auth.hash_password("same-password")
    assert a != b  # different salts -> different stored hashes


def test_authenticate_rejects_unknown_username():
    assert auth.authenticate("nobody", "whatever") is None


def test_authenticate_rejects_wrong_password():
    assert auth.authenticate("admin", "wrong") is None


def test_authenticate_accepts_correct_credentials():
    user = auth.authenticate("admin", "adminpass123")
    assert user is not None
    assert user["role"] == "admin"


def test_issued_session_resolves_back_to_the_same_identity():
    user = auth.authenticate("admin", "adminpass123")
    session = auth.issue_session(user)
    identity = auth.resolve_session(session["token"])
    assert identity == {"username": "admin", "role": "admin", "hospital_id": None}


def test_resolve_session_rejects_unknown_token():
    assert auth.resolve_session("not-a-real-token") is None


def test_resolve_session_rejects_expired_token(monkeypatch):
    monkeypatch.setattr(config, "SESSION_TTL_HOURS", -1)  # expires immediately
    user = auth.authenticate("admin", "adminpass123")
    session = auth.issue_session(user)
    assert auth.resolve_session(session["token"]) is None


def test_legacy_app_access_token_resolves_as_admin(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    identity = auth.resolve_identity("s3cret")
    assert identity == {"username": "legacy-api-key", "role": "admin", "hospital_id": None}


def test_legacy_app_access_token_rejects_wrong_value(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    assert auth.resolve_identity("wrong") is None


# ------------------------------------------------------- endpoint-level ---

def _login(username="admin", password="adminpass123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_login_returns_a_usable_token():
    body = _login()
    assert body["role"] == "admin"
    r = client.get("/api/hospitals", headers={"X-API-Key": body["token"]})
    assert r.status_code == 200


def test_login_with_wrong_password_returns_401():
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_requests_without_any_credential_are_rejected():
    r = client.get("/api/hospitals")
    assert r.status_code == 401


def test_requests_with_invalid_token_are_rejected():
    r = client.get("/api/hospitals", headers={"X-API-Key": "garbage"})
    assert r.status_code == 401


def test_health_endpoint_never_requires_auth():
    r = client.get("/api/health")
    assert r.status_code == 200


def test_logout_invalidates_the_token():
    token = _login()["token"]
    assert client.get("/api/hospitals", headers={"X-API-Key": token}).status_code == 200
    r = client.post("/api/auth/logout", headers={"X-API-Key": token})
    assert r.status_code == 200
    assert client.get("/api/hospitals", headers={"X-API-Key": token}).status_code == 401


def test_me_returns_the_current_identity():
    token = _login()["token"]
    r = client.get("/api/auth/me", headers={"X-API-Key": token})
    assert r.status_code == 200
    assert r.json()["username"] == "admin"


# ---------------------------------------------------------------- roles ---

def _admin_token():
    return _login()["token"]


def _create_user(token, username, password, role, hospital_id=None):
    return client.post("/api/auth/users", headers={"X-API-Key": token}, json={
        "username": username, "password": password, "role": role, "hospital_id": hospital_id,
    })


def test_only_admin_can_create_users():
    admin_token = _admin_token()
    r = _create_user(admin_token, "dispatch1", "dispatchpass1", "dispatcher")
    assert r.status_code == 200

    dispatch_login = client.post("/api/auth/login", json={"username": "dispatch1", "password": "dispatchpass1"}).json()
    r2 = _create_user(dispatch_login["token"], "dispatch2", "dispatchpass2", "dispatcher")
    assert r2.status_code == 403


def test_hospital_role_requires_a_valid_hospital_id():
    admin_token = _admin_token()
    r = _create_user(admin_token, "hosp1", "hosppass1", "hospital", hospital_id="NOT_REAL")
    assert r.status_code == 400


def test_dispatcher_can_create_trips_but_hospital_role_cannot():
    admin_token = _admin_token()
    hospitals = client.get("/api/hospitals", headers={"X-API-Key": admin_token}).json()
    hid = hospitals[0]["id"]
    _create_user(admin_token, "dispatch1", "dispatchpass1", "dispatcher")
    _create_user(admin_token, "hosp1", "hosppass1", "hospital", hospital_id=hid)

    dtoken = client.post("/api/auth/login", json={"username": "dispatch1", "password": "dispatchpass1"}).json()["token"]
    htoken = client.post("/api/auth/login", json={"username": "hosp1", "password": "hosppass1"}).json()["token"]

    r_ok = client.post("/api/trips", headers={"X-API-Key": dtoken}, json={"incident_location": DEFAULT_INCIDENT})
    assert r_ok.status_code == 200

    r_forbidden = client.post("/api/trips", headers={"X-API-Key": htoken}, json={"incident_location": DEFAULT_INCIDENT})
    assert r_forbidden.status_code == 403


def test_hospital_role_can_only_update_its_own_hospitals_capacity():
    admin_token = _admin_token()
    hospitals = client.get("/api/hospitals", headers={"X-API-Key": admin_token}).json()
    own_id, other_id = hospitals[0]["id"], hospitals[1]["id"]
    _create_user(admin_token, "hosp1", "hosppass1", "hospital", hospital_id=own_id)
    htoken = client.post("/api/auth/login", json={"username": "hosp1", "password": "hosppass1"}).json()["token"]

    r_own = client.post(f"/api/hospitals/{own_id}/capacity", headers={"X-API-Key": htoken}, json={"icu_beds_free": 1})
    assert r_own.status_code == 200

    r_other = client.post(f"/api/hospitals/{other_id}/capacity", headers={"X-API-Key": htoken}, json={"icu_beds_free": 1})
    assert r_other.status_code == 403


# ------------------------------------------------------------- websocket --

def test_websocket_connection_rejected_without_token():
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass


def test_websocket_connection_accepted_with_a_real_session_token():
    token = _admin_token()
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.close()


def test_websocket_connection_accepted_with_legacy_access_token(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    with client.websocket_connect("/ws?token=s3cret") as ws:
        ws.close()


# ------------------------------------------------- login not required -----
# REQUIRE_LOGIN defaults to False (see config.py) - everything above forces
# it on via the reset_state fixture since it's testing login enforcement
# itself. These cover the actual default: no credentials needed at all.

def test_health_reports_the_login_required_flag(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_LOGIN", False)
    assert client.get("/api/health").json()["login_required"] is False
    monkeypatch.setattr(config, "REQUIRE_LOGIN", True)
    assert client.get("/api/health").json()["login_required"] is True


def test_requests_without_any_credential_succeed_when_login_not_required(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_LOGIN", False)
    r = client.get("/api/hospitals")
    assert r.status_code == 200


def test_open_access_identity_has_full_admin_equivalent_access(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_LOGIN", False)
    r = client.post("/api/trips", json={"incident_location": DEFAULT_INCIDENT})
    assert r.status_code == 200


def test_websocket_connection_accepted_without_token_when_login_not_required(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_LOGIN", False)
    with client.websocket_connect("/ws") as ws:
        ws.close()


def test_logging_in_still_scopes_a_real_role_even_when_login_not_required(monkeypatch):
    """Open access is the *default* identity for a request with no
    credentials - it doesn't disable RBAC for someone who actually does log
    in with a real (e.g. hospital-scoped) account."""
    admin_token = _admin_token()
    hospitals = client.get("/api/hospitals", headers={"X-API-Key": admin_token}).json()
    own_id, other_id = hospitals[0]["id"], hospitals[1]["id"]
    _create_user(admin_token, "hosp1", "hosppass1", "hospital", hospital_id=own_id)
    htoken = client.post("/api/auth/login", json={"username": "hosp1", "password": "hosppass1"}).json()["token"]

    monkeypatch.setattr(config, "REQUIRE_LOGIN", False)

    r_own = client.post(f"/api/hospitals/{own_id}/capacity", headers={"X-API-Key": htoken}, json={"icu_beds_free": 1})
    assert r_own.status_code == 200

    r_other = client.post(f"/api/hospitals/{other_id}/capacity", headers={"X-API-Key": htoken}, json={"icu_beds_free": 1})
    assert r_other.status_code == 403
