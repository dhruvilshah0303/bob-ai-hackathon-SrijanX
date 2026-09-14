"""
Road-following route geometry (fixes the "ambulance cuts diagonally across
the map instead of driving on roads" issue).

Same graceful-degradation shape as eta_service.py, deliberately: try a real
routing provider first (Google Directions, or Mapbox Directions - whichever
key is configured), and if no key is set or the live call fails for any
reason, fall back to a straight 2-point "path" (origin -> destination).
That fallback is exactly the old behavior, so a missing/blocked API never
breaks movement - it just silently loses the "follows real roads" polish,
the same trade-off eta_service.py already makes for ETA accuracy.

The result is a list of {"lat", "lng"} points in travel order - the
frontend draws it as a polyline, and app.py's movement loop walks along it
(see utils.interpolate_along_path) instead of lerping straight between two
lat/lngs.
"""
import logging
import os
import re
import time

import requests

from env_loader import load_env

load_env()  # idempotent - same defensive import as eta_service.py, in case
            # this module is ever imported before config.py runs
logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 300  # a route for a given origin/destination pair doesn't
                       # change tick-to-tick the way traffic ETA does, so this
                       # is cached far longer than eta_service's 20s
_CACHE_MAX_ENTRIES = 500


def _google_key() -> str:
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()


def _mapbox_key() -> str:
    return os.environ.get("MAPBOX_API_KEY", "").strip()


_cache: dict[str, dict] = {}

# Surfaced via provider_status() so the frontend (and whoever's watching the
# demo) can tell "no routing key configured at all" apart from "a key IS
# configured but the live call is failing" - these look identical from the
# outside (both silently fall back to a straight line) but need completely
# different fixes. The single most common cause of the second case: Google
# Maps Platform treats Directions API and Distance Matrix API as separate
# products that must each be enabled on the project - a key that already
# works for live ETA (Distance Matrix) is NOT guaranteed to also be enabled
# for Directions.
_last_failure_reason: str | None = None

# SECURITY: `requests`' own exception messages often embed the full request
# URL, including the "key=..."/"access_token=..." query parameter - and
# _last_failure_reason is surfaced through the *public* /api/scenario and
# /api/health endpoints (and shown right in the trip-status UI) so anyone
# with the page open could otherwise read the live Google/Mapbox API key
# straight out of an error message the moment a live routing call raised an
# exception. Every string that reaches _last_failure_reason from exception
# text is redacted through this first.
_KEY_PARAM_RE = re.compile(r"(?i)(key|access_token)=[^&\s'\")]+")


def _redact(text: str) -> str:
    return _KEY_PARAM_RE.sub(r"\1=***REDACTED***", text)


def _cache_key(origin_lat, origin_lng, dest_id):
    # Rounded to ~100m so minor movement-loop position drift doesn't miss
    # the cache on every single tick.
    return f"{round(origin_lat, 3)},{round(origin_lng, 3)}->{dest_id}"


def _evict_if_needed():
    if len(_cache) > _CACHE_MAX_ENTRIES:
        oldest_keys = list(_cache.keys())[: len(_cache) // 2]
        for k in oldest_keys:
            _cache.pop(k, None)


def _straight_line(origin_lat, origin_lng, dest_lat, dest_lng) -> dict:
    return {
        "points": [{"lat": origin_lat, "lng": origin_lng}, {"lat": dest_lat, "lng": dest_lng}],
        "source": "simulated",
    }


def _decode_polyline(encoded: str) -> list[dict]:
    """Decodes Google's polyline encoding (the standard algorithm - see
    https://developers.google.com/maps/documentation/utilities/polylinealgorithm).
    No third-party dependency needed for this; it's ~15 lines."""
    points = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lng += delta
        points.append({"lat": lat / 1e5, "lng": lng / 1e5})
    return points


def _google_route(origin_lat, origin_lng, dest_lat, dest_lng) -> dict | None:
    global _last_failure_reason
    key = _google_key()
    if not key:
        return None
    try:
        url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin": f"{origin_lat},{origin_lng}",
            "destination": f"{dest_lat},{dest_lng}",
            "departure_time": "now",
            "key": key,
        }
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        if data.get("status") != "OK" or not data.get("routes"):
            status = data.get("status", "?")
            error_message = data.get("error_message", "")
            hint = (
                " - this almost always means 'Directions API' is not enabled for this "
                "key's project in Google Cloud Console (it's a separate API from "
                "'Distance Matrix API', which is why live ETA can work while this doesn't)."
                if status == "REQUEST_DENIED"
                else ""
            )
            reason = _redact(f"Google Directions status={status} {error_message}{hint}".strip())
            logger.warning(reason)
            _last_failure_reason = reason
            return None
        overview = data["routes"][0]["overview_polyline"]["points"]
        points = _decode_polyline(overview)
        if len(points) < 2:
            return None
        _last_failure_reason = None
        return {"points": points, "source": "live_api"}
    except Exception as e:
        logger.warning("Google Directions call failed: %s", e)
        _last_failure_reason = _redact(f"Google Directions call raised: {e}")
        return None


def _mapbox_route(origin_lat, origin_lng, dest_lat, dest_lng) -> dict | None:
    global _last_failure_reason
    key = _mapbox_key()
    if not key:
        return None
    try:
        url = f"https://api.mapbox.com/directions/v5/mapbox/driving-traffic/{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
        params = {"access_token": key, "overview": "full", "geometries": "geojson"}
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()
        coords = data["routes"][0]["geometry"]["coordinates"]  # [lng, lat] pairs
        points = [{"lat": c[1], "lng": c[0]} for c in coords]
        if len(points) < 2:
            return None
        _last_failure_reason = None
        return {"points": points, "source": "live_api"}
    except Exception as e:
        logger.warning("Mapbox Directions call failed: %s", e)
        _last_failure_reason = _redact(f"Mapbox Directions call raised: {e}")
        return None


def get_route(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float, dest_id: str) -> dict:
    """Returns {"points": [{"lat","lng"}, ...], "source": "live_api"|"simulated"}."""
    key = _cache_key(origin_lat, origin_lng, dest_id)
    cached = _cache.get(key)
    if cached and (time.time() - cached["_ts"]) < _CACHE_TTL_SEC:
        return {k: v for k, v in cached.items() if k != "_ts"}

    result = _google_route(origin_lat, origin_lng, dest_lat, dest_lng)
    if result is None:
        result = _mapbox_route(origin_lat, origin_lng, dest_lat, dest_lng)
    if result is None:
        result = _straight_line(origin_lat, origin_lng, dest_lat, dest_lng)

    result["_ts"] = time.time()
    _cache[key] = result
    _evict_if_needed()
    return {k: v for k, v in result.items() if k != "_ts"}


def provider_status() -> dict:
    """Mirrors eta_service.provider_status(), plus last_failure_reason - the
    detail that actually lets you tell "no key configured" apart from "a key
    IS configured but every live call so far has failed" (see the module-
    level comment on _last_failure_reason for why that distinction matters
    and its most common cause)."""
    google_key = _google_key()
    mapbox_key = _mapbox_key()
    return {
        "google_configured": bool(google_key),
        "mapbox_configured": bool(mapbox_key),
        "active_mode": "live_api" if (google_key or mapbox_key) else "simulated",
        "last_failure_reason": _last_failure_reason,
    }
