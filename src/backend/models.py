"""
SQLAlchemy ORM models for the real, persistent SrijanX schema (users,
hospitals, ambulances, emergencies, trips, reservations, notifications,
audit events, ...).

This is deliberately a SEPARATE Base/metadata from db.py's existing
SQLAlchemy Core tables (audit_log, trips) - those two Core tables back the
current in-memory demo's persistence (state.log(), db.upsert_trip()) and
stay untouched until app.py's business endpoints are migrated onto these
ORM models phase by phase (hospitals/emergencies first, then trips). Both
sets of tables live in the same physical database (same `engine`, imported
from db.py) with no name collisions, so nothing here breaks the existing
demo persistence while the migration is in progress.

Field names on Hospital/HospitalResources/HospitalSpecialist intentionally
match what recommendation_engine.py already expects on a hospital dict
(icu_beds_total/icu_beds_free/ed_bays_total/ed_bays_occupied/
ward_beds_total/ward_beds_free, specialists[].type/on_duty) rather than the
master-spec's icu_total/icu_available/ed_capacity/ed_occupied naming, so
that engine's constraint-filtering/scoring logic (already correct, already
tested) doesn't need to change - only its data source does. See
db_serializers.py for the ORM -> plain-dict adapter recommend() consumes.

Every primary key is a full UUID4 string (not the 8-char truncated ids the
in-memory demo used for trip/audit ids) - collision-safe across restarts
and real concurrent multi-user load, which the demo's short ids were never
meant to survive.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from db import engine

Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_models():
    """Test/dev-only convenience: create every table in Base.metadata if it
    doesn't already exist (checkfirst=True, the create_all default - a
    no-op against a DB Alembic already migrated). A real deployment must
    still go through `alembic upgrade head` (see alembic/), never this
    function - it exists so the test suite (tests/conftest.py) doesn't need
    a real Alembic run just to get a schema to test against."""
    Base.metadata.create_all(bind=engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Roles (plain string column + Python-side enum, not a DB-native ENUM type -
# keeps sqlite/postgres portable and avoids Alembic enum-migration overhead;
# validity is enforced by Pydantic schemas / service code, not the DB).
# ---------------------------------------------------------------------------
class Role:
    ADMIN = "ADMIN"
    DISPATCHER = "DISPATCHER"
    HOSPITAL_ADMIN = "HOSPITAL_ADMIN"
    AMBULANCE_OPERATOR = "AMBULANCE_OPERATOR"
    ALL = (ADMIN, DISPATCHER, HOSPITAL_ADMIN, AMBULANCE_OPERATOR)


class HospitalVerificationStatus:
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    SUSPENDED = "SUSPENDED"


class HospitalAvailability:
    ONLINE = "ONLINE"
    BUSY = "BUSY"
    FULL = "FULL"
    OFFLINE = "OFFLINE"


class AmbulanceStatus:
    AVAILABLE = "AVAILABLE"
    DISPATCHED = "DISPATCHED"
    EN_ROUTE = "EN_ROUTE"
    ARRIVING = "ARRIVING"
    AT_HOSPITAL = "AT_HOSPITAL"
    OFFLINE = "OFFLINE"


class EmergencyStatus:
    NEW = "NEW"
    ASSESSING = "ASSESSING"
    AWAITING_HOSPITAL = "AWAITING_HOSPITAL"
    HOSPITAL_REQUESTED = "HOSPITAL_REQUESTED"
    HOSPITAL_ACCEPTED = "HOSPITAL_ACCEPTED"
    AMBULANCE_DISPATCHED = "AMBULANCE_DISPATCHED"
    EN_ROUTE = "EN_ROUTE"
    ARRIVING = "ARRIVING"
    ARRIVED = "ARRIVED"
    TRANSFERRED = "TRANSFERRED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TripStatus:
    CREATED = "CREATED"
    DISPATCHED = "DISPATCHED"
    HOSPITAL_REQUESTED = "HOSPITAL_REQUESTED"
    HOSPITAL_ACCEPTED = "HOSPITAL_ACCEPTED"
    EN_ROUTE = "EN_ROUTE"
    ARRIVING = "ARRIVING"
    ARRIVED = "ARRIVED"
    TRANSFERRED = "TRANSFERRED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class HospitalRequestStatus:
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class ReservationStatus:
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    phone = Column(String(32), nullable=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False)
    hospital_id = Column(String(36), ForeignKey("hospitals.id"), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    last_login = Column(DateTime(timezone=True), nullable=True)

    hospital = relationship("Hospital", back_populates="staff")


# ---------------------------------------------------------------------------
# Hospitals
# ---------------------------------------------------------------------------
class Hospital(Base):
    __tablename__ = "hospitals"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(200), nullable=False)
    registration_number = Column(String(100), nullable=True)
    phone = Column(String(32), nullable=True)
    email = Column(String(255), nullable=True)
    address = Column(String(400), nullable=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    verification_status = Column(String(16), nullable=False, default=HospitalVerificationStatus.PENDING)
    availability_status = Column(String(16), nullable=False, default=HospitalAvailability.ONLINE)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    staff = relationship("User", back_populates="hospital")
    resources = relationship("HospitalResources", back_populates="hospital", uselist=False)
    specialists = relationship("HospitalSpecialist", back_populates="hospital")


class HospitalResources(Base):
    """1:1 with Hospital. Field names match recommendation_engine.py's
    expected hospital-dict keys (icu_beds_total/free, ed_bays_total/
    occupied, ward_beds_total/free) - see module docstring."""
    __tablename__ = "hospital_resources"

    hospital_id = Column(String(36), ForeignKey("hospitals.id"), primary_key=True)
    icu_beds_total = Column(Integer, nullable=False, default=0)
    icu_beds_free = Column(Integer, nullable=False, default=0)
    ward_beds_total = Column(Integer, nullable=False, default=0)
    ward_beds_free = Column(Integer, nullable=False, default=0)
    ed_bays_total = Column(Integer, nullable=False, default=0)
    ed_bays_occupied = Column(Integer, nullable=False, default=0)
    ventilator_total = Column(Integer, nullable=False, default=0)
    ventilator_available = Column(Integer, nullable=False, default=0)
    avg_historical_wait_min = Column(Float, nullable=False, default=10.0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    hospital = relationship("Hospital", back_populates="resources")


class HospitalSpecialist(Base):
    __tablename__ = "hospital_specialists"

    id = Column(String(36), primary_key=True, default=_uuid)
    hospital_id = Column(String(36), ForeignKey("hospitals.id"), nullable=False)
    doctor_name = Column(String(200), nullable=True)
    # "type" (not "specialization") matches recommendation_engine.py's
    # s["type"] lookup against severity_rules.json's preferred_specialist.
    type = Column(String(64), nullable=False)
    on_duty = Column(Boolean, nullable=False, default=False)
    on_call = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    hospital = relationship("Hospital", back_populates="specialists")

    __table_args__ = (UniqueConstraint("hospital_id", "type", name="uq_hospital_specialist_type"),)


# ---------------------------------------------------------------------------
# Ambulances (operator = a User with role=AMBULANCE_OPERATOR; no separate
# ambulance_operators table - the spec's "ambulance_operators" entity would
# just duplicate users, since an operator only ever needs the one ambulance
# they're assigned to via Ambulance.operator_id).
# ---------------------------------------------------------------------------
class Ambulance(Base):
    __tablename__ = "ambulances"

    id = Column(String(36), primary_key=True, default=_uuid)
    vehicle_number = Column(String(32), nullable=False, unique=True)
    operator_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    status = Column(String(16), nullable=False, default=AmbulanceStatus.OFFLINE)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    speed = Column(Float, nullable=True)
    heading = Column(Float, nullable=True)
    last_location_update = Column(DateTime(timezone=True), nullable=True)
    is_online = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


# ---------------------------------------------------------------------------
# Patients / emergencies / vitals
# ---------------------------------------------------------------------------
class Patient(Base):
    __tablename__ = "patients"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(200), nullable=True)
    age = Column(Integer, nullable=True)
    gender = Column(String(16), nullable=True)
    phone = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Emergency(Base):
    __tablename__ = "emergencies"

    id = Column(String(36), primary_key=True, default=_uuid)
    patient_id = Column(String(36), ForeignKey("patients.id"), nullable=True)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    emergency_type = Column(String(64), nullable=True)
    # condition_code ties into data/severity_rules.json's condition list -
    # this is what recommendation_engine.py's condition_rule lookup keys on.
    condition_code = Column(String(64), nullable=True)
    severity = Column(String(16), nullable=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    status = Column(String(32), nullable=False, default=EmergencyStatus.NEW)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class EmergencyVitals(Base):
    __tablename__ = "emergency_vitals"

    id = Column(String(36), primary_key=True, default=_uuid)
    emergency_id = Column(String(36), ForeignKey("emergencies.id"), nullable=False)
    heart_rate = Column(Integer, nullable=True)
    spo2 = Column(Integer, nullable=True)
    systolic_bp = Column(Integer, nullable=True)
    diastolic_bp = Column(Integer, nullable=True)
    respiratory_rate = Column(Integer, nullable=True)
    conscious = Column(Boolean, nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------
class Trip(Base):
    __tablename__ = "trips_v2"  # "_v2": db.py already owns a Core table named "trips"

    id = Column(String(36), primary_key=True, default=_uuid)
    emergency_id = Column(String(36), ForeignKey("emergencies.id"), nullable=False)
    ambulance_id = Column(String(36), ForeignKey("ambulances.id"), nullable=True)
    dest_hospital_id = Column(String(36), ForeignKey("hospitals.id"), nullable=True)
    status = Column(String(32), nullable=False, default=TripStatus.CREATED)
    selection_type = Column(String(16), nullable=True)  # "accepted" | "overridden"
    override_reason = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    arrived_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)


class TripLocation(Base):
    """GPS history. Written at a throttled interval by the ambulance
    operator's real browser geolocation stream - NOT every watchPosition
    tick, to avoid flooding Postgres (see realtime.md once written)."""
    __tablename__ = "trip_locations"

    id = Column(String(36), primary_key=True, default=_uuid)
    trip_id = Column(String(36), ForeignKey("trips_v2.id"), nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    speed = Column(Float, nullable=True)
    heading = Column(Float, nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class TripEvent(Base):
    """Patient-journey timeline entries (emergency created, hospital
    accepted, rerouted, arrived, ...) - one row per step, for the
    dispatcher's timeline view."""
    __tablename__ = "trip_events"

    id = Column(String(36), primary_key=True, default=_uuid)
    trip_id = Column(String(36), ForeignKey("trips_v2.id"), nullable=False)
    event_type = Column(String(64), nullable=False)
    actor_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ---------------------------------------------------------------------------
