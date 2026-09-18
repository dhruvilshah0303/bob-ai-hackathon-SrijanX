"""
Real ambulance accounts + live GPS. Mounted at /api/ambulances/* in app.py.

An ambulance operator can only ever act on the one ambulance assigned to
them (Ambulance.operator_id == the caller's user id) - checked server-side
on every status/location write, never trusted from the request body.

Every GPS update refreshes the ambulance's live position in place (a
single-row UPDATE - always current, regardless of trip state). When the
ambulance has an active EN_ROUTE/ARRIVING trip, it also appends a
TripLocation history row - throttled to at most one every
_TRIP_LOCATION_MIN_INTERVAL_SEC seconds, so a real phone's watchPosition()
stream (which can fire faster than once a second) doesn't flood Postgres
with a row per tick.

Server timestamps every location update itself (datetime.now(UTC)) rather
than trusting a client-supplied timestamp - a phone's clock isn't a
trustworthy input, and "how stale is this" (is_online / last_location_update)
only means anything measured against the server's own clock.
"""
import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from auth_service import get_current_user, require_role
from db_session import get_db
from models import (
    Ambulance, AmbulanceStatus, AuditEvent, Role, Trip, TripLocation,
    TripStatus, User,
)

# Minimum gap between recorded TripLocation history rows for the same trip -
# a phone's watchPosition() can fire every second or faster, and recording
# every single tick would flood Postgres for no real benefit (Rule: don't
# write excessively frequent GPS records). The ambulance's LIVE position
# (Ambulance.lat/lng, above) still updates on every call regardless - only
# the durable per-trip history is throttled.
_TRIP_LOCATION_MIN_INTERVAL_SEC = 5


