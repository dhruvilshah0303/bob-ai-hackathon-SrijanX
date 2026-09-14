"""
Simple in-memory, per-IP rate limiter for the API.

Deliberately in-memory rather than Redis-backed: this app is architected to
run as a single process (see the README's "state in memory" note), so an
in-memory limiter is consistent with that constraint, not a workaround for
it. If this app is ever split across multiple instances, this needs to move
to a shared store (Redis) at the same time as the rest of the state does -
see PRODUCTION_READINESS_RECOMMENDATIONS.md, Tier 3.

Algorithm: fixed-window counter per (client IP, current minute). Simple,
cheap, and good enough to stop accidental hammering or a runaway frontend
retry loop - it is not a defense against a determined attacker (that needs
a real WAF/edge rate limiter in front, which most hosting platforms offer).
"""
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import config

_counters: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))  # ip -> (window_start_minute, count)


def _client_ip(request: Request) -> str:
    # SECURITY: only trust the "X-Forwarded-For" header when explicitly
    # configured to (TRUST_PROXY_HEADERS=true) - it's client-controlled and
    # trusting it blindly lets anyone dodge the rate limit or frame another
    # IP. Default is to always use the direct connection's address.
    if config.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# /api/health is polled frequently by platform health checks / uptime
# monitors and isn't "real traffic" - it shouldn't eat into the same budget
# a real client's requests share, or a busy health checker could lock
# everyone else out.
_EXEMPT_PATHS = {"/api/health"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not config.RATE_LIMIT_ENABLED or not path.startswith("/api/") or path in _EXEMPT_PATHS:
            return await call_next(request)

        ip = _client_ip(request)
        current_minute = int(time.time() // 60)
        window_start, count = _counters[ip]

        if window_start != current_minute:
            window_start, count = current_minute, 0

        count += 1
        _counters[ip] = (window_start, count)

        if count > config.RATE_LIMIT_PER_MINUTE:
            return JSONResponse(
                {"detail": f"Rate limit exceeded ({config.RATE_LIMIT_PER_MINUTE} requests/minute). Try again shortly."},
                status_code=429,
            )

        # Cheap, unbounded-growth guard: forget IPs we haven't seen in a
        # while so this dict doesn't grow forever over a long-running process.
        if len(_counters) > 10_000:
            stale = [k for k, (w, _) in _counters.items() if w != current_minute]
            for k in stale:
                _counters.pop(k, None)

        return await call_next(request)