# Hospital acceptance workflow + resource reservations
# ---------------------------------------------------------------------------
class HospitalRequest(Base):
    __tablename__ = "hospital_requests"

    id = Column(String(36), primary_key=True, default=_uuid)
    emergency_id = Column(String(36), ForeignKey("emergencies.id"), nullable=False)
    hospital_id = Column(String(36), ForeignKey("hospitals.id"), nullable=False)
    status = Column(String(16), nullable=False, default=HospitalRequestStatus.PENDING)
    requested_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    responded_at = Column(DateTime(timezone=True), nullable=True)
    response_reason = Column(String(300), nullable=True)


class CapacityReservation(Base):
    """One row per reserved unit of a hospital resource (an ICU bed, an ED
    bay, ...) for a given emergency - see reservation_service.py (Phase 3)
    for the transactional reserve/release logic that prevents two
    emergencies claiming the same last bed."""
    __tablename__ = "capacity_reservations"

    id = Column(String(36), primary_key=True, default=_uuid)
    hospital_id = Column(String(36), ForeignKey("hospitals.id"), nullable=False)
    emergency_id = Column(String(36), ForeignKey("emergencies.id"), nullable=False)
    resource_type = Column(String(32), nullable=False)  # "icu" | "ed" | "ventilator"
    status = Column(String(16), nullable=False, default=ReservationStatus.ACTIVE)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    released_at = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# AI decision audit, notifications, general audit log
