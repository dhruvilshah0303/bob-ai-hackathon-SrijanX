"""
AI Ambulance-to-Hospital Coordinator - Backend
===============================================================
FastAPI backend implementing the TRD architecture:
  - Hospital Capacity Service (persisted in the database, mutated by a
    logged-in hospital user through the API - a real hospital's own ED
    dashboard would call the same endpoint)
  - Real per-user login (username/password -> session token - see auth.py)
    with role-based access control (admin / dispatcher / hospital)
  - Severity Intake -> required-capability mapping
  - ETA / Routing Service (real API with graceful fallback - eta_service.py)
  - AI Recommendation Engine (recommendation_engine.py)
  - Rerouting / Alerting Engine (per-trip background loop + websocket push)
  - Pre-Alert service, with a real hospital-side accept/reject response
  - Audit log + trip/hospital state, persisted via db.py so a restart
    doesn't erase in-progress emergencies or live hospital capacity

Multi-ambulance support: the system tracks any number of concurrent trips
(state.trips, keyed by trip_id), because hospital capacity is a single
shared resource multiple ambulances compete for. All hospital-capacity
mutations and trip-state mutations go through STATE_LOCK - a single
coarse-grained lock. That's a deliberate simplification: at real pilot
request volume, a single lock costs nothing in practice and makes
correctness easy to reason about; splitting it into finer-grained
per-hospital/per-trip locks is a reasonable follow-up once real concurrent
load makes the coarse lock measurably slow, not before.

Ambulance position: there is no physical vehicle GPS feed behind this
prototype, so position comes from one of two honest sources, both labeled
in the trip's `position_source` field so the frontend never presents an
estimate as if it were a live feed:
  - "device_gps": a real position reported by the ambulance crew's own
    device via POST .../position (the frontend uses the browser
    Geolocation API for this when permission is granted).
  - "estimated": interpolated along the real routed road geometry
    (routing_service.py) at the real elapsed wall-clock time implied by the
    real traffic-aware ETA (eta_service.py) - i.e. a real ETA of 8 minutes
    takes 8 real minutes to reach 100%, not an accelerated demo countdown.

Run with:  uvicorn app:app --reload --port 8000
Then open http://localhost:8000 in a browser.
"""
import asyncio
import logging
import os
import random
import string
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import config
config.configure_logging()
logger = logging.getLogger("app")

import auth
import db
import eta_service
import ratelimit
import routing_service
import triage_service
from recommendation_engine import recommend
from utils import interpolate_along_path, load_json

if config.SENTRY_DSN:
    import sentry_init  # noqa: F401 - side-effect import, initializes Sentry if configured

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR.parent / "frontend"

TICK_SECONDS = 0.5
REROUTE_CHECK_EVERY_SEC = 5.0  # throttled - each check calls the live traffic/ETA API for every hospital
DEVICE_GPS_FRESHNESS_SEC = 20.0  # how long a reported device position "wins" over the interpolated estimate


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Resume movement loops for any trip that was still en route the last
    # time the process persisted it, so a server restart doesn't strand an
    # in-progress emergency with a dead background loop. `state` and
    # `_run_movement_loop` are module-level names defined further down this
    # file - safe to reference here because this function only runs once
    # the whole module (and therefore those names) has finished loading.
    for trip in list(state.trips.values()):
        if trip.get("status") == "en_route" and trip.get("movement_started_at"):
            asyncio.create_task(_run_movement_loop(trip["id"], trip.get("movement_run_id", 0)))
            logger.info("Resumed movement loop for trip %s after restart", trip["id"])
    yield


app = FastAPI(title="AI Ambulance-to-Hospital Coordinator", lifespan=_lifespan)

config.startup_warnings(logger)
logger.info(
    "Starting up: environment=%s db=%s cors_origins=%s",
    config.ENVIRONMENT,
    "sqlite" if db.IS_SQLITE else "postgres",
    config.ALLOWED_ORIGINS,
)

# CORS origins come from config (ALLOWED_ORIGINS env var). Defaults to "*"
# because that's correct for local dev (frontend and backend share an
# origin); config.startup_warnings() already yells if that default is still
# in effect with ENVIRONMENT=production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(auth.AccessTokenMiddleware)
app.add_middleware(ratelimit.RateLimitMiddleware)


# ---------------------------------------------------------------------------
# In-memory state (rehydrated from the database at startup - see State.reset)
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now().astimezone().isoformat()


def _new_ambulance_label():
    suffix = "".join(random.choices(string.digits, k=2))
    return f"AMB-{suffix}"


