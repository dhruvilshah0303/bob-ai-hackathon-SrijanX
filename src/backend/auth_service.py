"""
Real user authentication: password hashing (bcrypt) + JWT session tokens +
role-based authorization dependencies.

This is the only authentication mechanism in the app - every /api/* route
requires a valid access token via get_current_user()/require_role() except
POST /api/auth/register and POST /api/auth/login themselves (a client needs
somewhere to get a token from) and GET /api/health (so platform health
checks don't need credentials).

Token shape: two JWTs per login, both HS256-signed with config.JWT_SECRET.
  - access token: short-lived (JWT_ACCESS_EXPIRE_MINUTES), sent as
    "Authorization: Bearer <token>" on every request (or "?token=..." on a
    WebSocket handshake, which can't set custom headers).
  - refresh token: long-lived (JWT_REFRESH_EXPIRE_DAYS), used only against
    POST /api/auth/refresh to get a new access token without re-entering a
    password. Both carry {"sub": user_id, "role": ..., "hospital_id": ...,
    "type": "access"|"refresh"} so a refresh token can never be used where
    an access token is expected (checked explicitly below) and vice versa.

No server-side token store/blocklist (Rule: PostgreSQL/DB state is the
source of truth for business data, not session state) - logout is
client-side (the frontend discards its stored tokens); an access token
remains technically valid until it naturally expires, which is why the
access token's lifetime is kept short. A real revocation list is future
work if a compromised-token scenario ever requires immediate invalidation.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

import config
from db_session import get_db
from models import Role, User

logger = logging.getLogger(__name__)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# auto_error=False: a missing/malformed Authorization header should produce
# our own 401 JSON shape (see get_current_user), not FastAPI/Starlette's
# default one, so every auth failure in the app looks the same to a client.
_bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(password, password_hash)
    except Exception:
        # Malformed/legacy hash, not a real password mismatch - still a
        # failed login, never a 500.
        return False


def _create_token(user: User, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "role": user.role,
        "hospital_id": user.hospital_id,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def create_access_token(user: User) -> str:
    return _create_token(user, "access", timedelta(minutes=config.JWT_ACCESS_EXPIRE_MINUTES))


def create_refresh_token(user: User) -> str:
    return _create_token(user, "refresh", timedelta(days=config.JWT_REFRESH_EXPIRE_DAYS))


class TokenError(Exception):
    pass


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise TokenError("token expired")
    except jwt.InvalidTokenError:
        raise TokenError("invalid token")
    if payload.get("type") != expected_type:
        raise TokenError(f"expected a {expected_type} token")
    return payload


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise _unauthorized("missing bearer token")
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except TokenError as e:
        raise _unauthorized(str(e))

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise _unauthorized("user not found or inactive")
    return user


def require_role(*roles: str):
    """Dependency factory: Depends(require_role(Role.ADMIN, Role.DISPATCHER)).
    Role is always re-checked against the DB-loaded User (via
    get_current_user), never trusted from the request body/query - a client
    cannot elevate itself by claiming a different role in the payload."""
    allowed = set(roles)

    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{user.role}' is not permitted to perform this action",
            )
        return user

    return _check


def verify_ws_token(token: Optional[str], db: Session) -> Optional[User]:
    """Same access-token check as get_current_user, for the /ws WebSocket
    handshake (?token=... query param - browsers can't set a custom header
    on a WebSocket handshake, same constraint the old shared-token scheme
    had). Returns None instead of raising so the caller can close the
    socket with a clean app-defined code rather than an HTTP error."""
    if not token:
        return None
    try:
        payload = decode_token(token, expected_type="access")
    except TokenError:
        return None
    user = db.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        return None
    return user
