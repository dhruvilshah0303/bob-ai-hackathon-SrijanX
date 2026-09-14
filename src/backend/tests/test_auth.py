"""
Tests for auth.py's shared-secret access gate - the thing that actually
stops a stranger with the URL from cancelling someone else's ambulance trip
or changing a hospital's capacity once APP_ACCESS_TOKEN is set.

These run against the real app (app_module.app) via TestClient, monkeypatching
config.APP_ACCESS_TOKEN per test. That's safe and doesn't leak between tests:
auth.py/app.py/ratelimit.py all `import config` and read config.APP_ACCESS_TOKEN
fresh on every single request rather than caching it at import time, and
pytest's `monkeypatch` fixture automatically reverts every setattr after each
test finishes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import app as app_module  # noqa: E402
import auth  # noqa: E402
import config  # noqa: E402

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def reset_state():
    client.post("/api/reset")
    yield


# --------------------------------------------------------- unit-level -----

def test_valid_accepts_correct_token(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    assert auth._valid("s3cret") is True


def test_valid_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    assert auth._valid("wrong") is False


def test_valid_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    assert auth._valid(None) is False


def test_valid_allows_anything_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", None)
    assert auth._valid(None) is True
    assert auth._valid("literally-anything") is True


def test_check_ws_token_mirrors_valid(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    assert auth.check_ws_token("s3cret") is True
    assert auth.check_ws_token("nope") is False
    assert auth.check_ws_token(None) is False


# ------------------------------------------------------- middleware-level -

def test_api_requests_succeed_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", None)
    r = client.get("/api/hospitals")
    assert r.status_code == 200


def test_api_requests_rejected_without_key_when_auth_enabled(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    r = client.get("/api/hospitals")
    assert r.status_code == 401


def test_api_requests_rejected_with_wrong_key(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    r = client.get("/api/hospitals", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_api_requests_succeed_with_correct_key(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    r = client.get("/api/hospitals", headers={"X-API-Key": "s3cret"})
    assert r.status_code == 200


def test_health_endpoint_never_requires_auth(monkeypatch):
    # Platform health checks can't send a custom header - /api/health must
    # stay reachable even with a token configured.
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    r = client.get("/api/health")
    assert r.status_code == 200


def test_websocket_connection_rejected_without_token(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    with pytest.raises(Exception):
        # starlette's TestClient raises when the server closes during handshake
        with client.websocket_connect("/ws"):
            pass


def test_websocket_connection_accepted_with_correct_token(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", "s3cret")
    with client.websocket_connect("/ws?token=s3cret") as ws:
        # Getting this far without an exception means the handshake was
        # accepted (auth.check_ws_token passed) - close cleanly.
        ws.close()


def test_websocket_connection_accepted_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(config, "APP_ACCESS_TOKEN", None)
    with client.websocket_connect("/ws") as ws:
        ws.close()
