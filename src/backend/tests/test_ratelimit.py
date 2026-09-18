"""
Tests for ratelimit.py: the per-IP request cap, the X-Forwarded-For trust
gap identified in HARDENING_RECOMMENDATIONS.md (now closed by
config.TRUST_PROXY_HEADERS, default false), and the /api/health exemption.

Uses the real app via TestClient, monkeypatching config values per test -
safe because ratelimit.py reads config.* fresh on every request rather than
caching it, and conftest.py's autouse fixture clears ratelimit._counters
before and after every test so these don't interfere with each other or
with any other test file sharing the same TestClient "client host".

Hammers POST /api/auth/login with a deliberately wrong password rather than
a GET, now that every real GET endpoint requires a valid JWT (there's no
public, side-effect-free GET left except the deliberately rate-limit-exempt
/api/health) - a bad-credentials login is cheap, has no side effects, and
returns a deterministic 401 every time it's NOT rate limited, which is all
these tests need: whether the request got through to the route at all
(any non-429 status) versus was rejected by the limiter itself (429).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from starlette.requests import Request
from fastapi.testclient import TestClient

import app as app_module  # noqa: E402
import config  # noqa: E402
import ratelimit  # noqa: E402

client = TestClient(app_module.app)

_LOGIN_PAYLOAD = {"email": "ratelimit-probe@example-dev.test", "password": "definitely-wrong"}


def _probe():
    return client.post("/api/auth/login", json=_LOGIN_PAYLOAD)


def _fake_request(client_host="1.2.3.4", forwarded_for=None):
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "client": (client_host, 12345),
    }
    return Request(scope)


# --------------------------------------------------------- _client_ip -----

def test_client_ip_ignores_forwarded_for_by_default(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", False)
    req = _fake_request(client_host="5.5.5.5", forwarded_for="9.9.9.9")
    assert ratelimit._client_ip(req) == "5.5.5.5"


def test_client_ip_honors_forwarded_for_when_explicitly_trusted(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    req = _fake_request(client_host="5.5.5.5", forwarded_for="9.9.9.9, 8.8.8.8")
    assert ratelimit._client_ip(req) == "9.9.9.9"


def test_client_ip_falls_back_to_direct_connection_with_no_header(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    req = _fake_request(client_host="5.5.5.5", forwarded_for=None)
    assert ratelimit._client_ip(req) == "5.5.5.5"


def test_client_ip_handles_missing_client(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", False)
    req = Request({"type": "http", "headers": [], "client": None})
    assert ratelimit._client_ip(req) == "unknown"


# ------------------------------------------------------- middleware-level -

def test_requests_within_limit_all_succeed(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 5)
    for _ in range(5):
        assert _probe().status_code == 401  # reached the route (wrong password), not rate limited


def test_requests_beyond_limit_get_429(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 3)
    codes = [_probe().status_code for _ in range(6)]
    assert codes == [401, 401, 401, 429, 429, 429]


def test_disabling_rate_limit_allows_unlimited_requests(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 1)
    codes = [_probe().status_code for _ in range(5)]
    assert codes == [401, 401, 401, 401, 401]


def test_health_endpoint_is_exempt_from_rate_limit(monkeypatch):
    # /api/health is polled by health checkers and shouldn't compete with
    # real traffic for the same tiny budget (HARDENING_RECOMMENDATIONS.md).
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 2)
    codes = [client.get("/api/health").status_code for _ in range(10)]
    assert all(c == 200 for c in codes)


def test_health_exemption_does_not_exempt_other_routes(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 2)
    # Burn the budget on /api/health (exempt) - shouldn't help the login route.
    for _ in range(10):
        client.get("/api/health")
    codes = [_probe().status_code for _ in range(4)]
    assert 429 in codes


def test_spoofed_forwarded_for_does_not_bypass_limit_by_default(monkeypatch):
    # Regression test for the exact HARDENING_RECOMMENDATIONS.md finding:
    # sending a different X-Forwarded-For value per request must NOT grant
    # each request its own fresh bucket when TRUST_PROXY_HEADERS is off.
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 3)
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", False)
    codes = [
        client.post("/api/auth/login", json=_LOGIN_PAYLOAD, headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code
        for i in range(6)
    ]
    assert 429 in codes
