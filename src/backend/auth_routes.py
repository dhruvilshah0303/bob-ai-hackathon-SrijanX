"""
Real authentication endpoints (Rule: "Replace shared-token authentication
with real user accounts"). Mounted at /api/auth/* in app.py.

POST /api/auth/register - public self-signup, DISPATCHER/AMBULANCE_OPERATOR
    only. HOSPITAL_ADMIN and ADMIN accounts are provisioned directly (dev:
    scripts/seed_development.py; real deployment: an ADMIN using the future
    admin user-management endpoint - Phase 9) because both need a
    hospital_id/elevated trust an anonymous signup form can't establish.
POST /api/auth/login    - email+password -> access + refresh token pair.
POST /api/auth/logout   - stateless (no server-side session store - see
    auth_service.py's docstring on why); just confirms so the frontend can
    discard its stored tokens.
GET  /api/auth/me       - the caller's own profile, from their access token.
POST /api/auth/refresh  - refresh token -> new access token.
"""
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth_service import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from db_session import get_db
from models import Role, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Roles an anonymous visitor may self-register as. HOSPITAL_ADMIN needs a
# verified hospital_id assigned by an ADMIN; ADMIN needs to be provisioned
# by someone already trusted. Neither can come from an open signup form.
_SELF_REGISTERABLE_ROLES = (Role.DISPATCHER, Role.AMBULANCE_OPERATOR)


def _clean_email(v: str) -> str:
    v = v.strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("not a valid email address")
    return v


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str
    password: str = Field(min_length=8, max_length=128)
    phone: Optional[str] = Field(default=None, max_length=32)
    role: Literal[Role.DISPATCHER, Role.AMBULANCE_OPERATOR]

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v):
        return _clean_email(v)

    @field_validator("name", "phone")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v):
        return _clean_email(v)


class RefreshRequest(BaseModel):
    refresh_token: str


class UserPublic(BaseModel):
    id: str
    name: str
    email: str
    phone: Optional[str]
    role: str
    hospital_id: Optional[str]
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime]

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserPublic


def _tokens_for(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
        user=UserPublic.model_validate(user),
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if req.role not in _SELF_REGISTERABLE_ROLES:
        raise HTTPException(status_code=400, detail="this role cannot self-register")

    user = User(
        name=req.name,
        email=req.email,
        phone=req.phone,
        password_hash=hash_password(req.password),
        role=req.role,
        hospital_id=None,
        is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="an account with this email already exists")
    db.refresh(user)
    return _tokens_for(user)


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    # Same generic error whether the email doesn't exist or the password is
    # wrong - never reveal which one, that's an account-enumeration leak.
    invalid = HTTPException(status_code=401, detail="invalid email or password")
    if user is None or not verify_password(req.password, user.password_hash):
        raise invalid
    if not user.is_active:
        raise HTTPException(status_code=403, detail="this account has been deactivated")

    user.last_login = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return _tokens_for(user)


@router.post("/logout")
def logout(user: User = Depends(get_current_user)):
    # Stateless tokens, no server-side session store (see auth_service.py) -
    # there is nothing to invalidate server-side. This endpoint exists so
    # the frontend has a real call to make when the user clicks "log out",
    # and so a future revocation list has an obvious place to plug in.
    return {"ok": True}


@router.get("/me", response_model=UserPublic)
def me(user: User = Depends(get_current_user)):
    return UserPublic.model_validate(user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)):
    try:
        payload = decode_token(req.refresh_token, expected_type="refresh")
    except TokenError as e:
        raise HTTPException(status_code=401, detail=str(e))

    user = db.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or inactive")
    return _tokens_for(user)
