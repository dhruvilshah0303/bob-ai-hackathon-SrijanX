"""
GET /api/analytics - real metrics computed from the actual DB tables
(HospitalRequest, Trip, CapacityReservation, AuditEvent), not fabricated
numbers. Every count here is a live SQL query against real records; there
is no separate "analytics" store to fall out of sync with reality.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth_service import require_role
from db_session import get_db
from models import (
    HospitalRequest, HospitalRequestStatus, Role, Trip, TripEvent, TripStatus, User,
)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class AnalyticsResponse(BaseModel):
    generated_at: str
    total_emergencies_with_hospital_request: int
    total_trips: int
    active_trips: int
    completed_trips: int
    cancelled_trips: int
    hospital_requests_total: int
    hospital_requests_accepted: int
    hospital_requests_declined: int
    hospital_acceptance_rate: float
    reroute_count: int
    average_dispatch_to_arrival_minutes: float | None


def _minutes_between(a: datetime, b: datetime) -> float:
    a = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
    b = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
    return (b - a).total_seconds() / 60.0


@router.get("", response_model=AnalyticsResponse)
def get_analytics(
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    requests = db.query(HospitalRequest).all()
    accepted = [r for r in requests if r.status == HospitalRequestStatus.ACCEPTED]
    declined = [r for r in requests if r.status == HospitalRequestStatus.DECLINED]
    responded = accepted + declined

    trips = db.query(Trip).all()
    active = [t for t in trips if t.status not in (TripStatus.COMPLETED, TripStatus.CANCELLED)]
    completed = [t for t in trips if t.status == TripStatus.COMPLETED]
    cancelled = [t for t in trips if t.status == TripStatus.CANCELLED]

    dispatch_to_arrival = [
        _minutes_between(t.started_at, t.arrived_at)
        for t in trips
        if t.started_at is not None and t.arrived_at is not None
    ]
    avg_dispatch_to_arrival = round(sum(dispatch_to_arrival) / len(dispatch_to_arrival), 1) if dispatch_to_arrival else None

    # A trip has been rerouted if its TripEvent log contains a TRIP_REROUTED
    # entry - counted via the events table directly rather than a trip-level
    # flag, since a trip can be rerouted more than once.
    reroute_count = db.query(TripEvent).filter(TripEvent.event_type == "TRIP_REROUTED").count()

    distinct_emergency_ids = {r.emergency_id for r in requests}

    return AnalyticsResponse(
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_emergencies_with_hospital_request=len(distinct_emergency_ids),
        total_trips=len(trips),
        active_trips=len(active),
        completed_trips=len(completed),
        cancelled_trips=len(cancelled),
        hospital_requests_total=len(requests),
        hospital_requests_accepted=len(accepted),
        hospital_requests_declined=len(declined),
        hospital_acceptance_rate=round(len(accepted) / len(responded), 3) if responded else 0.0,
        reroute_count=reroute_count,
        average_dispatch_to_arrival_minutes=avg_dispatch_to_arrival,
    )