class State:
    def __init__(self):
        self.hospitals: dict = {}
        self.severity_rules: dict = {}
        self.condition_by_code: dict = {}
        self.trips: dict = {}
        self.audit_log: list = []
        self.reset(wipe_history=False)

    def reset(self, wipe_history: bool = True):
        if wipe_history:
            db.reset_operational_state()

        self.severity_rules = load_json(DATA_DIR / "severity_rules.json")
        self.condition_by_code = {c["code"]: c for c in self.severity_rules["conditions"]}

        # Hospitals: the database is the source of truth for live capacity.
        # data/hospitals.json is only the one-time SEED used the very first
        # time this app runs against an empty database (or right after a
        # wipe) - after that, every capacity change is persisted and
        # reloaded from the database, so a restart no longer silently
        # reverts hospitals back to their seed state.
        persisted = db.load_hospitals()
        if persisted:
            self.hospitals = {h["id"]: h for h in persisted}
        else:
            seed = load_json(DATA_DIR / "hospitals.json")
            for h in seed:
                h["last_capacity_update_at"] = _now_iso()
            self.hospitals = {h["id"]: h for h in seed}
            for h in self.hospitals.values():
                db.upsert_hospital(h)

        # Trips: reload whatever was still active (not arrived/cancelled)
        # the last time it was persisted, so an in-progress emergency
        # survives a restart instead of disappearing. app.py's startup
        # event (see _resume_active_trips below) restarts each en-route
        # trip's movement loop after this.
        if wipe_history:
            self.trips = {}
        else:
            self.trips = {t["id"]: t for t in db.load_active_trips()}

        if wipe_history:
            self.audit_log = []
        else:
            # Startup path: recover audit history across a restart instead of
            # silently losing it.
            self.audit_log = db.load_audit_log()

    def log(self, event_type, payload):
        event = {
            "id": str(uuid.uuid4())[:8],
            "event_type": event_type,
            "payload": payload,
            "created_at": _now_iso(),
        }
        self.audit_log.append(event)
        db.insert_audit_event(event)


db.init_db()
state = State()
STATE_LOCK = asyncio.Lock()

# One-time bootstrap: if no user accounts exist at all (fresh database),
# create the admin account from ADMIN_USERNAME/ADMIN_PASSWORD so there's
# always a way to log in without touching the database by hand.
if db.count_users() == 0:
    auth.create_user(config.ADMIN_USERNAME, config.ADMIN_PASSWORD, role="admin")
    logger.warning(
        "No user accounts existed - created an initial admin account (username=%s). "
        "Log in and create real dispatcher/hospital accounts via POST /api/auth/users.",
        config.ADMIN_USERNAME,
    )


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


def _public_trip_list():
    """Lightweight summary of all active trips, for the fleet/map view and
    the hospital pre-alert dashboard (which needs to see alerts from every
    ambulance, not just "your own")."""
    return [
        {
            "id": t["id"],
            "ambulance_label": t["ambulance_label"],
            "status": t["status"],
            "ambulance_pos": t["ambulance_pos"],
            "position_source": t.get("position_source", "estimated"),
            "dest_hospital_id": t["dest_hospital_id"],
            "condition_code": t["condition_code"],
            "prealert": t.get("prealert"),
        }
        for t in state.trips.values()
    ]


async def _broadcast_trip(trip):
    await manager.broadcast({"type": "trip_updated", "trip_id": trip["id"], "trip": trip})
    await manager.broadcast({"type": "trips_list_updated", "trips": _public_trip_list()})


async def _broadcast_hospitals():
    await manager.broadcast({"type": "hospitals_updated", "hospitals": list(state.hospitals.values())})


def _persist_trip(trip: dict):
    db.upsert_trip(trip)


# ---------------------------------------------------------------------------
# Reservation helpers (holds a bed the moment a hospital is selected, not
# only when the ambulance eventually arrives)
# ---------------------------------------------------------------------------

def _reserve_hospital(hospital_id: str, condition_code: str):
    h = state.hospitals.get(hospital_id)
    if not h:
        return
    condition_rule = state.condition_by_code.get(condition_code, {})
    if "icu" in condition_rule.get("required", []):
        h["icu_beds_free"] = max(0, h["icu_beds_free"] - 1)
    h["ed_bays_occupied"] = min(h["ed_bays_total"], h["ed_bays_occupied"] + 1)
    h["last_capacity_update_at"] = _now_iso()
    db.upsert_hospital(h)


def _release_hospital(hospital_id: str, condition_code: str):
    h = state.hospitals.get(hospital_id)
    if not h:
        return
    condition_rule = state.condition_by_code.get(condition_code, {})
    if "icu" in condition_rule.get("required", []):
        h["icu_beds_free"] = min(h["icu_beds_total"], h["icu_beds_free"] + 1)
    h["ed_bays_occupied"] = max(0, h["ed_bays_occupied"] - 1)
    h["last_capacity_update_at"] = _now_iso()
    db.upsert_hospital(h)


