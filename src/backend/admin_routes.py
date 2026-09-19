"""
ADMIN-only user management: create a user of ANY role (including
HOSPITAL_ADMIN/ADMIN, which POST /api/auth/register deliberately refuses -
see auth_routes.py), list users, and activate/deactivate an account.

Without this, onboarding a real hospital had no path except directly
editing the database (scripts/seed_development.py, which itself refuses to
run in production) - this is that path for a real deployment.
"""
import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth_service import hash_password, require_role
from db_session import get_db
from models import Hospital, Role, User

router = APIRouter(prefix="/api/admin", tags=["admin"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserPublic(BaseModel):
    id: str
    name: str
    email: str
    phone: Optional[str]
    role: str
    hospital_id: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login: Optional[datetime]

    model_config = {"from_attributes": True}


class AdminCreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str
    password: str = Field(min_length=8, max_length=128)
    phone: Optional[str] = Field(default=None, max_length=32)
    role: str
    hospital_id: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v):
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("not a valid email address")
        return v

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v):
        if v not in Role.ALL:
            raise ValueError(f"role must be one of {Role.ALL}")
        return v


class AdminUpdateUserRequest(BaseModel):
    is_active: Optional[bool] = None
    hospital_id: Optional[str] = None


@router.post("/users", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def create_user(
    req: AdminCreateUserRequest,
    admin: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    if req.role == Role.HOSPITAL_ADMIN:
        if not req.hospital_id:
            raise HTTPException(status_code=400, detail="hospital_id is required for a HOSPITAL_ADMIN account")
        if db.get(Hospital, req.hospital_id) is None:
            raise HTTPException(status_code=404, detail="hospital not found")
    elif req.hospital_id:
        raise HTTPException(status_code=400, detail="hospital_id is only valid for a HOSPITAL_ADMIN account")

    user = User(
        name=req.name, email=req.email, phone=req.phone,
        password_hash=hash_password(req.password), role=req.role,
        hospital_id=req.hospital_id, is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="an account with this email already exists")
    db.refresh(user)
    return UserPublic.model_validate(user)


@router.get("/users", response_model=List[UserPublic])
def list_users(
    admin: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    return [UserPublic.model_validate(u) for u in db.query(User).order_by(User.created_at.desc()).all()]


@router.patch("/users/{user_id}", response_model=UserPublic)
def update_user(
    user_id: str,
    req: AdminUpdateUserRequest,
    admin: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    if req.hospital_id is not None:
        if user.role != Role.HOSPITAL_ADMIN:
            raise HTTPException(status_code=400, detail="hospital_id can only be set on a HOSPITAL_ADMIN account")
        if db.get(Hospital, req.hospital_id) is None:
            raise HTTPException(status_code=404, detail="hospital not found")
        user.hospital_id = req.hospital_id

    if req.is_active is not None:
        user.is_active = req.is_active

    db.commit()
    db.refresh(user)
    return UserPublic.model_validate(user)