def _as_aware_utc(dt: datetime) -> datetime:
    """SQLite (unlike Postgres) doesn't actually preserve tzinfo through a
    round trip - a DateTime(timezone=True) column comes back naive after a
    fresh query, even though the same value was timezone-aware at the
    moment it was created in this same process. Postgres (production)
    round-trips it correctly, so this is a SQLite/local-dev-only quirk, but
    comparing a naive value against datetime.now(timezone.utc) raises
    TypeError either way if unnormalized - always assume naive means UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

router = APIRouter(prefix="/api/ambulances", tags=["ambulances"])


def _require_owner_or_privileged(user: User, ambulance: Ambulance) -> None:
    if user.role in (Role.ADMIN, Role.DISPATCHER):
        return
    if user.role == Role.AMBULANCE_OPERATOR and ambulance.operator_id == user.id:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized for this ambulance")


def _get_ambulance_or_404(db: Session, ambulance_id: str) -> Ambulance:
    ambulance = db.get(Ambulance, ambulance_id)
    if ambulance is None:
        raise HTTPException(status_code=404, detail="ambulance not found")
    return ambulance


class AmbulancePublic(BaseModel):
    id: str
    vehicle_number: str
    operator_id: Optional[str]
    status: str
    lat: Optional[float]
    lng: Optional[float]
    speed: Optional[float]
    heading: Optional[float]
    last_location_update: Optional[datetime]
    is_online: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AmbulanceCreateRequest(BaseModel):
    vehicle_number: str = Field(min_length=1, max_length=32)
    operator_id: Optional[str] = None


class AmbulanceUpdateRequest(BaseModel):
    vehicle_number: Optional[str] = Field(default=None, min_length=1, max_length=32)
    operator_id: Optional[str] = None


class StatusUpdateRequest(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v):
        valid = (
            AmbulanceStatus.AVAILABLE, AmbulanceStatus.DISPATCHED, AmbulanceStatus.EN_ROUTE,
            AmbulanceStatus.ARRIVING, AmbulanceStatus.AT_HOSPITAL, AmbulanceStatus.OFFLINE,
        )
        if v not in valid:
            raise ValueError(f"status must be one of {valid}")
        return v


class LocationUpdateRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    speed: Optional[float] = Field(default=None, ge=0, le=300)  # km/h - generous ceiling, catches obviously-bad GPS glitches
    heading: Optional[float] = Field(default=None, ge=0, le=360)


def _reject_if_null_island(req: LocationUpdateRequest) -> None:
    # (0, 0) is open ocean off West Africa - the single most common
    # "impossible" GPS reading, produced by an unfixed/failed lock rather
    # than a real position. Checked as a pair (not per-field), since a real
    # location can legitimately have lat or lng near zero on its own.
    if math.isclose(req.lat, 0.0, abs_tol=1e-6) and math.isclose(req.lng, 0.0, abs_tol=1e-6):
        raise HTTPException(status_code=400, detail="(0, 0) is not a valid ambulance location - likely a GPS fix failure")


@router.post("", response_model=AmbulancePublic, status_code=status.HTTP_201_CREATED)
def create_ambulance(
    req: AmbulanceCreateRequest,
    user: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    if req.operator_id is not None:
        operator = db.get(User, req.operator_id)
        if operator is None or operator.role != Role.AMBULANCE_OPERATOR:
            raise HTTPException(status_code=400, detail="operator_id must reference an AMBULANCE_OPERATOR user")
        already_assigned = db.query(Ambulance).filter(Ambulance.operator_id == req.operator_id).first()
        if already_assigned is not None:
            raise HTTPException(status_code=409, detail="this operator is already assigned to another ambulance")

    ambulance = Ambulance(vehicle_number=req.vehicle_number, operator_id=req.operator_id)
    db.add(ambulance)
    db.add(AuditEvent(
        user_id=user.id, event_type="ambulance_created",
        description=f"Ambulance '{req.vehicle_number}' created",
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="an ambulance with this vehicle_number already exists")
    db.refresh(ambulance)
    return AmbulancePublic.model_validate(ambulance)


@router.get("", response_model=list[AmbulancePublic])
def list_ambulances(
    user: User = Depends(require_role(Role.ADMIN, Role.DISPATCHER)),
    db: Session = Depends(get_db),
):
    return [AmbulancePublic.model_validate(a) for a in db.query(Ambulance).order_by(Ambulance.vehicle_number).all()]


@router.get("/{ambulance_id}", response_model=AmbulancePublic)
def get_ambulance(
    ambulance_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ambulance = _get_ambulance_or_404(db, ambulance_id)
    _require_owner_or_privileged(user, ambulance)
    return AmbulancePublic.model_validate(ambulance)


@router.patch("/{ambulance_id}", response_model=AmbulancePublic)
def update_ambulance(
    ambulance_id: str,
    req: AmbulanceUpdateRequest,
    user: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    ambulance = _get_ambulance_or_404(db, ambulance_id)
    updates = req.model_dump(exclude_unset=True)

    if "operator_id" in updates and updates["operator_id"] is not None:
        operator = db.get(User, updates["operator_id"])
        if operator is None or operator.role != Role.AMBULANCE_OPERATOR:
            raise HTTPException(status_code=400, detail="operator_id must reference an AMBULANCE_OPERATOR user")
        conflict = (
            db.query(Ambulance)
            .filter(Ambulance.operator_id == updates["operator_id"], Ambulance.id != ambulance.id)
            .first()
        )
        if conflict is not None:
            raise HTTPException(status_code=409, detail="this operator is already assigned to another ambulance")

    for field, value in updates.items():
        setattr(ambulance, field, value)

    if updates:
        db.add(AuditEvent(
            user_id=user.id, event_type="ambulance_updated",
            description=f"Ambulance '{ambulance.vehicle_number}' updated", event_metadata=str(updates),
        ))
        db.commit()
        db.refresh(ambulance)
    return AmbulancePublic.model_validate(ambulance)


@router.post("/{ambulance_id}/status", response_model=AmbulancePublic)
def update_status(
    ambulance_id: str,
    req: StatusUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ambulance = _get_ambulance_or_404(db, ambulance_id)
    _require_owner_or_privileged(user, ambulance)

    ambulance.status = req.status
    if req.status == AmbulanceStatus.OFFLINE:
        ambulance.is_online = False
    db.add(AuditEvent(
        user_id=user.id, event_type="ambulance_status_updated",
        description=f"Ambulance '{ambulance.vehicle_number}' status -> {req.status}",
    ))
    db.commit()
    db.refresh(ambulance)
    return AmbulancePublic.model_validate(ambulance)


@router.post("/{ambulance_id}/location", response_model=AmbulancePublic)
def update_location(
    ambulance_id: str,
    req: LocationUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ambulance = _get_ambulance_or_404(db, ambulance_id)
    # GPS is the one write an ADMIN should NOT be able to fake on an
    # operator's behalf (Rule: real GPS, not a dispatcher/admin console
    # click standing in for it) - only the assigned operator may post a
    # location, full stop.
    if not (user.role == Role.AMBULANCE_OPERATOR and ambulance.operator_id == user.id):
        raise HTTPException(status_code=403, detail="only this ambulance's assigned operator may send its location")

    _reject_if_null_island(req)

    now = datetime.now(timezone.utc)
    ambulance.lat = req.lat
    ambulance.lng = req.lng
    ambulance.speed = req.speed
    ambulance.heading = req.heading
    ambulance.last_location_update = now
    ambulance.is_online = True

    active_trip = (
        db.query(Trip)
        .filter(Trip.ambulance_id == ambulance.id, Trip.status.in_((TripStatus.EN_ROUTE, TripStatus.ARRIVING)))
        .order_by(Trip.created_at.desc())
        .first()
    )
    if active_trip is not None:
        last_point = (
            db.query(TripLocation)
            .filter(TripLocation.trip_id == active_trip.id)
            .order_by(TripLocation.recorded_at.desc())
            .first()
        )
        stale_enough = last_point is None or (now - _as_aware_utc(last_point.recorded_at)).total_seconds() >= _TRIP_LOCATION_MIN_INTERVAL_SEC
        if stale_enough:
            db.add(TripLocation(trip_id=active_trip.id, lat=req.lat, lng=req.lng, speed=req.speed, heading=req.heading, recorded_at=now))

    db.commit()
    db.refresh(ambulance)
    return AmbulancePublic.model_validate(ambulance)