def _start_movement_leg(trip: dict, dest_hospital_id: str, eta_min: float) -> int:
    """Kicks off (or restarts, on reroute) the ambulance's movement toward
    dest_hospital_id: fetches a real road route (falling back to a straight
    line if no live routing provider is available - see routing_service.py)
    so the movement loop can follow actual roads instead of cutting a
    diagonal line across the map, and sets up the timing the movement loop
    reads. The leg now takes exactly the real ETA in real wall-clock
    seconds - no artificial time acceleration. Returns the new
    movement_run_id so the caller can pass it to _run_movement_loop (see
    that function's docstring for why this matters).

    Called with STATE_LOCK already held, same as the ETA lookups in
    compute_recommendation() - a known simplification (a blocking network
    call inside the lock), not an oversight.
    """
    dest = state.hospitals[dest_hospital_id]
    origin = trip["ambulance_pos"]
    route = routing_service.get_route(origin["lat"], origin["lng"], dest["lat"], dest["lng"], dest_hospital_id)
    trip["route_points"] = route["points"]
    trip["route_source"] = route["source"]
    trip["origin_pos"] = dict(origin)
    trip["movement_started_at"] = time.time()
    trip["movement_duration_sec"] = max(4.0, eta_min * 60)
    trip["movement_run_id"] = trip.get("movement_run_id", 0) + 1
    return trip["movement_run_id"]


# ---------------------------------------------------------------------------
# Core recommendation helper
# ---------------------------------------------------------------------------

def _resort_candidates(snapshot: dict) -> dict:
    """Re-applies the same viable-first/best-score-first, then
    non-viable-by-distance ordering recommend() uses, and recomputes
    recommended_hospital_id - needed after _apply_exclusions mutates some
    candidates' viability."""
    candidates = snapshot["candidates"]
    viable_sorted = sorted([c for c in candidates if c["viable"]], key=lambda c: -c["score"])
    non_viable_sorted = sorted(
        [c for c in candidates if not c["viable"]],
        key=lambda c: (c["distance_km"] if c["distance_km"] is not None else 9999),
    )
    snapshot["candidates"] = viable_sorted + non_viable_sorted
    snapshot["recommended_hospital_id"] = viable_sorted[0]["hospital_id"] if viable_sorted else None
    return snapshot


def _apply_exclusions(snapshot: dict, exclude_ids: set[str]) -> dict:
    """A hospital that explicitly rejected this patient's pre-alert must not
    be recommended again for the same trip - mark it non-viable with an
    honest reason and re-rank."""
    if not exclude_ids:
        return snapshot
    changed = False
    for c in snapshot["candidates"]:
        if c["hospital_id"] in exclude_ids and c["viable"]:
            c["viable"] = False
            c["disqualifiers"] = list(c.get("disqualifiers") or []) + ["Hospital declined this transfer"]
            c["reasons"] = list(c["disqualifiers"])
            c["score"] = 0.0
            changed = True
    if changed:
        snapshot = _resort_candidates(snapshot)
    return snapshot


def compute_recommendation(origin_lat, origin_lng, condition_code: str, trip_id: str, exclude_ids: Optional[set] = None) -> dict:
    condition_rule = state.condition_by_code[condition_code]
    eta_lookup = {}
    for hid, h in state.hospitals.items():
        eta = eta_service.get_eta(origin_lat, origin_lng, h["lat"], h["lng"], hid, seed_key=trip_id)
        eta_lookup[hid] = eta
    weights = state.severity_rules["scoring_weights"]
    stale_threshold = state.severity_rules["stale_data_threshold_minutes"]
    snapshot = recommend(list(state.hospitals.values()), eta_lookup, condition_rule, weights, stale_threshold)
    snapshot = _apply_exclusions(snapshot, exclude_ids or set())
    snapshot["generated_at"] = _now_iso()
    snapshot["condition_code"] = condition_code
    snapshot["condition_label"] = condition_rule["label"]
    return snapshot


def _new_trip(ambulance_label: Optional[str], incident_location: dict, created_by: str,
              patient_age: Optional[int], patient_sex: Optional[str], patient_notes: Optional[str]) -> dict:
    incident = dict(incident_location)
    trip = {
        "id": str(uuid.uuid4())[:8],
        "ambulance_label": ambulance_label or _new_ambulance_label(),
        "created_by": created_by,
        "status": "awaiting_assessment",
        "condition_code": None,
        "ambulance_pos": dict(incident),
        "origin_pos": dict(incident),
        "incident_location": dict(incident),
        "patient_age": patient_age,
        "patient_sex": patient_sex,
        "patient_notes": patient_notes,
        "dest_hospital_id": None,
        "selection_type": None,
        "override_reason": None,
        "route_points": None,
        "route_source": None,
        "recommendation": None,
        "last_triage_suggestion": None,
        "last_triage_note": None,
        "prealert": None,
        "rejected_hospital_ids": [],
        "reroute_alert": None,
        "position_source": "estimated",
        "last_device_update_at": None,
        "movement_started_at": None,
        "movement_duration_sec": None,
        "movement_run_id": 0,
        "created_at": _now_iso(),
        "finished_at": None,
    }
    state.trips[trip["id"]] = trip
    _persist_trip(trip)
    return trip


