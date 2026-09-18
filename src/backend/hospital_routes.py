"""
Real, DB-backed hospital accounts: create/verify (ADMIN), read, and
resource/specialist updates scoped to the caller's own hospital
(HOSPITAL_ADMIN) or any hospital (ADMIN).

Mounted at /api/hospitals/* in app.py, alongside (not replacing) the
existing in-memory GET /api/hospitals and POST /api/hospitals/{id}/capacity
demo endpoints already in app.py - those still back the not-yet-migrated
trip/recommendation engine (see models.py's module docstring); no path here
collides with them (no existing GET /api/hospitals/{id}, PATCH .../resources,
or .../specialists route). Cutting the trip engine itself over to read these
real hospital records is a later phase - see app.py's include_router comment.

Every write here: validates input for real (rejects an inconsistent
free-vs-total count instead of silently clamping it - Rule: real production
validation, not demo-shaped leniency), commits to Postgres/SQLite, writes an
AuditEvent row, and broadcasts a websocket event over the same connection
manager the rest of the app already uses (a dedicated /ws/hospital/{id}
channel is later work - see realtime.md once written).
"""
import json
from datetime import datetime, timezone
from typing import Callable, Coroutine, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from auth_service import get_current_user, require_role
from db_session import get_db
from models import (
    AuditEvent, Hospital, HospitalAvailability, HospitalResources,
    HospitalSpecialist, HospitalVerificationStatus, Role, User,
)

router = APIRouter(prefix="/api/hospitals", tags=["hospitals"])

# Set once by app.py at startup (app.include_router happens after the
# websocket ConnectionManager exists) - avoids a circular import between
# this module and app.py. A broadcast failure must never fail the HTTP
# request that triggered it, so _broadcast() below always swallows errors.
_broadcaster: Optional[Callable[[dict], Coroutine]] = None


def set_broadcaster(fn: Callable[[dict], Coroutine]) -> None:
    global _broadcaster
    _broadcaster = fn


async def _broadcast(message: dict) -> None:
    if _broadcaster is None:
        return
    try:
        await _broadcaster(message)
    except Exception:
        pass  # best-effort real-time push; the DB write already succeeded


def _write_audit(db: Session, user: User, event_type: str, description: str, metadata: dict) -> None:
    db.add(AuditEvent(
        user_id=user.id,
        event_type=event_type,
        description=description,
        event_metadata=json.dumps(metadata),
    ))


def _require_own_hospital_or_admin(user: User, hospital_id: str) -> None:
    if user.role == Role.ADMIN:
        return
    if user.role == Role.HOSPITAL_ADMIN and user.hospital_id == hospital_id:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized for this hospital")


def _get_hospital_or_404(db: Session, hospital_id: str) -> Hospital:
    hospital = db.get(Hospital, hospital_id)
    if hospital is None:
        raise HTTPException(status_code=404, detail="hospital not found")
    return hospital


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ResourcesPublic(BaseModel):
    icu_beds_total: int
    icu_beds_free: int
    ward_beds_total: int
    ward_beds_free: int
    ed_bays_total: int
    ed_bays_occupied: int
    ventilator_total: int
    ventilator_available: int
    avg_historical_wait_min: float
    updated_at: datetime

    model_config = {"from_attributes": True}


class SpecialistPublic(BaseModel):
    id: str
    type: str
    doctor_name: Optional[str]
    on_duty: bool
    on_call: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class HospitalPublic(BaseModel):
    id: str
    name: str
    registration_number: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    address: Optional[str]
    lat: float
    lng: float
    verification_status: str
    availability_status: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    resources: Optional[ResourcesPublic]
    specialists: List[SpecialistPublic]

    model_config = {"from_attributes": True}


def _serialize(hospital: Hospital) -> HospitalPublic:
    return HospitalPublic.model_validate(hospital)


class HospitalCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    registration_number: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=32)
    email: Optional[str] = Field(default=None, max_length=255)
    address: Optional[str] = Field(default=None, max_length=400)
    lat: float
    lng: float


class HospitalVerificationRequest(BaseModel):
    verification_status: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("verification_status")
    @classmethod
    def _valid_status(cls, v):
        if v is not None and v not in (
            HospitalVerificationStatus.PENDING,
            HospitalVerificationStatus.VERIFIED,
            HospitalVerificationStatus.SUSPENDED,
        ):
            raise ValueError("invalid verification_status")
        return v


class ResourcesUpdateRequest(BaseModel):
    icu_beds_total: Optional[int] = Field(default=None, ge=0)
    icu_beds_free: Optional[int] = Field(default=None, ge=0)
    ward_beds_total: Optional[int] = Field(default=None, ge=0)
    ward_beds_free: Optional[int] = Field(default=None, ge=0)
    ed_bays_total: Optional[int] = Field(default=None, ge=0)
    ed_bays_occupied: Optional[int] = Field(default=None, ge=0)
    ventilator_total: Optional[int] = Field(default=None, ge=0)
    ventilator_available: Optional[int] = Field(default=None, ge=0)
    availability_status: Optional[str] = None

    @field_validator("availability_status")
    @classmethod
    def _valid_availability(cls, v):
        valid = (HospitalAvailability.ONLINE, HospitalAvailability.BUSY,
                 HospitalAvailability.FULL, HospitalAvailability.OFFLINE)
        if v is not None and v not in valid:
            raise ValueError(f"availability_status must be one of {valid}")
        return v


