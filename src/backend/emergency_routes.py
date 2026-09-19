"""
Real emergency intake: dispatcher creates an emergency with patient/vitals,
AI-assisted triage runs synchronously against it, and the structured result
(required resources/specializations/priority - never a diagnosis) is stored
as an auditable AiDecision row.

Mounted at /api/emergencies/* in app.py. Creating an emergency here does
NOT itself create a Trip or request a hospital - GET .../recommendation,
POST /api/hospital-requests, and POST /api/trips (all in trip_routes.py)
are separate, dispatcher-initiated steps, matching the real workflow: triage
is decision support offered immediately, but hospital selection and
dispatch are still explicit human actions.

Triage reuses triage_service.classify() (real: IBM watsonx.ai with a
labeled keyword-classifier fallback, see that module's docstring) against
data/severity_rules.json's condition list - condition_code is the join key
between this triage output and the recommendation engine's hard-constraint
filtering (trip_routes.py) and reservation logic (reservation_service.py),
so all three agree on what a given condition requires without duplicating
that table.
"""
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

import severity_config
import triage_service
from auth_service import get_current_user, require_role
from db_session import get_db
from models import (
    AiDecision, Ambulance, AuditEvent, Emergency, EmergencyStatus,
    EmergencyVitals, HospitalRequest, Patient, Role, Trip, User,
)

router = APIRouter(prefix="/api/emergencies", tags=["emergencies"])

# Now a real trip/hospital-request workflow exists (trip_routes.py):
# HOSPITAL_REQUESTED and later statuses have reservations/ambulances/hospital
# notifications attached to them, so cancelling from that point on goes
# through POST /api/trips/{id}/cancel (or hospital-request decline) instead
# of this endpoint, which only ever touched the emergency row itself.
_CANCELLABLE_FROM = {EmergencyStatus.NEW, EmergencyStatus.ASSESSING, EmergencyStatus.AWAITING_HOSPITAL}


