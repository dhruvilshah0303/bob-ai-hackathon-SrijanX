"""
The core coordination workflow, now backed by real DB hospitals/emergencies/
ambulances instead of app.py's in-memory demo trip engine:

  emergency -> recommendation (real hospitals + real ETA)
            -> dispatcher requests a hospital
            -> hospital accepts (transactional reservation) or declines
            -> dispatcher creates a trip against an available ambulance
            -> trip lifecycle: start -> arrive -> complete
            -> dynamic reroute (release old reservation, reserve new)
            -> cancel

Every consequential step (hospital selection, reroute) is dispatcher/admin-
initiated - never automatic - and every state-changing call writes an
AuditEvent and broadcasts a websocket event. Mounted at /api/hospital-
requests/* and /api/trips/* in app.py.
"""
import json
from datetime import datetime, timezone
from typing import Callable, Coroutine, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

import eta_service
import hospital_service
import notification_service
import reservation_service
import severity_config
from auth_service import get_current_user, require_role
from db_session import get_db
from models import (
    AiDecision, Ambulance, AmbulanceStatus, AuditEvent, Emergency,
    EmergencyStatus, Hospital, HospitalRequest, HospitalRequestStatus,
    HospitalVerificationStatus, Role, Trip, TripEvent, TripStatus, User,
)
from recommendation_engine import recommend

router = APIRouter(tags=["trips"])

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
        pass


def _audit(db: Session, user: User, event_type: str, description: str, emergency_id: Optional[str] = None, **metadata) -> None:
    db.add(AuditEvent(
        user_id=user.id, emergency_id=emergency_id, event_type=event_type,
        description=description, event_metadata=json.dumps(metadata) if metadata else None,
    ))


def _requires_icu(emergency: Emergency) -> bool:
    """Deterministic, from the same structured table triage already used
    (Rule: hard resource constraints are deterministic/auditable, not
    re-derived from free-form model output)."""
    rule = severity_config.condition_by_code().get(emergency.condition_code, {})
    return "icu" in rule.get("required", [])


def _hospital_admins(db: Session, hospital_id: str) -> List[User]:
    return db.query(User).filter(User.hospital_id == hospital_id, User.role == Role.HOSPITAL_ADMIN, User.is_active.is_(True)).all()


def _get_emergency_or_404(db: Session, emergency_id: str) -> Emergency:
    e = db.get(Emergency, emergency_id)
    if e is None:
        raise HTTPException(status_code=404, detail="emergency not found")
    return e


def _get_hospital_request_or_404(db: Session, request_id: str) -> HospitalRequest:
    r = db.get(HospitalRequest, request_id)
    if r is None:
        raise HTTPException(status_code=404, detail="hospital request not found")
    return r


def _get_trip_or_404(db: Session, trip_id: str) -> Trip:
    t = db.get(Trip, trip_id)
    if t is None:
        raise HTTPException(status_code=404, detail="trip not found")
    return t


def _require_trip_access(user: User, trip: Trip, ambulance: Optional[Ambulance], dest_hospital_id: Optional[str]) -> None:
    if user.role in (Role.ADMIN, Role.DISPATCHER):
        return
    if user.role == Role.AMBULANCE_OPERATOR and ambulance is not None and ambulance.operator_id == user.id:
        return
    if user.role == Role.HOSPITAL_ADMIN and dest_hospital_id is not None and user.hospital_id == dest_hospital_id:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized for this trip")


# ---------------------------------------------------------------------------
# Recommendation - real hospitals + real ETA, existing engine unchanged
# ---------------------------------------------------------------------------
class RecommendationResponse(BaseModel):
    generated_at: str
    condition_code: str
    condition_label: str
    candidates: list
    recommended_hospital_id: Optional[str]
    required_capabilities: list
    preferred_specialist: Optional[str]