def _get_trip_or_404(trip_id: str) -> dict:
    trip = state.trips.get(trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="trip not found (it may have arrived/been reset)")
    return trip


def _require_hospital_scope(user: dict, hospital_id: str):
    """A "hospital" role user may only act on their own hospital's data -
    admin (and dispatcher, where applicable) are unrestricted."""
    if user["role"] == "hospital" and user.get("hospital_id") != hospital_id:
        raise HTTPException(status_code=403, detail="not authorized for this hospital")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

def _strip_control_chars(value: Optional[str]) -> Optional[str]:
    """Defense-in-depth alongside the frontend's HTML-escaping (see
    escapeHtml() in app.js): free-text fields get control characters
    stripped and whitespace trimmed server-side too, for any caller that
    isn't going through that frontend at all (curl, another client)."""
    if value is None:
        return None
    cleaned = "".join(ch for ch in value if ch == "\n" or ch == "\t" or not ord(ch) < 0x20)
    return cleaned.strip() or None


class IncidentLocation(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    label: Optional[str] = Field(default=None, max_length=200)

    @field_validator("label")
    @classmethod
    def _clean_label(cls, v):
        return _strip_control_chars(v)


class NewTripRequest(BaseModel):
    ambulance_label: Optional[str] = Field(default=None, max_length=64)
    incident_location: IncidentLocation
    patient_age: Optional[int] = Field(default=None, ge=0, le=130)
    patient_sex: Optional[str] = Field(default=None, max_length=20)
    patient_notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("ambulance_label")
    @classmethod
    def _clean_ambulance_label(cls, v):
        return _strip_control_chars(v)

    @field_validator("patient_sex", "patient_notes")
    @classmethod
    def _clean_free_text(cls, v):
        return _strip_control_chars(v)


class CaseRequest(BaseModel):
    condition_code: str


class TriageRequest(BaseModel):
    """A dispatcher's free-text note, e.g. '55yo male, crushing chest pain,
    sweating, radiating to left arm' - classified into a suggested
    condition_code by triage_service.classify() (IBM watsonx.ai Granite,
    with a keyword-classifier fallback). This never sets the trip's
    condition directly; the dispatcher still confirms via POST .../case."""
    note: str = Field(min_length=1, max_length=1000)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, v):
        cleaned = _strip_control_chars(v)
        if not cleaned:
            raise ValueError("note must not be empty")
        return cleaned


class SelectRequest(BaseModel):
    hospital_id: str
    override_reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("override_reason")
    @classmethod
    def _clean_override_reason(cls, v):
        return _strip_control_chars(v)


class CapacityUpdate(BaseModel):
    icu_beds_free: Optional[int] = None
    ed_bays_occupied: Optional[int] = None
    accepting_status: Optional[str] = None
    specialist_type: Optional[str] = None
    specialist_on_duty: Optional[bool] = None


class RerouteResponse(BaseModel):
    accept: bool


class PrealertRespondRequest(BaseModel):
    accept: bool
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v):
        return _strip_control_chars(v)


class PositionReport(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: Optional[float] = Field(default=None, ge=0)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    role: str
    hospital_id: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v):
        if v not in ("admin", "dispatcher", "hospital"):
            raise ValueError("role must be one of: admin, dispatcher, hospital")
        return v


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.post("/api/auth/login")
def login(req: LoginRequest):
    user = auth.authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid username or password")
    session = auth.issue_session(user)
    return {
        "token": session["token"],
        "username": user["username"],
        "role": user["role"],
        "hospital_id": user.get("hospital_id"),
        "expires_at": session["expires_at"],
    }


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.headers.get("x-api-key")
    if token:
        db.delete_session(token)
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


@app.post("/api/auth/users")
def create_user(req: CreateUserRequest, request: Request):
    auth.require_role(request, "admin")
    if db.get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail="username already exists")
    if req.role == "hospital":
        if not req.hospital_id or req.hospital_id not in state.hospitals:
            raise HTTPException(status_code=400, detail="hospital role requires a valid hospital_id")
    user = auth.create_user(req.username, req.password, req.role, req.hospital_id)
    return {"username": user["username"], "role": user["role"], "hospital_id": user.get("hospital_id")}


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "hospitals_loaded": len(state.hospitals),
        "active_trips": len(state.trips),
        "eta_provider": eta_service.provider_status(),
        "routing_provider": routing_service.provider_status(),
        "triage_provider": triage_service.provider_status(),
        "login_required": config.REQUIRE_LOGIN,
    }