class PatientInput(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    age: Optional[int] = Field(default=None, ge=0, le=130)
    gender: Optional[str] = Field(default=None, max_length=16)
    phone: Optional[str] = Field(default=None, max_length=32)


class VitalsInput(BaseModel):
    heart_rate: Optional[int] = Field(default=None, ge=0, le=350)
    spo2: Optional[int] = Field(default=None, ge=0, le=100)
    systolic_bp: Optional[int] = Field(default=None, ge=0, le=350)
    diastolic_bp: Optional[int] = Field(default=None, ge=0, le=250)
    respiratory_rate: Optional[int] = Field(default=None, ge=0, le=100)
    conscious: Optional[bool] = None


class EmergencyCreateRequest(BaseModel):
    patient: Optional[PatientInput] = None
    emergency_type: Optional[str] = Field(default=None, max_length=64)
    # Free-text description fed to AI Triage Assist (e.g. "55yo male,
    # crushing chest pain radiating to left arm") - same role as the
    # existing POST /api/trips/{id}/triage note in app.py, just captured at
    # emergency-creation time instead of as a separate follow-up call.
    symptoms: str = Field(min_length=1, max_length=1000)
    vitals: Optional[VitalsInput] = None
    lat: float
    lng: float

    @field_validator("symptoms")
    @classmethod
    def _clean_symptoms(cls, v):
        cleaned = "".join(ch for ch in v if ch == "\n" or ch == "\t" or not ord(ch) < 0x20).strip()
        if not cleaned:
            raise ValueError("symptoms must not be empty")
        return cleaned


class TriageResultPublic(BaseModel):
    condition_code: str
    condition_label: str
    severity: Optional[str]
    priority: Optional[str]
    confidence: float
    reasoning: str
    source: str  # "watsonx" | "keyword_fallback"
    requires_icu: bool
    requires_ed: bool
    required_specializations: List[str]


class VitalsPublic(BaseModel):
    heart_rate: Optional[int]
    spo2: Optional[int]
    systolic_bp: Optional[int]
    diastolic_bp: Optional[int]
    respiratory_rate: Optional[int]
    conscious: Optional[bool]
    recorded_at: str

    model_config = {"from_attributes": True}


class PatientPublic(BaseModel):
    id: str
    name: Optional[str]
    age: Optional[int]
    gender: Optional[str]
    phone: Optional[str]

    model_config = {"from_attributes": True}


class EmergencyPublic(BaseModel):
    id: str
    patient: Optional[PatientPublic]
    emergency_type: Optional[str]
    condition_code: Optional[str]
    severity: Optional[str]
    lat: float
    lng: float
    status: str
    created_at: str
    updated_at: str
    triage: Optional[TriageResultPublic]
    vitals: List[VitalsPublic]


def _run_triage(db: Session, emergency: Emergency, symptoms: str) -> TriageResultPublic:
    """Decision support only - never treated as a diagnosis (Rule: AI must
    not claim to diagnose patients). Hard resource/specialist requirements
    come straight from severity_rules.json's structured condition table
    (deterministic, auditable), not from anything the model free-generates;
    the model only picks WHICH condition_code applies and explains why -
    see triage_service.classify()'s own docstring for its safety framing."""
    conditions = severity_config.load()["conditions"]
    result = triage_service.classify(symptoms, conditions)
    rule = severity_config.condition_by_code()[result["condition_code"]]
    required = rule.get("required", [])
    specialist = rule.get("preferred_specialist")

    triage = TriageResultPublic(
        condition_code=result["condition_code"],
        condition_label=rule["label"],
        severity=rule.get("severity"),
        priority=rule.get("severity"),  # same axis for now - see docs/ai.md once written
        confidence=result["confidence"],
        reasoning=result["reasoning"],
        source=result["source"],
        requires_icu="icu" in required,
        requires_ed="ed_accepting" in required,
        required_specializations=[specialist] if specialist else [],
    )

    db.add(AiDecision(
        emergency_id=emergency.id,
        decision_type="triage",
        reasoning=triage.reasoning,
        constraints=json.dumps({
            "requires_icu": triage.requires_icu,
            "requires_ed": triage.requires_ed,
            "required_specializations": triage.required_specializations,
        }),
        score_breakdown=json.dumps({"confidence": triage.confidence, "source": triage.source}),
    ))
    return triage


def _serialize(emergency: Emergency, db: Session) -> EmergencyPublic:
    patient = db.get(Patient, emergency.patient_id) if emergency.patient_id else None
    vitals_rows = (
        db.query(EmergencyVitals)
        .filter(EmergencyVitals.emergency_id == emergency.id)
        .order_by(EmergencyVitals.recorded_at.asc())
        .all()
    )
    latest_decision = (
        db.query(AiDecision)
        .filter(AiDecision.emergency_id == emergency.id, AiDecision.decision_type == "triage")
        .order_by(AiDecision.created_at.desc())
        .first()
    )
    triage = None
    if latest_decision is not None and emergency.condition_code:
        rule = severity_config.condition_by_code().get(emergency.condition_code, {})
        constraints = json.loads(latest_decision.constraints or "{}")
        score = json.loads(latest_decision.score_breakdown or "{}")
        triage = TriageResultPublic(
            condition_code=emergency.condition_code,
            condition_label=rule.get("label", emergency.condition_code),
            severity=emergency.severity,
            priority=emergency.severity,
            confidence=score.get("confidence", 0.0),
            reasoning=latest_decision.reasoning or "",
            source=score.get("source", "unknown"),
            requires_icu=constraints.get("requires_icu", False),
            requires_ed=constraints.get("requires_ed", False),
            required_specializations=constraints.get("required_specializations", []),
        )

    return EmergencyPublic(
        id=emergency.id,
        patient=PatientPublic.model_validate(patient) if patient else None,
        emergency_type=emergency.emergency_type,
        condition_code=emergency.condition_code,
        severity=emergency.severity,
        lat=emergency.lat,
        lng=emergency.lng,
        status=emergency.status,
        created_at=emergency.created_at.isoformat(),
        updated_at=emergency.updated_at.isoformat(),
        triage=triage,
        vitals=[
            VitalsPublic(
                heart_rate=v.heart_rate, spo2=v.spo2, systolic_bp=v.systolic_bp,
                diastolic_bp=v.diastolic_bp, respiratory_rate=v.respiratory_rate,
                conscious=v.conscious, recorded_at=v.recorded_at.isoformat(),
            )
            for v in vitals_rows
        ],
    )


def _get_emergency_or_404(db: Session, emergency_id: str) -> Emergency:
    emergency = db.get(Emergency, emergency_id)
    if emergency is None:
        raise HTTPException(status_code=404, detail="emergency not found")
    return emergency


@router.post("", response_model=EmergencyPublic, status_code=status.HTTP_201_CREATED)
def create_emergency(
    req: EmergencyCreateRequest,
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    patient = None
    if req.patient is not None:
        patient = Patient(
            name=req.patient.name, age=req.patient.age,
            gender=req.patient.gender, phone=req.patient.phone,
        )
        db.add(patient)
        db.flush()

    emergency = Emergency(
        patient_id=patient.id if patient else None,
        created_by=user.id,
        emergency_type=req.emergency_type,
        lat=req.lat, lng=req.lng,
        status=EmergencyStatus.NEW,
    )
    db.add(emergency)
    db.flush()

    if req.vitals is not None:
        db.add(EmergencyVitals(emergency_id=emergency.id, **req.vitals.model_dump()))

    triage = _run_triage(db, emergency, req.symptoms)
    emergency.condition_code = triage.condition_code
    emergency.severity = triage.severity
    emergency.status = EmergencyStatus.AWAITING_HOSPITAL

    db.add(AuditEvent(
        user_id=user.id, emergency_id=emergency.id, event_type="emergency_created",
        description=f"Emergency created ({triage.condition_label})",
        event_metadata=json.dumps({"condition_code": triage.condition_code, "source": triage.source}),
    ))
    db.commit()
    db.refresh(emergency)
    return _serialize(emergency, db)


@router.get("", response_model=List[EmergencyPublic])
def list_emergencies(
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    emergencies = db.query(Emergency).order_by(Emergency.created_at.desc()).all()
    return [_serialize(e, db) for e in emergencies]


def _can_view_emergency(user: User, emergency_id: str, db: Session) -> bool:
    """Dispatcher/admin see everything. A HOSPITAL_ADMIN or
    AMBULANCE_OPERATOR only sees an emergency once they have a real,
    DB-recorded link to it - a hospital request/trip destined for their own
    hospital, or a trip assigned to their own ambulance (Rule: a hospital
    only sees patient information necessary for an emergency routed to it;
    an ambulance crew only sees the patient they're actually transporting).
    Avoids importing trip_routes.py (which itself imports this module's
    sibling severity_config, not this file) - queries the same tables
    directly instead of introducing a cross-import."""
    if user.role in (Role.DISPATCHER, Role.ADMIN):
        return True
    if user.role == Role.HOSPITAL_ADMIN and user.hospital_id:
        linked = (
            db.query(HospitalRequest)
            .filter(HospitalRequest.emergency_id == emergency_id, HospitalRequest.hospital_id == user.hospital_id)
            .first()
            or db.query(Trip)
            .filter(Trip.emergency_id == emergency_id, Trip.dest_hospital_id == user.hospital_id)
            .first()
        )
        return linked is not None
    if user.role == Role.AMBULANCE_OPERATOR:
        linked = (
            db.query(Trip)
            .join(Ambulance, Trip.ambulance_id == Ambulance.id)
            .filter(Trip.emergency_id == emergency_id, Ambulance.operator_id == user.id)
            .first()
        )
        return linked is not None
    return False


@router.get("/{emergency_id}", response_model=EmergencyPublic)
def get_emergency(
    emergency_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    emergency = _get_emergency_or_404(db, emergency_id)
    if not _can_view_emergency(user, emergency_id, db):
        raise HTTPException(status_code=403, detail="not authorized for this emergency")
    return _serialize(emergency, db)


@router.post("/{emergency_id}/vitals", response_model=EmergencyPublic)
def add_vitals(
    emergency_id: str,
    req: VitalsInput,
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    emergency = _get_emergency_or_404(db, emergency_id)
    if emergency.status in (EmergencyStatus.COMPLETED, EmergencyStatus.CANCELLED):
        raise HTTPException(status_code=409, detail=f"cannot add vitals to a {emergency.status.lower()} emergency")

    db.add(EmergencyVitals(emergency_id=emergency.id, **req.model_dump()))
    db.add(AuditEvent(
        user_id=user.id, emergency_id=emergency.id, event_type="vitals_recorded",
        description="New vitals reading recorded", event_metadata=json.dumps(req.model_dump()),
    ))
    db.commit()
    db.refresh(emergency)
    return _serialize(emergency, db)


@router.post("/{emergency_id}/cancel", response_model=EmergencyPublic)
def cancel_emergency(
    emergency_id: str,
    user: User = Depends(require_role(Role.DISPATCHER, Role.ADMIN)),
    db: Session = Depends(get_db),
):
    emergency = _get_emergency_or_404(db, emergency_id)
    if emergency.status not in _CANCELLABLE_FROM:
        raise HTTPException(
            status_code=409,
            detail=f"cannot cancel an emergency in status {emergency.status} through this endpoint",
        )
    emergency.status = EmergencyStatus.CANCELLED
    db.add(AuditEvent(
        user_id=user.id, emergency_id=emergency.id, event_type="emergency_cancelled",
        description="Emergency cancelled by dispatcher",
    ))
    db.commit()
    db.refresh(emergency)
    return _serialize(emergency, db)
