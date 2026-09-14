"""Small shared helpers: distance math and freshness checks."""
import math
from datetime import datetime, timezone


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two lat/lng points."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def minutes_since(ts: str) -> float:
    """Minutes elapsed since an ISO timestamp, using the timestamp's own tz if present."""
    then = parse_iso(ts)
    now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
    return (now - then).total_seconds() / 60.0


def is_stale(ts: str, threshold_minutes: float) -> bool:
    try:
        return minutes_since(ts) > threshold_minutes
    except Exception:
        return True


def interpolate_along_path(points: list[dict], frac: float) -> dict:
    """Given a road-route polyline as a list of {"lat", "lng"} points (in
    travel order) and a fraction 0..1 of the total leg, return the
    lat/lng that many fractional distance-units along the path.

    This replaces naive straight-line lat/lng interpolation (which cuts
    diagonally across the map, ignoring roads entirely) with movement that
    follows the actual route geometry: it walks the cumulative distance
    along each segment of the path rather than treating origin/destination
    as the only two points. With a 2-point path (the graceful-degradation
    fallback when no live routing is available - see routing_service.py)
    this collapses back to exactly the old straight-line behavior, so
    nothing regresses when a route can't be fetched.
    """
    if not points:
        return {"lat": 0.0, "lng": 0.0}
    if len(points) == 1 or frac <= 0:
        return dict(points[0])
    if frac >= 1:
        return dict(points[-1])

    seg_lengths = [
        haversine_km(points[i]["lat"], points[i]["lng"], points[i + 1]["lat"], points[i + 1]["lng"])
        for i in range(len(points) - 1)
    ]
    total = sum(seg_lengths)
    if total <= 0:
        return dict(points[-1])

    target = frac * total
    covered = 0.0
    for i, seg_len in enumerate(seg_lengths):
        if covered + seg_len >= target or i == len(seg_lengths) - 1:
            seg_frac = 0.0 if seg_len <= 0 else (target - covered) / seg_len
            seg_frac = max(0.0, min(1.0, seg_frac))
            a, b = points[i], points[i + 1]
            return {
                "lat": a["lat"] + (b["lat"] - a["lat"]) * seg_frac,
                "lng": a["lng"] + (b["lng"] - a["lng"]) * seg_frac,
            }
        covered += seg_len
    return dict(points[-1])
