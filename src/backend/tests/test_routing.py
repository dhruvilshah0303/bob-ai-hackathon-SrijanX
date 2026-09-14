"""
Unit tests for the road-following route logic added to fix the "ambulance
cuts a diagonal line across the map instead of driving on roads" issue:
  - utils.interpolate_along_path: walks a multi-point polyline by cumulative
    distance instead of lerping straight from the first point to the last.
  - routing_service._decode_polyline: Google's polyline encoding, checked
    against the canonical worked example from Google's own documentation.
  - routing_service.get_route: falls back to a straight 2-point path (the
    old behavior) when no live provider is reachable, so a missing/blocked
    API key degrades gracefully instead of breaking movement.

Run with:  cd backend && python3 -m pytest tests/ -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import interpolate_along_path  # noqa: E402
import routing_service  # noqa: E402


def test_interpolate_along_path_follows_waypoints_not_a_straight_line():
    # An L-shaped path: east, then north. A naive straight-line lerp from
    # the first point to the last would move diagonally the whole time;
    # walking the path should instead traverse the first leg fully before
    # starting the second.
    path = [
        {"lat": 0.0, "lng": 0.0},
        {"lat": 0.0, "lng": 1.0},  # end of leg 1 (heading east)
        {"lat": 1.0, "lng": 1.0},  # end of leg 2 (heading north)
    ]

    start = interpolate_along_path(path, 0.0)
    assert start == {"lat": 0.0, "lng": 0.0}

    end = interpolate_along_path(path, 1.0)
    assert end == {"lat": 1.0, "lng": 1.0}

    # Halfway through the total distance (the two legs are equal length),
    # we should be exactly at the corner - still heading-east progress,
    # zero north progress yet. A straight-line lerp would instead already
    # show lat=0.5, lng=0.5 here, which is the bug this replaces.
    midpoint = interpolate_along_path(path, 0.5)
    assert midpoint["lng"] == 1.0
    assert midpoint["lat"] == 0.0

    # Partway into the second leg: lng stays pinned at 1.0 (already at the
    # eastern edge), lat has started climbing north.
    into_second_leg = interpolate_along_path(path, 0.75)
    assert into_second_leg["lng"] == 1.0
    assert 0.0 < into_second_leg["lat"] < 1.0


def test_interpolate_along_path_two_point_fallback_matches_old_straight_line_behavior():
    # When no live routing provider is available, routing_service returns a
    # plain 2-point [origin, destination] path - this must behave exactly
    # like the old direct lerp, so that case is not a regression.
    path = [{"lat": 10.0, "lng": 20.0}, {"lat": 12.0, "lng": 24.0}]
    mid = interpolate_along_path(path, 0.5)
    assert mid == {"lat": 11.0, "lng": 22.0}


def test_interpolate_along_path_handles_degenerate_input():
    assert interpolate_along_path([], 0.5) == {"lat": 0.0, "lng": 0.0}
    single = [{"lat": 5.0, "lng": 6.0}]
    assert interpolate_along_path(single, 0.5) == {"lat": 5.0, "lng": 6.0}


def test_decode_polyline_matches_googles_canonical_example():
    # Worked example straight from Google's own polyline algorithm docs:
    # https://developers.google.com/maps/documentation/utilities/polylinealgorithm
    encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    points = routing_service._decode_polyline(encoded)
    expected = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert len(points) == len(expected)
    for p, (lat, lng) in zip(points, expected):
        assert abs(p["lat"] - lat) < 1e-4
        assert abs(p["lng"] - lng) < 1e-4


def test_get_route_falls_back_to_straight_line_without_any_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("MAPBOX_API_KEY", raising=False)
    result = routing_service.get_route(1.0, 2.0, 3.0, 4.0, "some_hospital")
    assert result["source"] == "simulated"
    assert result["points"] == [{"lat": 1.0, "lng": 2.0}, {"lat": 3.0, "lng": 4.0}]


def test_get_route_falls_back_when_live_call_raises(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key-for-test")
    monkeypatch.delenv("MAPBOX_API_KEY", raising=False)
    routing_service._cache.clear()

    def _boom(*args, **kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(routing_service.requests, "get", _boom)
    result = routing_service.get_route(1.0, 2.0, 3.0, 4.0, "some_other_hospital")
    assert result["source"] == "simulated"
    assert result["points"] == [{"lat": 1.0, "lng": 2.0}, {"lat": 3.0, "lng": 4.0}]


def test_provider_status_distinguishes_unconfigured_from_configured_but_failing(monkeypatch):
    # Both "no key at all" and "a key IS set but every live call has failed"
    # degrade identically from movement's point of view (straight line), but
    # need completely different fixes - this is what lets the frontend (and
    # a person debugging) tell them apart instead of always saying "not
    # configured" even when a key is genuinely present.
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("MAPBOX_API_KEY", raising=False)
    routing_service._last_failure_reason = None
    status = routing_service.provider_status()
    assert status["google_configured"] is False
    assert status["active_mode"] == "simulated"
    assert status["last_failure_reason"] is None

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake-key-for-test")
    routing_service._cache.clear()
    monkeypatch.setattr(
        routing_service.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("simulated failure")),
    )
    routing_service.get_route(1.0, 2.0, 3.0, 4.0, "provider_status_test_hospital")
    status = routing_service.provider_status()
    assert status["google_configured"] is True
    assert status["last_failure_reason"] is not None


def test_provider_status_never_leaks_the_api_key_in_a_failure_reason(monkeypatch):
    # SECURITY REGRESSION TEST: last_failure_reason is surfaced through the
    # *public* /api/scenario and /api/health endpoints (no auth required
    # unless APP_ACCESS_TOKEN is set) and rendered straight into the
    # trip-status UI - `requests`' own exception text embeds the full
    # request URL, key=... included, so an unredacted failure reason would
    # hand out a live Google/Mapbox API key to anyone with the page open the
    # moment a live call raised an exception. This must never happen.
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "SECRET-KEY-MUST-NOT-LEAK-12345")
    monkeypatch.delenv("MAPBOX_API_KEY", raising=False)
    routing_service._cache.clear()

    def _boom(*args, **kwargs):
        raise ConnectionError(
            "https://maps.googleapis.com/maps/api/directions/json?"
            "origin=1,2&destination=3,4&key=SECRET-KEY-MUST-NOT-LEAK-12345 failed"
        )

    monkeypatch.setattr(routing_service.requests, "get", _boom)
    routing_service.get_route(1.0, 2.0, 3.0, 4.0, "leak_test_hospital")
    status = routing_service.provider_status()
    assert status["last_failure_reason"] is not None
    assert "SECRET-KEY-MUST-NOT-LEAK-12345" not in status["last_failure_reason"]
    assert "REDACTED" in status["last_failure_reason"]


def test_redact_strips_key_and_access_token_query_params():
    assert "supersecret" not in routing_service._redact("...?key=supersecret&foo=bar")
    assert "supersecret" not in routing_service._redact("...?access_token=supersecret")
    # Non-secret text is left alone.
    assert routing_service._redact("Google Directions status=REQUEST_DENIED") == (
        "Google Directions status=REQUEST_DENIED"
    )
