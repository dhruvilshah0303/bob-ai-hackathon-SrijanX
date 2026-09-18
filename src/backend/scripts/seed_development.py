"""
Development/test fixture seeding for the real DB-backed schema (models.py).

Run with:  cd backend && python -m scripts.seed_development

Every account/hospital/ambulance this creates is clearly named "Test ..."
(Rule: "Development/test seed data is allowed, but it must live in the
database and be clearly marked as development/test data" - never hard-coded
into frontend JS, never presented as a real facility). Idempotent: re-running
this skips any row that already exists (matched by its natural unique key -
email, vehicle_number, or hospital name) instead of erroring or duplicating.

Refuses to run at all against a production database (ENVIRONMENT=production)
- seed data has no business existing in a real deployment's database.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import auth_service  # noqa: E402
from models import (  # noqa: E402
    Ambulance, AmbulanceStatus, Hospital, HospitalAvailability,
    HospitalRequestStatus, HospitalResources, HospitalSpecialist,
    HospitalVerificationStatus, Role, SessionLocal, User, init_models,
)

DEV_PASSWORD = "DevPass123!"  # noqa: S105 - intentionally shared/known, dev-only fixture accounts

# lat/lng reused from data/hospitals.json's real entries purely for
# geographic plausibility on a map - these are otherwise unrelated,
# fictional "Test Hospital" records, not the same rows the (still
# in-memory, not-yet-migrated) demo endpoints in app.py read.
_HOSPITALS = [
    {"name": "Test Hospital A", "lat": 23.0395, "lng": 72.5660, "icu_total": 8, "icu_free": 3,
     "ed_total": 12, "ed_occ": 4, "ward_total": 40, "ward_free": 12, "vent_total": 4, "vent_free": 2,
     "specialists": [("cardiologist", True), ("neurologist", False), ("trauma_surgeon", True)]},
    {"name": "Test Hospital B", "lat": 23.0510, "lng": 72.6050, "icu_total": 15, "icu_free": 5,
     "ed_total": 20, "ed_occ": 9, "ward_total": 70, "ward_free": 25, "vent_total": 6, "vent_free": 3,
     "specialists": [("cardiologist", True), ("neurologist", True), ("trauma_surgeon", True)]},
    {"name": "Test Hospital C", "lat": 23.0225, "lng": 72.5714, "icu_total": 6, "icu_free": 1,
     "ed_total": 10, "ed_occ": 9, "ward_total": 30, "ward_free": 4, "vent_total": 2, "vent_free": 0,
     "specialists": [("cardiologist", False), ("neurologist", False), ("trauma_surgeon", True)]},
]

_AMBULANCES = ["Test AMB-101", "Test AMB-102"]


def _get_or_create_hospital(db, spec: dict) -> Hospital:
    existing = db.query(Hospital).filter(Hospital.name == spec["name"]).first()
    if existing:
        print(f"  = hospital already exists: {spec['name']}")
        return existing

    hospital = Hospital(
        name=spec["name"],
        registration_number=f"DEV-REG-{spec['name'].split()[-1]}",
        phone="000-000-0000",
        email=f"{spec['name'].lower().replace(' ', '.')}@example-dev.test",
        address="Development/test fixture - not a real facility",
        lat=spec["lat"],
        lng=spec["lng"],
        verification_status=HospitalVerificationStatus.VERIFIED,
        availability_status=HospitalAvailability.ONLINE,
        is_active=True,
    )
    db.add(hospital)
    db.flush()  # assigns hospital.id without committing yet

    db.add(HospitalResources(
        hospital_id=hospital.id,
        icu_beds_total=spec["icu_total"], icu_beds_free=spec["icu_free"],
        ward_beds_total=spec["ward_total"], ward_beds_free=spec["ward_free"],
        ed_bays_total=spec["ed_total"], ed_bays_occupied=spec["ed_occ"],
        ventilator_total=spec["vent_total"], ventilator_available=spec["vent_free"],
        avg_historical_wait_min=10.0,
    ))
    for spec_type, on_duty in spec["specialists"]:
        db.add(HospitalSpecialist(hospital_id=hospital.id, type=spec_type, on_duty=on_duty))

    print(f"  + created hospital: {spec['name']}")
    return hospital


def _get_or_create_user(db, *, name, email, role, hospital_id=None) -> User:
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        print(f"  = user already exists: {email} ({role})")
        return existing
    user = User(
        name=name, email=email, phone="000-000-0000",
        password_hash=auth_service.hash_password(DEV_PASSWORD),
        role=role, hospital_id=hospital_id, is_active=True,
    )
    db.add(user)
    db.flush()
    print(f"  + created user: {email} ({role}) password={DEV_PASSWORD}")
    return user


def _get_or_create_ambulance(db, vehicle_number: str, operator_id: str) -> Ambulance:
    existing = db.query(Ambulance).filter(Ambulance.vehicle_number == vehicle_number).first()
    if existing:
        print(f"  = ambulance already exists: {vehicle_number}")
        return existing
    amb = Ambulance(
        vehicle_number=vehicle_number, operator_id=operator_id,
        status=AmbulanceStatus.AVAILABLE, is_online=False,
    )
    db.add(amb)
    print(f"  + created ambulance: {vehicle_number}")
    return amb


def main():
    if config.IS_PRODUCTION:
        print("Refusing to seed development/test data: ENVIRONMENT=production.", file=sys.stderr)
        sys.exit(1)

    init_models()  # test/dev convenience; see its own docstring
    db = SessionLocal()
    try:
        print("Hospitals:")
        hospitals = [_get_or_create_hospital(db, spec) for spec in _HOSPITALS]
        db.flush()

        print("Users:")
        _get_or_create_user(db, name="Test Admin", email="admin@example-dev.test", role=Role.ADMIN)
        _get_or_create_user(db, name="Test Dispatcher", email="dispatcher@example-dev.test", role=Role.DISPATCHER)

        hospital_admins = []
        for hospital in hospitals:
            suffix = hospital.name.split()[-1]  # "A" / "B" / "C"
            admin = _get_or_create_user(
                db, name=f"Test Hospital Admin {suffix}",
                email=f"hospitaladmin.{suffix.lower()}@example-dev.test",
                role=Role.HOSPITAL_ADMIN, hospital_id=hospital.id,
            )
            hospital_admins.append(admin)
        db.flush()

        print("Ambulance operators + ambulances:")
        for i, vehicle_number in enumerate(_AMBULANCES, start=1):
            operator = _get_or_create_user(
                db, name=f"Test Ambulance Operator {i}",
                email=f"operator{i}@example-dev.test", role=Role.AMBULANCE_OPERATOR,
            )
            db.flush()
            _get_or_create_ambulance(db, vehicle_number, operator.id)

        db.commit()
        print(f"\nDone. All seeded accounts use password: {DEV_PASSWORD}")
        print("These are development/test fixtures only - not real hospitals, staff, or vehicles.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