@app.get("/api/conditions")
def get_conditions():
    return state.severity_rules["conditions"]


@app.get("/api/hospitals")
def get_hospitals():
    return list(state.hospitals.values())


@app.post("/api/hospitals/{hospital_id}/capacity")
async def update_capacity(hospital_id: str, update: CapacityUpdate, request: Request):
    user = auth.require_role(request, "hospital")
    _require_hospital_scope(user, hospital_id)

    async with STATE_LOCK:
        h = state.hospitals.get(hospital_id)
        if not h:
            raise HTTPException(status_code=404, detail="hospital not found")

        changed = {}
        if update.icu_beds_free is not None:
            h["icu_beds_free"] = max(0, min(update.icu_beds_free, h["icu_beds_total"]))
            changed["icu_beds_free"] = h["icu_beds_free"]
        if update.ed_bays_occupied is not None:
            h["ed_bays_occupied"] = max(0, min(update.ed_bays_occupied, h["ed_bays_total"]))
            changed["ed_bays_occupied"] = h["ed_bays_occupied"]
        if update.accepting_status is not None:
            if update.accepting_status not in ("yes", "limited", "no"):
                raise HTTPException(status_code=400, detail="accepting_status must be yes, limited, or no")
            h["accepting_status"] = update.accepting_status
            changed["accepting_status"] = h["accepting_status"]
        if update.specialist_type is not None and update.specialist_on_duty is not None:
            for s in h["specialists"]:
                if s["type"] == update.specialist_type:
                    s["on_duty"] = update.specialist_on_duty
                    changed[f"specialist:{update.specialist_type}"] = update.specialist_on_duty
        h["last_capacity_update_at"] = _now_iso()
        db.upsert_hospital(h)

        state.log("capacity_update", {"hospital_id": hospital_id, "changed": changed, "by": user["username"]})
        en_route_ids = [t["id"] for t in state.trips.values() if t["status"] == "en_route"]

    await _broadcast_hospitals()
    # Capacity is a shared resource - ANY en-route trip might now need a
    # reroute, not just one.
    for tid in en_route_ids:
        await _check_for_reroute(tid)

    return {"ok": True, "hospital": h}


@app.post("/api/trips")
async def create_trip(req: NewTripRequest, request: Request):
    user = auth.require_role(request, "dispatcher")
    async with STATE_LOCK:
        trip = _new_trip(
            req.ambulance_label,
            req.incident_location.model_dump(),
            user["username"],
            req.patient_age,
            req.patient_sex,
            req.patient_notes,
        )
        state.log("trip_created", {"trip_id": trip["id"], "ambulance_label": trip["ambulance_label"], "by": user["username"]})
    await manager.broadcast({"type": "trips_list_updated", "trips": _public_trip_list()})
    return trip


@app.get("/api/trips")
def list_trips():
    return list(state.trips.values())


@app.get("/api/trips/{trip_id}")
def get_trip(trip_id: str):
    return _get_trip_or_404(trip_id)


@app.delete("/api/trips/{trip_id}")
async def discard_trip(trip_id: str, request: Request):
    auth.require_role(request, "dispatcher")
    async with STATE_LOCK:
        trip = state.trips.get(trip_id)
        if not trip:
            raise HTTPException(status_code=404, detail="trip not found")
        if trip["dest_hospital_id"] and trip["status"] not in ("arrived",):
            _release_hospital(trip["dest_hospital_id"], trip["condition_code"])
        trip["status"] = "cancelled"
        trip["finished_at"] = _now_iso()
        _persist_trip(trip)
        del state.trips[trip_id]
        state.log("trip_cancelled", {"trip_id": trip_id})

    await _broadcast_hospitals()
    await manager.broadcast({"type": "trip_removed", "trip_id": trip_id})
    await manager.broadcast({"type": "trips_list_updated", "trips": _public_trip_list()})
    return {"ok": True}


@app.post("/api/trips/{trip_id}/triage")
async def suggest_triage(trip_id: str, req: TriageRequest, request: Request):
    """AI Triage Assist: classifies req.note into a suggested condition_code
    (does not itself change the trip's condition - see TriageRequest)."""
    auth.require_role(request, "dispatcher")
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        conditions = state.severity_rules["conditions"]
        suggestion = triage_service.classify(req.note, conditions)
        trip["last_triage_suggestion"] = suggestion
        trip["last_triage_note"] = req.note
        _persist_trip(trip)

        state.log("triage_suggested", {
            "trip_id": trip_id,
            "note": req.note,
            "suggested_condition_code": suggestion["condition_code"],
            "confidence": suggestion["confidence"],
            "source": suggestion["source"],
        })

    return suggestion


