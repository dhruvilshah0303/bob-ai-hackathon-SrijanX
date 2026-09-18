"""
Adapts real DB hospitals (models.Hospital/HospitalResources/
HospitalSpecialist) into the plain-dict shape recommendation_engine.py has
always consumed - that engine's constraint-filtering/weighted-scoring logic
is unchanged (Rule: preserve the existing recommendation engine); only its
data source moves from data/hospitals.json to Postgres.

Only VERIFIED, active hospitals are candidates - an unverified or suspended
hospital must never be recommended to a real dispatcher (Rule: only
verified hospitals appear in production recommendation results).
"""
from sqlalchemy.orm import Session

from models import Hospital, HospitalAvailability, HospitalVerificationStatus

_AVAILABILITY_TO_ACCEPTING = {
    HospitalAvailability.ONLINE: "yes",
    HospitalAvailability.BUSY: "limited",
    HospitalAvailability.FULL: "no",
    HospitalAvailability.OFFLINE: "no",
}


def hospitals_for_recommendation(db: Session) -> dict[str, dict]:
    """Returns {hospital_id: hospital_dict}, hospital_dict shaped exactly
    like the dicts recommendation_engine.recommend() and
    recommendation_engine._check_hard_constraints()/_estimate_hospital_wait()
    already expect (see that module - id/name/lat/lng/ed_bays_*/icu_beds_*/
    ward_beds_*/accepting_status/specialists[].type+on_duty/
    avg_historical_wait_min/last_capacity_update_at)."""
    hospitals = (
        db.query(Hospital)
        .filter(
            Hospital.verification_status == HospitalVerificationStatus.VERIFIED,
            Hospital.is_active.is_(True),
        )
        .all()
    )
    result: dict[str, dict] = {}
    for h in hospitals:
        r = h.resources
        if r is None:
            continue  # data integrity gap (every hospital gets a resources row on creation) - skip, don't crash a recommendation for everyone else
        result[h.id] = {
            "id": h.id,
            "name": h.name,
            "lat": h.lat,
            "lng": h.lng,
            "ed_bays_total": r.ed_bays_total,
            "ed_bays_occupied": r.ed_bays_occupied,
            "icu_beds_total": r.icu_beds_total,
            "icu_beds_free": r.icu_beds_free,
            "ward_beds_total": r.ward_beds_total,
            "ward_beds_free": r.ward_beds_free,
            "accepting_status": _AVAILABILITY_TO_ACCEPTING[h.availability_status],
            "specialists": [{"type": s.type, "on_duty": s.on_duty} for s in h.specialists],
            "avg_historical_wait_min": r.avg_historical_wait_min,
            "last_capacity_update_at": r.updated_at.isoformat(),
        }
    return result