@router.get("/api/emergencies/{emergency_id}/recommendation", response_model=RecommendationResponse)
def get_recommendation(
    emergency_id: str,
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    emergency = _get_emergency_or_404(db, emergency_id)
    if not emergency.condition_code:
        raise HTTPException(status_code=400, detail="emergency has no triage result yet")

    condition_rule = severity_config.condition_by_code()[emergency.condition_code]
    hospitals = hospital_service.hospitals_for_recommendation(db)
    if not hospitals:
        raise HTTPException(status_code=503, detail="no verified hospitals available to recommend")

    eta_lookup = {
        hid: eta_service.get_eta(emergency.lat, emergency.lng, h["lat"], h["lng"], hid, seed_key=emergency.id)
        for hid, h in hospitals.items()
    }
    rules = severity_config.load()
    snapshot = recommend(list(hospitals.values()), eta_lookup, condition_rule, rules["scoring_weights"], rules["stale_data_threshold_minutes"])

    top = next((c for c in snapshot["candidates"] if c["hospital_id"] == snapshot["recommended_hospital_id"]), None)
    db.add(AuditEvent(
        user_id=user.id, emergency_id=emergency.id, event_type="recommendation_generated",
        description=f"Recommendation generated for {condition_rule['label']}",
        event_metadata=json.dumps({"recommended_hospital_id": snapshot["recommended_hospital_id"]}),
    ))
    db.add(AiDecision(
        emergency_id=emergency.id, decision_type="hospital_recommendation",
        recommended_hospital_id=snapshot["recommended_hospital_id"],
        reasoning="; ".join(top["reasons"]) if top else None,
        constraints=json.dumps(snapshot["required_capabilities"]),
        score_breakdown=json.dumps(top["score_breakdown"]) if top and top.get("score_breakdown") else None,
    ))
    db.commit()

    return RecommendationResponse(
        generated_at=datetime.now(timezone.utc).isoformat(),
        condition_code=emergency.condition_code,
        condition_label=condition_rule["label"],
        candidates=snapshot["candidates"],
        recommended_hospital_id=snapshot["recommended_hospital_id"],
        required_capabilities=snapshot["required_capabilities"],
        preferred_specialist=snapshot["preferred_specialist"],
    )


# ---------------------------------------------------------------------------
# Hospital request / acceptance
# ---------------------------------------------------------------------------
class HospitalRequestCreate(BaseModel):
    emergency_id: str
    hospital_id: str


class HospitalRequestPublic(BaseModel):
    id: str
    emergency_id: str
    hospital_id: str
    status: str
    requested_at: datetime
    responded_at: Optional[datetime]
    response_reason: Optional[str]

    model_config = {"from_attributes": True}


class DeclineRequest(BaseModel):
    reason: Optional[str] = None


def _require_own_hospital_or_privileged(user: User, hospital_id: str) -> None:
    if user.role in (Role.ADMIN, Role.DISPATCHER):
        return
    if user.role == Role.HOSPITAL_ADMIN and user.hospital_id == hospital_id:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not authorized for this hospital")


@router.get("/api/hospitals/{hospital_id}/hospital-requests", response_model=List[HospitalRequestPublic])
def list_hospital_requests(
    hospital_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_own_hospital_or_privileged(user, hospital_id)
    rows = (
        db.query(HospitalRequest)
        .filter(HospitalRequest.hospital_id == hospital_id)
        .order_by(HospitalRequest.requested_at.desc())
        .all()
    )
    return [HospitalRequestPublic.model_validate(r) for r in rows]


@router.post("/api/hospital-requests", response_model=HospitalRequestPublic, status_code=status.HTTP_201_CREATED)
async def create_hospital_request(
    req: HospitalRequestCreate,
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    emergency = _get_emergency_or_404(db, req.emergency_id)
    if emergency.status != EmergencyStatus.AWAITING_HOSPITAL:
        raise HTTPException(status_code=409, detail=f"emergency is not awaiting a hospital (status={emergency.status})")

    hospital = db.get(Hospital, req.hospital_id)
    if hospital is None:
        raise HTTPException(status_code=404, detail="hospital not found")
    if hospital.verification_status != HospitalVerificationStatus.VERIFIED or not hospital.is_active:
        raise HTTPException(status_code=400, detail="hospital is not a verified, active facility")

    hospital_request = HospitalRequest(emergency_id=emergency.id, hospital_id=hospital.id)
    db.add(hospital_request)
    emergency.status = EmergencyStatus.HOSPITAL_REQUESTED
    _audit(db, user, "hospital_request_created", f"Requested '{hospital.name}' for emergency", emergency.id, hospital_id=hospital.id)

    for admin in _hospital_admins(db, hospital.id):
        notification_service.notify(
            db, admin.id, "HOSPITAL_REQUEST",
            "New emergency request", f"A {emergency.severity or ''} severity patient has been requested for your hospital.",
        )
    db.commit()
    db.refresh(hospital_request)

    await _broadcast({"type": "HOSPITAL_REQUEST_CREATED", "request_id": hospital_request.id, "hospital_id": hospital.id, "emergency_id": emergency.id})
    return HospitalRequestPublic.model_validate(hospital_request)


@router.post("/api/hospital-requests/{request_id}/accept", response_model=HospitalRequestPublic)
async def accept_hospital_request(
    request_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    hospital_request = _get_hospital_request_or_404(db, request_id)
    if user.role not in (Role.ADMIN,) and not (user.role == Role.HOSPITAL_ADMIN and user.hospital_id == hospital_request.hospital_id):
        raise HTTPException(status_code=403, detail="not authorized to accept for this hospital")
    if hospital_request.status != HospitalRequestStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"request is not pending (status={hospital_request.status})")

    emergency = _get_emergency_or_404(db, hospital_request.emergency_id)
    reservations = reservation_service.reserve(db, hospital_request.hospital_id, emergency.id, requires_icu=_requires_icu(emergency))
    if reservations is None:
        raise HTTPException(status_code=409, detail="required capacity is no longer available at this hospital")

    hospital_request.status = HospitalRequestStatus.ACCEPTED
    hospital_request.responded_at = datetime.now(timezone.utc)
    emergency.status = EmergencyStatus.HOSPITAL_ACCEPTED
    _audit(db, user, "hospital_accepted", "Hospital accepted the emergency", emergency.id, hospital_id=hospital_request.hospital_id)

    dispatcher = db.get(User, emergency.created_by)
    if dispatcher is not None:
        notification_service.notify(db, dispatcher.id, "HOSPITAL_ACCEPTED", "Hospital accepted", "The requested hospital has accepted this patient.")
    db.commit()
    db.refresh(hospital_request)

    await _broadcast({"type": "HOSPITAL_ACCEPTED", "request_id": hospital_request.id, "emergency_id": emergency.id})
    await _broadcast({"type": "RESERVATION_CREATED", "emergency_id": emergency.id, "hospital_id": hospital_request.hospital_id})
    return HospitalRequestPublic.model_validate(hospital_request)


@router.post("/api/hospital-requests/{request_id}/decline", response_model=HospitalRequestPublic)
async def decline_hospital_request(
    request_id: str,
    req: DeclineRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    hospital_request = _get_hospital_request_or_404(db, request_id)
    if user.role not in (Role.ADMIN,) and not (user.role == Role.HOSPITAL_ADMIN and user.hospital_id == hospital_request.hospital_id):
        raise HTTPException(status_code=403, detail="not authorized to decline for this hospital")
    if hospital_request.status != HospitalRequestStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"request is not pending (status={hospital_request.status})")

    emergency = _get_emergency_or_404(db, hospital_request.emergency_id)
    hospital_request.status = HospitalRequestStatus.DECLINED
    hospital_request.responded_at = datetime.now(timezone.utc)
    hospital_request.response_reason = req.reason
    emergency.status = EmergencyStatus.AWAITING_HOSPITAL
    _audit(db, user, "hospital_declined", req.reason or "Hospital declined the emergency", emergency.id, hospital_id=hospital_request.hospital_id)

    dispatcher = db.get(User, emergency.created_by)
    if dispatcher is not None:
        notification_service.notify(db, dispatcher.id, "HOSPITAL_DECLINED", "Hospital declined", req.reason or "The requested hospital declined this patient.")
    db.commit()
    db.refresh(hospital_request)

    await _broadcast({"type": "HOSPITAL_DECLINED", "request_id": hospital_request.id, "emergency_id": emergency.id, "reason": req.reason})
    return HospitalRequestPublic.model_validate(hospital_request)


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------
class TripCreate(BaseModel):
    emergency_id: str
    ambulance_id: str


class TripPublic(BaseModel):
    id: str
    emergency_id: str
    ambulance_id: Optional[str]
    dest_hospital_id: Optional[str]
    status: str
    created_at: datetime
    started_at: Optional[datetime]
    arrived_at: Optional[datetime]
    completed_at: Optional[datetime]
    cancelled_at: Optional[datetime]

    model_config = {"from_attributes": True}


class RerouteRequest(BaseModel):
    new_hospital_id: str


def _log_event(db: Session, trip: Trip, event_type: str, user: User, details: Optional[dict] = None) -> None:
    db.add(TripEvent(trip_id=trip.id, event_type=event_type, actor_user_id=user.id, details=json.dumps(details) if details else None))


@router.get("/api/hospitals/{hospital_id}/trips", response_model=List[TripPublic])
def list_hospital_trips(
    hospital_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_own_hospital_or_privileged(user, hospital_id)
    rows = (
        db.query(Trip)
        .filter(Trip.dest_hospital_id == hospital_id)
        .order_by(Trip.created_at.desc())
        .all()
    )
    return [TripPublic.model_validate(t) for t in rows]


@router.post("/api/trips", response_model=TripPublic, status_code=status.HTTP_201_CREATED)
async def create_trip(
    req: TripCreate,
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    emergency = _get_emergency_or_404(db, req.emergency_id)
    if emergency.status != EmergencyStatus.HOSPITAL_ACCEPTED:
        raise HTTPException(status_code=409, detail="emergency has no accepted hospital yet")

    accepted_request = (
        db.query(HospitalRequest)
        .filter(HospitalRequest.emergency_id == emergency.id, HospitalRequest.status == HospitalRequestStatus.ACCEPTED)
        .order_by(HospitalRequest.responded_at.desc())
        .first()
    )
    if accepted_request is None:
        raise HTTPException(status_code=409, detail="no accepted hospital request found for this emergency")

    ambulance = db.get(Ambulance, req.ambulance_id)
    if ambulance is None:
        raise HTTPException(status_code=404, detail="ambulance not found")
    if ambulance.status != AmbulanceStatus.AVAILABLE:
        raise HTTPException(status_code=409, detail=f"ambulance is not available (status={ambulance.status})")

    trip = Trip(emergency_id=emergency.id, ambulance_id=ambulance.id, dest_hospital_id=accepted_request.hospital_id, status=TripStatus.DISPATCHED)
    db.add(trip)
    db.flush()
    ambulance.status = AmbulanceStatus.DISPATCHED
    emergency.status = EmergencyStatus.AMBULANCE_DISPATCHED
    _log_event(db, trip, "TRIP_CREATED", user)
    _audit(db, user, "trip_created", f"Trip created for ambulance {ambulance.vehicle_number}", emergency.id, trip_id=trip.id)

    if ambulance.operator_id:
        notification_service.notify(db, ambulance.operator_id, "TRIP_ASSIGNED", "New trip assigned", "You have been assigned a new emergency transport.")
    db.commit()
    db.refresh(trip)

    await _broadcast({"type": "TRIP_CREATED", "trip_id": trip.id, "emergency_id": emergency.id, "ambulance_id": ambulance.id})
    return TripPublic.model_validate(trip)


@router.get("/api/trips", response_model=List[TripPublic])
def list_trips(
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    return [TripPublic.model_validate(t) for t in db.query(Trip).order_by(Trip.created_at.desc()).all()]


_ACTIVE_TRIP_STATUSES = (TripStatus.DISPATCHED, TripStatus.EN_ROUTE, TripStatus.ARRIVING, TripStatus.ARRIVED)


@router.get("/api/trips/mine", response_model=TripPublic)
def get_my_trip(
    user: User = Depends(require_role(Role.AMBULANCE_OPERATOR)),
    db: Session = Depends(get_db),
):
    """So the ambulance portal can find its current trip without list-all
    access (DISPATCHER/ADMIN only) - registered before the dynamic
    /{trip_id} route below, same reasoning as ambulance_routes.py's /me."""
    ambulance = db.query(Ambulance).filter(Ambulance.operator_id == user.id).first()
    if ambulance is None:
        raise HTTPException(status_code=404, detail="no ambulance is assigned to your account")
    trip = (
        db.query(Trip)
        .filter(Trip.ambulance_id == ambulance.id, Trip.status.in_(_ACTIVE_TRIP_STATUSES))
        .order_by(Trip.created_at.desc())
        .first()
    )
    if trip is None:
        raise HTTPException(status_code=404, detail="no active trip assigned")
    return TripPublic.model_validate(trip)


@router.get("/api/trips/{trip_id}", response_model=TripPublic)
def get_trip(
    trip_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = _get_trip_or_404(db, trip_id)
    ambulance = db.get(Ambulance, trip.ambulance_id) if trip.ambulance_id else None
    _require_trip_access(user, trip, ambulance, trip.dest_hospital_id)
    return TripPublic.model_validate(trip)


@router.post("/api/trips/{trip_id}/start", response_model=TripPublic)
async def start_trip(
    trip_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = _get_trip_or_404(db, trip_id)
    ambulance = db.get(Ambulance, trip.ambulance_id) if trip.ambulance_id else None
    _require_trip_access(user, trip, ambulance, trip.dest_hospital_id)
    if trip.status != TripStatus.DISPATCHED:
        raise HTTPException(status_code=409, detail=f"trip cannot be started from status {trip.status}")

    trip.status = TripStatus.EN_ROUTE
    trip.started_at = datetime.now(timezone.utc)
    if ambulance is not None:
        ambulance.status = AmbulanceStatus.EN_ROUTE
    emergency = _get_emergency_or_404(db, trip.emergency_id)
    emergency.status = EmergencyStatus.EN_ROUTE
    _log_event(db, trip, "TRIP_STARTED", user)
    _audit(db, user, "trip_started", "Trip started", emergency.id, trip_id=trip.id)
    db.commit()
    db.refresh(trip)

    await _broadcast({"type": "TRIP_STARTED", "trip_id": trip.id})
    return TripPublic.model_validate(trip)


@router.post("/api/trips/{trip_id}/arrive", response_model=TripPublic)
async def arrive_trip(
    trip_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = _get_trip_or_404(db, trip_id)
    ambulance = db.get(Ambulance, trip.ambulance_id) if trip.ambulance_id else None
    _require_trip_access(user, trip, ambulance, trip.dest_hospital_id)
    if trip.status not in (TripStatus.EN_ROUTE, TripStatus.ARRIVING):
        raise HTTPException(status_code=409, detail=f"trip cannot arrive from status {trip.status}")

    trip.status = TripStatus.ARRIVED
    trip.arrived_at = datetime.now(timezone.utc)
    if ambulance is not None:
        ambulance.status = AmbulanceStatus.AT_HOSPITAL
    emergency = _get_emergency_or_404(db, trip.emergency_id)
    emergency.status = EmergencyStatus.ARRIVED
    _log_event(db, trip, "TRIP_ARRIVED", user)
    _audit(db, user, "trip_arrived", "Ambulance arrived at hospital", emergency.id, trip_id=trip.id)

    if trip.dest_hospital_id:
        for admin in _hospital_admins(db, trip.dest_hospital_id):
            notification_service.notify(db, admin.id, "AMBULANCE_ARRIVED", "Ambulance arrived", "An ambulance has arrived with your accepted patient.")
    db.commit()
    db.refresh(trip)

    await _broadcast({"type": "TRIP_ARRIVED", "trip_id": trip.id})
    return TripPublic.model_validate(trip)


@router.post("/api/trips/{trip_id}/complete", response_model=TripPublic)
async def complete_trip(
    trip_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    trip = _get_trip_or_404(db, trip_id)
    if user.role != Role.ADMIN and not (user.role == Role.HOSPITAL_ADMIN and user.hospital_id == trip.dest_hospital_id):
        raise HTTPException(status_code=403, detail="only the receiving hospital (or an admin) can confirm transfer completion")
    if trip.status != TripStatus.ARRIVED:
        raise HTTPException(status_code=409, detail=f"trip cannot be completed from status {trip.status}")

    trip.status = TripStatus.COMPLETED
    trip.completed_at = datetime.now(timezone.utc)
    ambulance = db.get(Ambulance, trip.ambulance_id) if trip.ambulance_id else None
    if ambulance is not None:
        ambulance.status = AmbulanceStatus.AVAILABLE
    emergency = _get_emergency_or_404(db, trip.emergency_id)
    emergency.status = EmergencyStatus.COMPLETED
    if trip.dest_hospital_id:
        reservation_service.close_without_releasing(db, emergency.id, trip.dest_hospital_id)
    _log_event(db, trip, "TRIP_COMPLETED", user)
    _audit(db, user, "trip_completed", "Transfer completed", emergency.id, trip_id=trip.id)

    dispatcher = db.get(User, emergency.created_by)
    if dispatcher is not None:
        notification_service.notify(db, dispatcher.id, "TRIP_COMPLETED", "Transfer completed", "The patient transfer has been completed.")
    db.commit()
    db.refresh(trip)

    await _broadcast({"type": "TRIP_COMPLETED", "trip_id": trip.id})
    return TripPublic.model_validate(trip)


@router.post("/api/trips/{trip_id}/reroute", response_model=TripPublic)
async def reroute_trip(
    trip_id: str,
    req: RerouteRequest,
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    trip = _get_trip_or_404(db, trip_id)
    if trip.status not in (TripStatus.DISPATCHED, TripStatus.EN_ROUTE, TripStatus.ARRIVING):
        raise HTTPException(status_code=409, detail=f"trip cannot be rerouted from status {trip.status}")
    if req.new_hospital_id == trip.dest_hospital_id:
        raise HTTPException(status_code=400, detail="new_hospital_id is already the current destination")

    new_hospital = db.get(Hospital, req.new_hospital_id)
    if new_hospital is None or new_hospital.verification_status != HospitalVerificationStatus.VERIFIED or not new_hospital.is_active:
        raise HTTPException(status_code=400, detail="new hospital is not a verified, active facility")

    emergency = _get_emergency_or_404(db, trip.emergency_id)
    old_hospital_id = trip.dest_hospital_id

    # Reserve the new hospital's capacity BEFORE releasing the old one - if
    # this fails, the ambulance keeps its current (still-valid) reservation
    # instead of a window where neither hospital holds one.
    new_reservations = reservation_service.reserve(db, new_hospital.id, emergency.id, requires_icu=_requires_icu(emergency))
    if new_reservations is None:
        raise HTTPException(status_code=409, detail="new hospital does not have the required capacity available")

    if old_hospital_id:
        reservation_service.release(db, emergency.id, old_hospital_id)

    trip.dest_hospital_id = new_hospital.id
    _log_event(db, trip, "TRIP_REROUTED", user, {"from": old_hospital_id, "to": new_hospital.id})
    _audit(db, user, "trip_rerouted", f"Rerouted to '{new_hospital.name}'", emergency.id, trip_id=trip.id, from_hospital_id=old_hospital_id, to_hospital_id=new_hospital.id)

    if old_hospital_id:
        for admin in _hospital_admins(db, old_hospital_id):
            notification_service.notify(db, admin.id, "TRIP_REROUTED", "Patient rerouted", "An inbound patient has been rerouted to a different hospital.")
    for admin in _hospital_admins(db, new_hospital.id):
        notification_service.notify(db, admin.id, "TRIP_REROUTED", "Incoming patient rerouted to you", "A patient previously headed elsewhere has been rerouted to your hospital.")
    ambulance = db.get(Ambulance, trip.ambulance_id) if trip.ambulance_id else None
    if ambulance is not None and ambulance.operator_id:
        notification_service.notify(db, ambulance.operator_id, "TRIP_REROUTED", "Destination changed", f"Your destination has changed to {new_hospital.name}.")

    db.commit()
    db.refresh(trip)

    await _broadcast({"type": "TRIP_REROUTED", "trip_id": trip.id, "from_hospital_id": old_hospital_id, "to_hospital_id": new_hospital.id})
    return TripPublic.model_validate(trip)


@router.post("/api/trips/{trip_id}/cancel", response_model=TripPublic)
async def cancel_trip(
    trip_id: str,
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    trip = _get_trip_or_404(db, trip_id)
    if trip.status in (TripStatus.COMPLETED, TripStatus.CANCELLED, TripStatus.ARRIVED, TripStatus.TRANSFERRED):
        raise HTTPException(status_code=409, detail=f"trip cannot be cancelled from status {trip.status}")

    emergency = _get_emergency_or_404(db, trip.emergency_id)
    if trip.dest_hospital_id:
        reservation_service.release(db, emergency.id, trip.dest_hospital_id)

    trip.status = TripStatus.CANCELLED
    trip.cancelled_at = datetime.now(timezone.utc)
    ambulance = db.get(Ambulance, trip.ambulance_id) if trip.ambulance_id else None
    if ambulance is not None:
        ambulance.status = AmbulanceStatus.AVAILABLE
    emergency.status = EmergencyStatus.CANCELLED
    _log_event(db, trip, "TRIP_CANCELLED", user)
    _audit(db, user, "trip_cancelled", "Trip cancelled", emergency.id, trip_id=trip.id)
    db.commit()
    db.refresh(trip)

    await _broadcast({"type": "TRIP_CANCELLED", "trip_id": trip.id})
    return TripPublic.model_validate(trip)