@app.post("/api/trips/{trip_id}/case")
async def create_case(trip_id: str, req: CaseRequest, request: Request):
    auth.require_role(request, "dispatcher")
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        if req.condition_code not in state.condition_by_code:
            raise HTTPException(status_code=400, detail="unknown condition_code")

        trip["condition_code"] = req.condition_code
        origin = trip["ambulance_pos"]
        snapshot = compute_recommendation(
            origin["lat"], origin["lng"], req.condition_code, trip_id,
            exclude_ids=set(trip.get("rejected_hospital_ids") or []),
        )
        trip["recommendation"] = snapshot
        trip["status"] = "recommendation_ready"
        _persist_trip(trip)

        state.log("recommendation_generated", {
            "trip_id": trip_id,
            "condition_code": req.condition_code,
            "recommended_hospital_id": snapshot["recommended_hospital_id"],
        })

    await _broadcast_trip(trip)
    return trip


@app.post("/api/trips/{trip_id}/select")
async def select_hospital(trip_id: str, req: SelectRequest, request: Request):
    auth.require_role(request, "dispatcher")
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        snapshot = trip["recommendation"]
        if not snapshot:
            raise HTTPException(status_code=400, detail="no recommendation available yet")
        if req.hospital_id not in state.hospitals:
            raise HTTPException(status_code=404, detail="hospital not found")

        is_override = req.hospital_id != snapshot["recommended_hospital_id"]
        trip["dest_hospital_id"] = req.hospital_id
        _reserve_hospital(req.hospital_id, trip["condition_code"])
        trip["selection_type"] = "overridden" if is_override else "accepted"
        trip["override_reason"] = req.override_reason if is_override else None
        trip["status"] = "en_route"

        candidate = next((c for c in snapshot["candidates"] if c["hospital_id"] == req.hospital_id), None)
        eta_min = candidate["eta_min"] if candidate else 10
        my_run_id = _start_movement_leg(trip, req.hospital_id, eta_min)

        state.log("hospital_selected", {
            "trip_id": trip_id,
            "hospital_id": req.hospital_id,
            "selection_type": trip["selection_type"],
            "override_reason": trip["override_reason"],
        })

        # AI hospital handover note: a short natural-language sentence for
        # the receiving ED (not just a bare condition code + ETA), via
        # IBM watsonx.ai with a deterministic template fallback - see
        # triage_service.generate_handover_note()'s docstring.
        destination_hospital = state.hospitals.get(req.hospital_id, {})
        handover = triage_service.generate_handover_note(
            trip.get("last_triage_note") or "",
            state.condition_by_code.get(trip["condition_code"], {}),
            eta_min,
            destination_hospital.get("name", req.hospital_id),
            patient_info={"age": trip.get("patient_age"), "sex": trip.get("patient_sex"), "notes": trip.get("patient_notes")},
        )

        prealert = {
            "id": str(uuid.uuid4())[:8],
            "hospital_id": req.hospital_id,
            "sent_at": _now_iso(),
            "condition_label": snapshot["condition_label"],
            "eta_min": eta_min,
            "handover_note": handover["summary"],
            "handover_source": handover["source"],
            "status": "pending",
            "acknowledged_at": None,
            "responded_at": None,
            "reject_reason": None,
        }
        trip["prealert"] = prealert
        _persist_trip(trip)
        state.log("prealert_sent", {
            "trip_id": trip_id,
            "hospital_id": req.hospital_id,
            "eta_min": eta_min,
            "handover_source": handover["source"],
        })

    await _broadcast_hospitals()
    await _broadcast_trip(trip)
    asyncio.create_task(_run_movement_loop(trip_id, my_run_id))
    return trip


@app.post("/api/trips/{trip_id}/prealert/respond")
async def respond_to_prealert(trip_id: str, req: PrealertRespondRequest, request: Request):
    """The receiving hospital accepts or rejects the inbound patient. Accept
    just acknowledges (prepares the team). Reject immediately excludes this
    hospital from that trip's recommendation and triggers a real reroute -
    the same "capacity changed, find the next best option" path a live
    capacity edit triggers, not a scripted animation."""
    user = auth.require_role(request, "hospital")
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        pre = trip.get("prealert")
        if not pre:
            raise HTTPException(status_code=400, detail="no active pre-alert for this trip")
        _require_hospital_scope(user, pre["hospital_id"])

        pre["responded_at"] = _now_iso()
        if req.accept:
            pre["status"] = "accepted"
            pre["acknowledged_at"] = _now_iso()
            state.log("prealert_accepted", {"trip_id": trip_id, "hospital_id": pre["hospital_id"], "by": user["username"]})
        else:
            pre["status"] = "rejected"
            pre["reject_reason"] = req.reason
            trip["rejected_hospital_ids"] = list(set((trip.get("rejected_hospital_ids") or []) + [pre["hospital_id"]]))
            state.log("prealert_rejected", {
                "trip_id": trip_id, "hospital_id": pre["hospital_id"], "reason": req.reason, "by": user["username"],
            })
        _persist_trip(trip)
        trigger_reroute = not req.accept and trip["status"] == "en_route"

    await _broadcast_trip(trip)
    if trigger_reroute:
        await _check_for_reroute(trip_id, force_reason=f"{state.hospitals.get(pre['hospital_id'], {}).get('name', pre['hospital_id'])} declined this transfer")
    return trip


