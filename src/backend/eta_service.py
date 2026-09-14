"""
Traffic-aware ETA service (TRD Section 7 / 12).

Tries a real provider first (Google Distance Matrix, or Mapbox Directions,
selected by which API key is present in the environment). If no key is
configured, or the live call fails for any reason, it falls back to a
simulated, clearly-labeled traffic-aware estimate so the demo never breaks
on a missing key or a flaky third-party API (TRD 5.5 / Section 10:
graceful degradation, never a silent/hard failure).

Design notes from the code review:
  - The simulated traffic multiplier is now SEEDED per (trip, hospital)
    instead of freshly randomized on every call. Re-rolling randomness on
    every recompute made hospital rankings visibly flicker between
    assessments during a demo, which undermines trust in the "AI decision"
    story. Seeding means the same trip sees a stable, plausible traffic
    condition for a given route - only the ambulance's actual movement
    changes the ETA, which is how real traffic behaves over a few minutes.
  - Constants that were previously inline "magic numbers" are named below
    so they can be tuned (or moved into severity_rules.json-style config)
    without hunting through function bodies.
  - The ETA cache is bounded (see _CACHE_MAX_ENTRIES) so a long-running
    process doesn't grow this dict forever.
"""
import logging
import os
import random
import time
import requests
from utils import haversine_km
from env_loader import load_env

load_env()  # idempotent - safe no matter what order modules import each other in
logger = logging.getLogger(__name__)

# --- Tunable constants (see docstring above) --------------------------------
ROAD_DISTANCE_FACTOR = 1.3       # straight-line -> approximate road distance
AVG_URBAN_SPEED_KMH = 32         # emergency-vehicle average urban speed
SIMULATED_TRAFFIC_MIN = 1.05     # narrowed from an earlier 1.0-1.6 range that
SIMULATED_TRAFFIC_MAX = 1.30     # made ETAs swing too wildly between recomputes
NO_ETA_FALLBACK_DELAY_MIN = 999  # sentinel used only if ETA is ever truly unavailable

_CACHE_TTL_SEC = 20               # avoid hammering the live API while the ambulance barely moves
_CACHE_MAX_ENTRIES = 500          # simple bound so this can't grow unbounded over a long run


def _google_key() -> str:
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()


def _mapbox_key() -> str:
    return os.environ.get("MAPBOX_API_KEY", "").strip()


_cache: dict[str, dict] = {}


def _cache_key(origin, dest_id):
    return f"{round(origin[0],4)},{round(origin[1],4)}->{dest_id}"


def _evict_if_needed():
    if len(_cache) > _CACHE_MAX_ENTRIES:
        # Cheap eviction: drop the oldest half by insertion order. Good enough
        # for a demo process; a real deployment would use a proper LRU/TTL
        # cache (e.g. cachetools) or push this to Redis per the TRD.
        oldest_keys = list(_cache.keys())[: len(_cache) // 2]
        for k in oldest_keys:
            _cache.pop(k, None)


def _simulated_eta(origin_lat, origin_lng, dest_lat, dest_lng, seed_key: str | None) -> dict:
    """Distance-based estimate with a traffic congestion factor that's
    seeded per (trip, hospital) so it stays stable across recomputes within
    one trip, rather than re-rolling (and flickering rankings) every call."""
    distance_km = haversine_km(origin_lat, origin_lng, dest_lat, dest_lng)
    road_km = distance_km * ROAD_DISTANCE_FACTOR
    rng = random.Random(seed_key) if seed_key else random
    traffic_factor = rng.uniform(SIMULATED_TRAFFIC_MIN, SIMULATED_TRAFFIC_MAX)
    eta_min = (road_km / AVG_URBAN_SPEED_KMH) * 60 * traffic_factor
    return {"eta_min": eta_min, "distance_km": road_km, "source": "simulated"}


def _google_eta(origin_lat, origin_lng, dest_lat, dest_lng) -> dict | None:
    key = _google_key()
    if not key:
        return None
    try:
        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            "origins": f"{origin_lat},{origin_lng}",
            "destinations": f"{dest_lat},{dest_lng}",
            "departure_time": "now",
            "traffic_model": "best_guess",
            "key": key,
        }
        resp = requests.get(url, params=params, timeout=4)
        data = resp.json()
        if data.get("status") != "OK":
            logger.warning("Google Distance Matrix top-level status: %s %s", data.get("status"), data.get("error_message", ""))
            return None
        element = data["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            logger.warning("Google Distance Matrix element status: %s", element.get("status"))
            return None
        duration_sec = element.get("duration_in_traffic", element["duration"])["value"]
        distance_m = element["distance"]["value"]
        return {"eta_min": duration_sec / 60.0, "distance_km": distance_m / 1000.0, "source": "live_api"}
    except Exception as e:
        logger.warning("Google Distance Matrix call failed: %s", e)
        return None


def _mapbox_eta(origin_lat, origin_lng, dest_lat, dest_lng) -> dict | None:
    key = _mapbox_key()
    if not key:
        return None
    try:
        url = f"https://api.mapbox.com/directions/v5/mapbox/driving-traffic/{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
        params = {"access_token": key, "overview": "false"}
        resp = requests.get(url, params=params, timeout=4)
        data = resp.json()
        route = data["routes"][0]
        return {"eta_min": route["duration"] / 60.0, "distance_km": route["distance"] / 1000.0, "source": "live_api"}
    except Exception as e:
        logger.warning("Mapbox Directions call failed: %s", e)
        return None


def get_eta(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    dest_id: str,
    seed_key: str | None = None,
) -> dict:
    """
    seed_key: pass something stable per-trip (e.g. the trip_id) so the
    simulated-fallback traffic factor doesn't change between recomputes of
    the same trip. Omit it (or pass None) to get fresh randomness each call.
    """
    key = _cache_key((origin_lat, origin_lng), dest_id)
    cached = _cache.get(key)
    if cached and (time.time() - cached["_ts"]) < _CACHE_TTL_SEC:
        return {k: v for k, v in cached.items() if k != "_ts"}

    result = _google_eta(origin_lat, origin_lng, dest_lat, dest_lng)
    if result is None:
        result = _mapbox_eta(origin_lat, origin_lng, dest_lat, dest_lng)
    if result is None:
        combined_seed = f"{seed_key}:{dest_id}" if seed_key else None
        result = _simulated_eta(origin_lat, origin_lng, dest_lat, dest_lng, combined_seed)

    result["_ts"] = time.time()
    _cache[key] = result
    _evict_if_needed()
    return {k: v for k, v in result.items() if k != "_ts"}


def provider_status() -> dict:
    google_key = _google_key()
    mapbox_key = _mapbox_key()
    return {
        "google_configured": bool(google_key),
        "mapbox_configured": bool(mapbox_key),
        "active_mode": "live_api" if (google_key or mapbox_key) else "simulated",
    }