# ---------------------------------------------------------------------------
class AiDecision(Base):
    __tablename__ = "ai_decisions"

    id = Column(String(36), primary_key=True, default=_uuid)
    emergency_id = Column(String(36), ForeignKey("emergencies.id"), nullable=False)
    decision_type = Column(String(64), nullable=False)  # "hospital_recommendation" | "triage" | "reroute"
    recommended_hospital_id = Column(String(36), ForeignKey("hospitals.id"), nullable=True)
    reasoning = Column(Text, nullable=True)
    constraints = Column(Text, nullable=True)  # JSON-encoded
    score_breakdown = Column(Text, nullable=True)  # JSON-encoded
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    approved_by = Column(String(36), ForeignKey("users.id"), nullable=True)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    type = Column(String(64), nullable=False)
    title = Column(String(200), nullable=False)
    message = Column(String(500), nullable=False)
    read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AuditEvent(Base):
    """New ORM audit trail for the real (DB-backed) business endpoints as
    they land phase by phase - distinct from db.py's existing Core
    audit_log table, which keeps recording the current in-memory demo
    endpoints until they're migrated. Both are consolidated once that
    migration is complete."""
    __tablename__ = "audit_events"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    emergency_id = Column(String(36), ForeignKey("emergencies.id"), nullable=True)
    event_type = Column(String(64), nullable=False)
    description = Column(String(500), nullable=True)
    event_metadata = Column(Text, nullable=True)  # JSON-encoded
    timestamp = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
