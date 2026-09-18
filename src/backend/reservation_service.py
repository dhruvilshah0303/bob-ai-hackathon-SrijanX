"""
Transactional hospital-resource reservations - the "two ambulances can't
both get told the last ICU bed is free" requirement.

Correctness approach: each resource is claimed with a single conditional
UPDATE (`SET x = x - 1 WHERE x > 0`), not a read-then-write
(`if free > 0: free -= 1`) in Python. The conditional UPDATE is atomic at
the database row level on both backends this app supports - on Postgres it
takes a row lock until commit/rollback, and on SQLite the whole-database
write lock serializes concurrent writers - so two concurrent requests for
the same last bed can never both see `rowcount == 1`; exactly one does, and
loses the free bed's write to a real race is architecturally the same class
of bug this module exists to prevent.

Reservation is all-or-nothing across every resource an emergency's triage
says it needs (currently: an ED bay always, an ICU bed if the triage result
said so) - if any required resource can't be claimed, every claim already
made in this call is rolled back before returning, so a hospital is never
left holding a partial/inconsistent reservation for an emergency it didn't
actually accept.
"""
from typing import List, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from models import CapacityReservation, HospitalResources, ReservationStatus


def reserve(db: Session, hospital_id: str, emergency_id: str, *, requires_icu: bool) -> Optional[List[CapacityReservation]]:
    """Attempts to reserve an ED bay (always) and an ICU bed (if
    requires_icu). Returns the created CapacityReservation rows on success,
    or None if any required resource was unavailable - in which case
    nothing was reserved (any partial claim already made in this call is
    rolled back) and the caller's own transaction is left clean to continue
    using (this function commits on success, rolls back on failure, either
    way the caller doesn't need to)."""
    table = HospitalResources.__table__
    claimed_types: List[str] = []

    ed_result = db.execute(
        update(table)
        .where(table.c.hospital_id == hospital_id, table.c.ed_bays_occupied < table.c.ed_bays_total)
        .values(ed_bays_occupied=table.c.ed_bays_occupied + 1)
    )
    if ed_result.rowcount != 1:
        db.rollback()
        return None
    claimed_types.append("ed")

    if requires_icu:
        icu_result = db.execute(
            update(table)
            .where(table.c.hospital_id == hospital_id, table.c.icu_beds_free > 0)
            .values(icu_beds_free=table.c.icu_beds_free - 1)
        )
        if icu_result.rowcount != 1:
            db.rollback()  # also undoes the ED claim above - all or nothing
            return None
        claimed_types.append("icu")

    reservations = [
        CapacityReservation(hospital_id=hospital_id, emergency_id=emergency_id, resource_type=rtype)
        for rtype in claimed_types
    ]
    db.add_all(reservations)
    db.commit()
    for r in reservations:
        db.refresh(r)
    return reservations


def release(db: Session, emergency_id: str, hospital_id: str) -> int:
    """Releases every still-ACTIVE reservation this emergency holds at this
    hospital (reroute/cancel path - the bed genuinely becomes free again,
    unlike trip completion, which "closes" a reservation without giving the
    bed back since the patient is now actually occupying it - see
    trip_routes.py's complete_trip()). Returns how many were released."""
    table = HospitalResources.__table__
    reservations = (
        db.query(CapacityReservation)
        .filter(
            CapacityReservation.emergency_id == emergency_id,
            CapacityReservation.hospital_id == hospital_id,
            CapacityReservation.status == ReservationStatus.ACTIVE,
        )
        .all()
    )
    for reservation in reservations:
        if reservation.resource_type == "ed":
            db.execute(
                update(table)
                .where(table.c.hospital_id == hospital_id)
                .values(ed_bays_occupied=table.c.ed_bays_occupied - 1)
            )
        elif reservation.resource_type == "icu":
            db.execute(
                update(table)
                .where(table.c.hospital_id == hospital_id)
                .values(icu_beds_free=table.c.icu_beds_free + 1)
        )
        reservation.status = ReservationStatus.RELEASED
    db.commit()
    return len(reservations)


def close_without_releasing(db: Session, emergency_id: str, hospital_id: str) -> int:
    """Trip completed: mark this emergency's reservations at this hospital
    as no longer an active hold, WITHOUT incrementing bed-free counts back
    up - the patient is now actually occupying that bed/bay, so "closing"
    the reservation is bookkeeping (it's no longer a pending hold awaiting
    arrival), not freeing the resource. The hospital's own resource-update
    endpoint (PATCH /api/hospitals/{id}/resources) is how that bed's count
    actually changes going forward, same as any other real admission."""
    reservations = (
        db.query(CapacityReservation)
        .filter(
            CapacityReservation.emergency_id == emergency_id,
            CapacityReservation.hospital_id == hospital_id,
            CapacityReservation.status == ReservationStatus.ACTIVE,
        )
        .all()
    )
    for reservation in reservations:
        reservation.status = ReservationStatus.RELEASED
    db.commit()
    return len(reservations)
