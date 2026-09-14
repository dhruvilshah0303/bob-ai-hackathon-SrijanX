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