@app.post("/api/trips/{trip_id}/position")
async def report_position(trip_id: str, req: PositionReport, request: Request):
    """Real ambulance position reported by the crew's own device (the
    frontend wires this to the browser Geolocation API). This is the only
    genuinely "live" position source this system has, since there is no
    physical vehicle telemetry feed behind it - see this module's docstring
    for how it interacts with the route-interpolation fallback."""
    auth.require_role(request, "dispatcher")
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        trip["ambulance_pos"] = {"lat": req.lat, "lng": req.lng}
        trip["position_source"] = "device_gps"
        trip["last_device_update_at"] = time.time()
        _persist_trip(trip)

    await manager.broadcast({"type": "position_updated", "trip_id": trip_id, "trip": trip})
    return {"ok": True}


@app.post("/api/trips/{trip_id}/reroute-response")
async def reroute_response(trip_id: str, req: RerouteResponse, request: Request):
    auth.require_role(request, "dispatcher")
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        alert = trip.get("reroute_alert")
        if not alert:
            raise HTTPException(status_code=400, detail="no active reroute alert")

        if req.accept:
            old_hospital_id = trip["dest_hospital_id"]
            new_hospital_id = alert["snapshot"]["recommended_hospital_id"]
            if old_hospital_id:
                _release_hospital(old_hospital_id, trip["condition_code"])
            _reserve_hospital(new_hospital_id, trip["condition_code"])
            trip["dest_hospital_id"] = new_hospital_id
            trip["selection_type"] = "accepted"
            trip["recommendation"] = alert["snapshot"]
            candidate = next(c for c in alert["snapshot"]["candidates"] if c["hospital_id"] == new_hospital_id)
            my_run_id = _start_movement_leg(trip, new_hospital_id, candidate["eta_min"])

            prealert = {
                "id": str(uuid.uuid4())[:8],
                "hospital_id": new_hospital_id,
                "sent_at": _now_iso(),
                "condition_label": alert["snapshot"]["condition_label"],
                "eta_min": candidate["eta_min"],
                "handover_note": None,
                "handover_source": None,
                "status": "pending",
                "acknowledged_at": None,
                "responded_at": None,
                "reject_reason": None,
            }
            trip["prealert"] = prealert
            state.log("reroute_accepted", {"trip_id": trip_id, "new_hospital_id": new_hospital_id})
            restart_movement = True
        else:
            state.log("reroute_declined", {"trip_id": trip_id, "reason": alert.get("reason")})
            restart_movement = False

        trip["reroute_alert"] = None
        _persist_trip(trip)

    await _broadcast_hospitals()
    await _broadcast_trip(trip)
    if restart_movement:
        asyncio.create_task(_run_movement_loop(trip_id, my_run_id))
    return trip


@app.get("/api/audit")
def get_audit():
    return state.audit_log


