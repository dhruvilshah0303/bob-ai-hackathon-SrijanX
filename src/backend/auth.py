"""
Shared-secret access gate for the API.

This is deliberately not full user accounts / RBAC - the PRD's real design
calls for per-role auth (crew / hospital / admin) once this is a real
pilot, and building that out is real work with real product decisions
(who issues accounts, how a hospital's staff log in, etc.) that shouldn't
be improvised into a hackathon prototype. What this DOES solve: the actual
blocker to sharing a public link at all, which is "anyone with the URL can
currently cancel anyone else's ambulance trip or change any hospital's
capacity." Setting APP_ACCESS_TOKEN closes that specific hole with about
thirty lines of code; it's a floor, not a ceiling.

Behavior:
  - APP_ACCESS_TOKEN unset (the default) -> auth is OFF. Zero friction for
    local development, exactly like before this file existed.
  - APP_ACCESS_TOKEN set -> every /api/* request (except /api/health, so
    platform health checks keep working unauthenticated) must send it as
    an `X-API-Key` header, and the /ws WebSocket connection must send it as
    a `?token=` query parameter (browsers can't set custom headers on the
    WebSocket handshake).
"""
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import config

UNAUTHENTICATED_PATHS = {"/api/health"}
# /api/auth/* is the real login/register system (auth_routes.py) - it must
# be reachable without already holding the legacy shared secret this
# middleware guards, otherwise nobody could ever log in to get a real
# token. Each /api/auth/* route enforces its own authorization (most are
# public by design - register/login; /me and /logout require a valid JWT
# via auth_service.get_current_user, checked independently of this gate).
UNAUTHENTICATED_PREFIXES = ("/api/auth/",)


def _valid(provided: str | None) -> bool:
    if not config.APP_ACCESS_TOKEN:
        return True  # auth disabled
    if not provided:
        return False
    # Constant-time comparison so this can't leak the token via timing.
    return hmac.compare_digest(provided, config.APP_ACCESS_TOKEN)


class AccessTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not config.APP_ACCESS_TOKEN:
            return await call_next(request)  # auth disabled entirely

        path = request.url.path
        needs_auth = (
            path.startswith("/api/")
            and path not in UNAUTHENTICATED_PATHS
            and not path.startswith(UNAUTHENTICATED_PREFIXES)
        )
        if needs_auth:
            provided = request.headers.get("x-api-key")
            if not _valid(provided):
                return JSONResponse({"detail": "missing or invalid X-API-Key"}, status_code=401)

        return await call_next(request)


def check_ws_token(token: str | None) -> bool:
    """Called by the /ws endpoint directly (WebSocket handshakes don't go
    through HTTP middleware the same way) before accepting the connection."""
    return _valid(token)