class SpecialistCreateRequest(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    doctor_name: Optional[str] = Field(default=None, max_length=200)
    on_duty: bool = False
    on_call: bool = False


class SpecialistUpdateRequest(BaseModel):
    doctor_name: Optional[str] = Field(default=None, max_length=200)
    on_duty: Optional[bool] = None
    on_call: Optional[bool] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post("", response_model=HospitalPublic, status_code=status.HTTP_201_CREATED)
async def create_hospital(
    req: HospitalCreateRequest,
    user: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    hospital = Hospital(
        name=req.name, registration_number=req.registration_number, phone=req.phone,
        email=req.email, address=req.address, lat=req.lat, lng=req.lng,
        verification_status=HospitalVerificationStatus.PENDING,
        availability_status=HospitalAvailability.OFFLINE,
        is_active=True,
    )
    db.add(hospital)
    db.flush()
    db.add(HospitalResources(hospital_id=hospital.id))  # all-zero defaults until the hospital reports real numbers
    _write_audit(db, user, "hospital_created", f"Hospital '{hospital.name}' created", {"hospital_id": hospital.id})
    db.commit()
    db.refresh(hospital)

    await _broadcast({"type": "HOSPITAL_CREATED", "hospital_id": hospital.id})
    return _serialize(hospital)


@router.get("/{hospital_id}", response_model=HospitalPublic)
def get_hospital(hospital_id: str, db: Session = Depends(get_db)):
    return _serialize(_get_hospital_or_404(db, hospital_id))


@router.patch("/{hospital_id}", response_model=HospitalPublic)
async def update_verification(
    hospital_id: str,
    req: HospitalVerificationRequest,
    user: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    hospital = _get_hospital_or_404(db, hospital_id)
    changed = {}
    if req.verification_status is not None:
        hospital.verification_status = req.verification_status
        changed["verification_status"] = req.verification_status
    if req.is_active is not None:
        hospital.is_active = req.is_active
        changed["is_active"] = req.is_active

    if changed:
        _write_audit(db, user, "hospital_verification_changed", f"Hospital '{hospital.name}' updated by admin", changed)
        db.commit()
        db.refresh(hospital)
        await _broadcast({"type": "HOSPITAL_VERIFICATION_UPDATED", "hospital_id": hospital.id, "changed": changed})
    return _serialize(hospital)


@router.patch("/{hospital_id}/resources", response_model=HospitalPublic)
async def update_resources(
    hospital_id: str,
    req: ResourcesUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_own_hospital_or_admin(user, hospital_id)
    hospital = _get_hospital_or_404(db, hospital_id)
    resources = hospital.resources
    if resources is None:
        raise HTTPException(status_code=500, detail="hospital has no resources row (data integrity issue)")

    updates = req.model_dump(exclude_unset=True, exclude={"availability_status"})
    # Validate free/occupied against whichever total is in effect (the new
    # value if this same request changes it, otherwise the current one) -
    # real validation, not the old demo endpoint's silent clamping.
    pairs = [
        ("icu_beds_free", "icu_beds_total"),
        ("ward_beds_free", "ward_beds_total"),
        ("ventilator_available", "ventilator_total"),
    ]
    for free_field, total_field in pairs:
        if free_field in updates:
            total = updates.get(total_field, getattr(resources, total_field))
            if updates[free_field] > total:
                raise HTTPException(status_code=400, detail=f"{free_field} ({updates[free_field]}) cannot exceed {total_field} ({total})")
    if "ed_bays_occupied" in updates:
        total = updates.get("ed_bays_total", resources.ed_bays_total)
        if updates["ed_bays_occupied"] > total:
            raise HTTPException(status_code=400, detail=f"ed_bays_occupied ({updates['ed_bays_occupied']}) cannot exceed ed_bays_total ({total})")

    for field, value in updates.items():
        setattr(resources, field, value)
    if req.availability_status is not None:
        hospital.availability_status = req.availability_status
        updates["availability_status"] = req.availability_status

    if not updates:
        return _serialize(hospital)

    _write_audit(db, user, "hospital_capacity_updated", f"Hospital '{hospital.name}' resources updated", updates)
    db.commit()
    db.refresh(hospital)

    await _broadcast({"type": "HOSPITAL_CAPACITY_UPDATED", "hospital_id": hospital.id, "changed": updates})
    return _serialize(hospital)


@router.post("/{hospital_id}/specialists", response_model=SpecialistPublic, status_code=status.HTTP_201_CREATED)
async def create_specialist(
    hospital_id: str,
    req: SpecialistCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_own_hospital_or_admin(user, hospital_id)
    hospital = _get_hospital_or_404(db, hospital_id)
    specialist = HospitalSpecialist(
        hospital_id=hospital.id, type=req.type, doctor_name=req.doctor_name,
        on_duty=req.on_duty, on_call=req.on_call,
    )
    db.add(specialist)
    _write_audit(db, user, "specialist_added", f"Specialist '{req.type}' added to '{hospital.name}'", {"type": req.type})
    db.commit()
    db.refresh(specialist)

    await _broadcast({"type": "SPECIALIST_AVAILABILITY_UPDATED", "hospital_id": hospital.id, "specialist_id": specialist.id})
    return SpecialistPublic.model_validate(specialist)


@router.patch("/{hospital_id}/specialists/{specialist_id}", response_model=SpecialistPublic)
async def update_specialist(
    hospital_id: str,
    specialist_id: str,
    req: SpecialistUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_own_hospital_or_admin(user, hospital_id)
    specialist = db.get(HospitalSpecialist, specialist_id)
    if specialist is None or specialist.hospital_id != hospital_id:
        raise HTTPException(status_code=404, detail="specialist not found for this hospital")

    updates = req.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(specialist, field, value)

    if updates:
        hospital = _get_hospital_or_404(db, hospital_id)
        _write_audit(db, user, "specialist_updated", f"Specialist '{specialist.type}' at '{hospital.name}' updated", updates)
        db.commit()
        db.refresh(specialist)
        await _broadcast({
            "type": "SPECIALIST_AVAILABILITY_UPDATED",
            "hospital_id": hospital_id,
            "specialist_id": specialist.id,
            "changed": updates,
        })
    return SpecialistPublic.model_validate(specialist)