@app.get("/api/analytics")
def get_analytics():
    events = state.audit_log
    recs = [e for e in events if e["event_type"] == "recommendation_generated"]
    selections = [e for e in events if e["event_type"] == "hospital_selected"]
    overrides = [e for e in selections if e["payload"]["selection_type"] == "overridden"]
    reroutes = [e for e in events if e["event_type"] == "reroute_accepted"]
    prealerts_sent = [e for e in events if e["event_type"] == "prealert_sent"]
    prealerts_ack = [e for e in events if e["event_type"] in ("prealert_accepted", "prealert_acknowledged")]
    prealerts_rejected = [e for e in events if e["event_type"] == "prealert_rejected"]
    return {
        "total_cases": len(recs),
        "total_selections": len(selections),
        "override_count": len(overrides),
        "override_rate": (len(overrides) / len(selections)) if selections else 0,
        "reroute_count": len(reroutes),
        "prealerts_sent": len(prealerts_sent),
        "prealerts_acknowledged": len(prealerts_ack),
        "prealerts_rejected": len(prealerts_rejected),
        "prealert_ack_rate": (len(prealerts_ack) / len(prealerts_sent)) if prealerts_sent else 0,
        "active_trips": len(state.trips),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not auth.check_ws_token(ws.query_params.get("token")):
        await ws.close(code=4401)  # custom close code in the 4000-4999 (app-defined) range
        return
    await manager.connect(ws)
    try:
        for trip in state.trips.values():
            await ws.send_json({"type": "trip_updated", "trip_id": trip["id"], "trip": trip})
        await ws.send_json({"type": "trips_list_updated", "trips": _public_trip_list()})
        await ws.send_json({"type": "hospitals_updated", "hospitals": list(state.hospitals.values())})
        while True:
            await ws.receive_text()  # keep-alive; client doesn't need to send anything meaningful
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Ambulance position tracking + dynamic rerouting check (per trip)
# ---------------------------------------------------------------------------

async def _run_movement_loop(trip_id: str, run_id: int):
    """run_id guards against a subtle race: a reroute starts a NEW movement
    loop for the same trip, but the OLD loop instance is still alive (its
    own while-loop hasn't noticed yet) and would otherwise keep computing
    positions from a stale origin/destination in parallel with the new one.
    Each loop only keeps running while it's still the trip's current
    (latest) run - an older one recognizes it's been superseded and exits.

    Real time, not accelerated: movement_duration_sec is the real ETA in
    real seconds, so this loop reaches frac=1.0 exactly when the ambulance
    would actually arrive."""
    started_run = time.time()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        async with STATE_LOCK:
            trip = state.trips.get(trip_id)
            if not trip or trip["status"] != "en_route" or trip.get("movement_run_id") != run_id:
                return
            started = trip["movement_started_at"]
            duration = trip["movement_duration_sec"] or 1
            elapsed = time.time() - started
            frac = min(elapsed / duration, 1.0)

            # A recent real device-GPS report (see POST .../position) wins
            # over the interpolated estimate - only fall back to walking the
            # routed path when no fresh real position has been reported.
            has_fresh_device_pos = (
                trip.get("last_device_update_at") is not None
                and (time.time() - trip["last_device_update_at"]) < DEVICE_GPS_FRESHNESS_SEC
            )
            if not has_fresh_device_pos:
                dest_h = state.hospitals[trip["dest_hospital_id"]]
                path = trip.get("route_points") or [
                    {"lat": trip["origin_pos"]["lat"], "lng": trip["origin_pos"]["lng"]},
                    {"lat": dest_h["lat"], "lng": dest_h["lng"]},
                ]
                trip["ambulance_pos"] = interpolate_along_path(path, frac)
                trip["position_source"] = "estimated"

            arrived = frac >= 1.0
            if arrived:
                trip["status"] = "arrived"
                trip["finished_at"] = _now_iso()
                state.log("arrived", {"trip_id": trip_id, "hospital_id": trip["dest_hospital_id"]})
            _persist_trip(trip)

        if arrived:
            await _broadcast_trip(trip)
            return

        await manager.broadcast({"type": "position_updated", "trip_id": trip_id, "trip": trip})

        # Throttled - each check calls the live traffic/ETA API for every
        # hospital, so checking too often burns API quota fast for no
        # benefit.
        if time.time() - started_run > REROUTE_CHECK_EVERY_SEC:
            started_run = time.time()
            await _check_for_reroute(trip_id)


async def _check_for_reroute(trip_id: str, force_reason: Optional[str] = None):
    async with STATE_LOCK:
        trip = state.trips.get(trip_id)
        if not trip or trip["status"] != "en_route" or trip.get("reroute_alert"):
            return
        pos = trip["ambulance_pos"]
        exclude_ids = set(trip.get("rejected_hospital_ids") or [])
        snapshot = compute_recommendation(pos["lat"], pos["lng"], trip["condition_code"], trip_id, exclude_ids=exclude_ids)
        current_id = trip["dest_hospital_id"]
        current_candidate = next((c for c in snapshot["candidates"] if c["hospital_id"] == current_id), None)
        best_id = snapshot["recommended_hospital_id"]

        reason = force_reason
        if reason is None:
            if current_candidate and not current_candidate["viable"]:
                reason = f"Current destination no longer viable: {', '.join(current_candidate['disqualifiers'])}"
            elif best_id and best_id != current_id:
                best_candidate = next(c for c in snapshot["candidates"] if c["hospital_id"] == best_id)
                if current_candidate and best_candidate["score"] > current_candidate["score"] * 1.2:
                    reason = (
                        f"{state.hospitals[best_id]['name']} now has a significantly better "
                        f"estimated treatment delay ({best_candidate['est_total_treatment_delay_min']:.0f} min "
                        f"vs {current_candidate['est_total_treatment_delay_min']:.0f} min)"
                    )

        if reason:
            trip["reroute_alert"] = {"snapshot": snapshot, "reason": reason}
            _persist_trip(trip)
            state.log("reroute_suggested", {"trip_id": trip_id, "reason": reason, "suggested_hospital_id": best_id})

    if reason:
        await manager.broadcast({"type": "reroute_suggested", "trip_id": trip_id, "trip": trip})


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
