"""
Real per-user authentication (username + password -> session token), plus
role-based access control (admin / dispatcher / hospital).

This replaces the old "single shared secret for the whole API" model with
actual accounts: each user logs in with credentials and gets back a random
session token, stored server-side (db.sessions) with an expiry - the token
is what the frontend then sends back as `X-API-Key` (kept as the header
name for backward compatibility with the existing request plumbing) on
every subsequent request, and as `?token=` on the WebSocket handshake.

Passwords are hashed with PBKDF2-HMAC-SHA256 (200k iterations, random
16-byte salt per user) using only the Python standard library's `hashlib`
and `secrets` - no extra dependency needed for this.

Roles:
  - "admin"      - full access, including creating other user accounts.
  - "dispatcher" - can create/triage/assess/select-hospital/manage trips.
  - "hospital"   - tied to exactly one hospital_id; can update that
                    hospital's own capacity and accept/reject pre-alerts
                    addressed to it. Cannot touch another hospital's data.

APP_ACCESS_TOKEN (legacy): if still set, it's accepted as an alternate
credential with admin-equivalent access - useful for a health check, a
script, or a CI job that shouldn't have to manage a real user account.
It is NOT the primary login mechanism any more; see README/.env.example.

REQUIRE_LOGIN (config.py): whether presenting credentials is actually
mandatory. Defaults to False - a request with no session/legacy token is
then treated as OPEN_ACCESS_IDENTITY (full admin access, no login screen).
Real accounts/roles keep working even while this is off: anyone who does
log in still gets their real (possibly hospital-scoped) role rather than
the open-access admin identity. Set REQUIRE_LOGIN=true to make presenting
real credentials mandatory again.
"""
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import config
import db

UNAUTHENTICATED_PATHS = {"/api/health", "/api/auth/login"}

PBKDF2_ITERATIONS = 200_000

# Identity attached to a request that presents no credentials at all, while
# REQUIRE_LOGIN is off. Full (admin-equivalent) access, so the app behaves
# exactly like open-access local dev used to before per-user login existed -
# this is what makes "no login required" actually mean no login required,
# rather than every unauthenticated call 403ing on role checks instead of
# 401ing on the gate.
OPEN_ACCESS_IDENTITY = {"username": "open-access", "role": "admin", "hospital_id": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return hmac.compare_digest(candidate, digest)


def create_user(username: str, password: str, role: str, hospital_id: str | None = None) -> dict:
    user = {
        "id": str(uuid.uuid4()),
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "hospital_id": hospital_id,
        "created_at": _now_iso(),
    }
    db.create_user(user)
    return user


def authenticate(username: str, password: str) -> dict | None:
    user = db.get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        return None
    return user


def issue_session(user: dict) -> dict:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=config.SESSION_TTL_HOURS)).isoformat()
    session = {
        "token": token,
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "hospital_id": user.get("hospital_id"),
        "created_at": _now_iso(),
        "expires_at": expires_at,
    }
    db.create_session(session)
    return session


def resolve_session(token: str | None) -> dict | None:
    """Returns {"username","role","hospital_id"} for a valid, non-expired
    session token, else None. Does not raise."""
    if not token:
        return None
    session = db.get_session(token)
    if not session:
        return None
    if session["expires_at"] < _now_iso():
        db.delete_session(token)
        return None
    return {"username": session["username"], "role": session["role"], "hospital_id": session.get("hospital_id")}


def _legacy_token_valid(provided: str | None) -> bool:
    if not config.APP_ACCESS_TOKEN or not provided:
        return False
    return hmac.compare_digest(provided, config.APP_ACCESS_TOKEN)


def resolve_identity(token: str | None) -> dict | None:
    """Session token first, then the legacy shared-secret fallback (treated
    as an admin identity). Returns None if neither validates - callers that
    should fall back to OPEN_ACCESS_IDENTITY when REQUIRE_LOGIN is off do
    that themselves (see AccessTokenMiddleware/check_ws_token) rather than
    here, so this function's "does this credential validate?" meaning stays
    unambiguous for real login (POST /api/auth/login) and require_role."""
    user = resolve_session(token)
    if user:
        return user
    if _legacy_token_valid(token):
        return {"username": "legacy-api-key", "role": "admin", "hospital_id": None}
    return None


class AccessTokenMiddleware(BaseHTTPMiddleware):
    """Runs on every /api/* request. Resolves the caller's identity from the
    X-API-Key header (a real session token, or the legacy shared secret) and
    attaches it to request.state.user for route handlers to read. Requests
    with no valid credential are rejected with 401, except the small
    allowlist of paths that must stay reachable unauthenticated (health
    checks, and the login endpoint itself - you can't authenticate your way
    into logging in)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in UNAUTHENTICATED_PATHS:
            return await call_next(request)

        provided = request.headers.get("x-api-key")
        identity = resolve_identity(provided)
        if not identity:
            if not config.REQUIRE_LOGIN:
                identity = OPEN_ACCESS_IDENTITY
            else:
                return JSONResponse({"detail": "missing or invalid credentials - please log in"}, status_code=401)

        request.state.user = identity
        return await call_next(request)


def check_ws_token(token: str | None) -> dict | None:
    """Called by the /ws endpoint directly (WebSocket handshakes don't go
    through HTTP middleware the same way) before accepting the connection.
    Returns the resolved identity, falling back to OPEN_ACCESS_IDENTITY when
    REQUIRE_LOGIN is off (mirrors AccessTokenMiddleware), or None if the
    connection should be rejected."""
    identity = resolve_identity(token)
    if identity:
        return identity
    if not config.REQUIRE_LOGIN:
        return OPEN_ACCESS_IDENTITY
    return None


def require_role(request: Request, *roles: str) -> dict:
    """Route-handler helper: raises 403 unless the caller's role is one of
    `roles` (admin always allowed in addition, unless "admin" is itself the
    only role required and excluded on purpose by the caller not passing
    it - callers that truly want to exclude admin don't use this helper).
    Returns the identity dict on success so the caller can also check e.g.
    hospital_id scoping."""
    from fastapi import HTTPException

    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="missing or invalid credentials - please log in")
    if user["role"] == "admin" or user["role"] in roles:
        return user
    raise HTTPException(status_code=403, detail=f"requires role: {' or '.join(roles)}")
