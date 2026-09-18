"""
AI Ambulance-to-Hospital Coordinator - Demo Prototype Backend
===============================================================
FastAPI backend implementing a simplified version of the TRD architecture:
  - Hospital Capacity Service (in-memory, mutable via API - simulates a
    hospital dashboard updating bed/specialist status)
  - Severity Intake -> required-capability mapping
  - ETA / Routing Service (real API with graceful fallback - eta_service.py)
  - AI Recommendation Engine (recommendation_engine.py)
  - Rerouting / Alerting Engine (per-trip background loop + websocket push)
  - Pre-Alert service (simulated hospital-side acknowledgment)
  - Audit log + trip history (in-memory for speed, persisted via db.py so a
    restart doesn't erase it - see the code review notes in db.py)

Multi-ambulance support: the system tracks any number of concurrent trips
(state.trips, keyed by trip_id), because hospital capacity is a single
shared resource multiple ambulances compete for - a first version of this
tracked exactly one global trip, which meant it couldn't actually show two
ambulances contending for the same last ICU bed (the core "why this
matters" story). All hospital-capacity mutations and trip-state mutations
go through STATE_LOCK - a single coarse-grained lock. That's a deliberate
simplification: at demo/pilot request volume, a single lock costs nothing
in practice and makes correctness easy to reason about; splitting it into
finer-grained per-hospital/per-trip locks is a reasonable follow-up once
real concurrent load makes the coarse lock measurably slow, not before.

Run with:  uvicorn app:app --reload --port 8000
Then open http://localhost:8000 in a browser.
"""
import asyncio
import json
import logging
import os
import random
import string
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

import config
config.configure_logging()
logger = logging.getLogger("app")

import auth
import auth_routes
import db
import eta_service
import ratelimit
import routing_service
import triage_service
from recommendation_engine import recommend
from utils import interpolate_along_path

if config.SENTRY_DSN:
    import sentry_init  # noqa: F401 - side-effect import, initializes Sentry if configured

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR.parent / "frontend"

DEMO_TIME_SCALE = float(os.environ.get("DEMO_TIME_SCALE", "10"))  # 1 real ETA-minute -> (60/scale) demo seconds
TICK_SECONDS = 0.5
REROUTE_CHECK_EVERY_SEC = 5.0  # throttled - see eta_service cost note in the code review

app = FastAPI(title="AI Ambulance-to-Hospital Coordinator")

config.startup_warnings(logger)
logger.info(
    "Starting up: environment=%s auth=%s db=%s cors_origins=%s",
    config.ENVIRONMENT,
    "ON" if config.APP_ACCESS_TOKEN else "off",
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

# Real user accounts (POST /api/auth/register|login|logout|refresh, GET
# /api/auth/me) - see auth_routes.py/auth_service.py. These sit alongside
# auth.AccessTokenMiddleware above rather than replacing it yet: the routes
# below this point (hospitals/trips/etc) are still the in-memory demo
# endpoints being migrated onto the real DB models phase by phase (see
# models.py's module docstring), so they keep using the shared-token gate
# until that migration lands - only /api/auth/* is real-JWT-protected so far.
app.include_router(auth_routes.router)


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

def _load_json(name):
    with open(DATA_DIR / name) as f:
        return json.load(f)


def _now_iso():
    return datetime.now().astimezone().isoformat()


def _new_ambulance_label():
    suffix = "".join(random.choices(string.digits, k=2))
    return f"AMB-{suffix}"


class State:
    def __init__(self):
        self.hospitals: dict = {}
        self.severity_rules: dict = {}
        self.scenario: dict = {}
        self.condition_by_code: dict = {}
        self.trips: dict = {}
        self.audit_log: list = []
        self.reset(wipe_history=False)

    def reset(self, wipe_history: bool = True):
        self.hospitals = {h["id"]: h for h in _load_json("hospitals.json")}
        # Stamp capacity data as "just updated" at demo start so the freshness
        # warning only appears after real time passes or a manual sim change.
        for h in self.hospitals.values():
            h["last_capacity_update_at"] = _now_iso()
        self.severity_rules = _load_json("severity_rules.json")
        self.scenario = _load_json("scenario.json")
        self.condition_by_code = {c["code"]: c for c in self.severity_rules["conditions"]}
        self.trips = {}

        if wipe_history:
            db.reset_all()
            self.audit_log = []
        else:
            # Startup path: recover audit history across a restart instead of
            # silently losing it (this was the persistence gap from the review).
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
            "dest_hospital_id": t["dest_hospital_id"],
            "condition_code": t["condition_code"],
            "autopilot": t.get("autopilot", False),
            "prealert": t.get("prealert"),
        }
        for t in state.trips.values()
    ]


async def _broadcast_trip(trip):
    await manager.broadcast({"type": "trip_updated", "trip_id": trip["id"], "trip": trip})
    await manager.broadcast({"type": "trips_list_updated", "trips": _public_trip_list()})


async def _broadcast_hospitals():
    await manager.broadcast({"type": "hospitals_updated", "hospitals": list(state.hospitals.values())})


# ---------------------------------------------------------------------------
# Reservation helpers (fixes the "no bed held on selection" bug from review)
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


def _release_hospital(hospital_id: str, condition_code: str):
    h = state.hospitals.get(hospital_id)
    if not h:
        return
    condition_rule = state.condition_by_code.get(condition_code, {})
    if "icu" in condition_rule.get("required", []):
        h["icu_beds_free"] = min(h["icu_beds_total"], h["icu_beds_free"] + 1)
    h["ed_bays_occupied"] = max(0, h["ed_bays_occupied"] - 1)
    h["last_capacity_update_at"] = _now_iso()


def _start_movement_leg(trip: dict, dest_hospital_id: str, eta_min: float) -> int:
    """Kicks off (or restarts, on reroute) the ambulance's movement toward
    dest_hospital_id: fetches a real road route (falling back to a straight
    line if no live routing provider is available - see routing_service.py)
    so the movement loop can follow actual roads instead of cutting a
    diagonal line across the map, and sets up the timing the movement loop
    reads. Returns the new movement_run_id so the caller can pass it to
    _run_movement_loop (see that function's docstring for why this matters).

    Called with STATE_LOCK already held, same as the ETA lookups in
    compute_recommendation() - a known simplification (a blocking network
    call inside the lock), not an oversight; see the code review notes.
    """
    dest = state.hospitals[dest_hospital_id]
    origin = trip["ambulance_pos"]
    route = routing_service.get_route(origin["lat"], origin["lng"], dest["lat"], dest["lng"], dest_hospital_id)
    trip["route_points"] = route["points"]
    trip["route_source"] = route["source"]
    trip["origin_pos"] = dict(origin)
    trip["movement_started_at"] = time.time()
    trip["movement_duration_sec"] = max(4.0, (eta_min * 60) / DEMO_TIME_SCALE)
    trip["movement_run_id"] = trip.get("movement_run_id", 0) + 1
    return trip["movement_run_id"]


# ---------------------------------------------------------------------------
# Core recommendation helper
# ---------------------------------------------------------------------------

def compute_recommendation(origin_lat, origin_lng, condition_code, trip_id: str) -> dict:
    condition_rule = state.condition_by_code[condition_code]
    eta_lookup = {}
    for hid, h in state.hospitals.items():
        eta = eta_service.get_eta(origin_lat, origin_lng, h["lat"], h["lng"], hid, seed_key=trip_id)
        eta_lookup[hid] = eta
    weights = state.severity_rules["scoring_weights"]
    stale_threshold = state.severity_rules["stale_data_threshold_minutes"]
    snapshot = recommend(list(state.hospitals.values()), eta_lookup, condition_rule, weights, stale_threshold)
    snapshot["generated_at"] = _now_iso()
    snapshot["condition_code"] = condition_code
    snapshot["condition_label"] = condition_rule["label"]
    return snapshot


def _new_trip(ambulance_label: Optional[str], incident_location: Optional[dict], autopilot: bool) -> dict:
    incident = dict(incident_location) if incident_location else dict(state.scenario["incident_location"])
    trip = {
        "id": str(uuid.uuid4())[:8],
        "ambulance_label": ambulance_label or _new_ambulance_label(),
        "status": "awaiting_assessment",
        "condition_code": None,
        "ambulance_pos": dict(incident),
        "origin_pos": dict(incident),
        "incident_location": dict(incident),
        "dest_hospital_id": None,
        "selection_type": None,
        "override_reason": None,
        "route_points": None,
        "route_source": None,
        "recommendation": None,
        "last_triage_suggestion": None,
        "last_triage_note": None,
        "prealert": None,
        "reroute_alert": None,
        "movement_started_at": None,
        "movement_duration_sec": None,
        "movement_run_id": 0,
        "created_at": _now_iso(),
        "finished_at": None,
        "autopilot": autopilot,
    }
    state.trips[trip["id"]] = trip
    return trip


def _get_trip_or_404(trip_id: str) -> dict:
    trip = state.trips.get(trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="trip not found (it may have arrived/been reset)")
    return trip


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

def _strip_control_chars(value: Optional[str]) -> Optional[str]:
    """Defense-in-depth alongside the frontend's HTML-escaping (see
    escapeHtml() in app.js): free-text fields get control characters
    stripped and whitespace trimmed server-side too, for any caller that
    isn't going through that frontend at all (curl, another client). This
    doesn't replace escaping on render - a client-side escaping bug would
    still be a bug - but it shrinks what a non-browser API caller can stuff
    into these fields in the first place."""
    if value is None:
        return None
    cleaned = "".join(ch for ch in value if ch == "\n" or ch == "\t" or not ord(ch) < 0x20)
    return cleaned.strip() or None


class NewTripRequest(BaseModel):
    ambulance_label: Optional[str] = Field(default=None, max_length=64)
    incident_location: Optional[dict] = None
    autopilot: bool = False

    @field_validator("ambulance_label")
    @classmethod
    def _clean_ambulance_label(cls, v):
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
    }


@app.get("/api/scenario")
def get_scenario():
    return {
        "incident_location": state.scenario["incident_location"],
        "conditions": state.severity_rules["conditions"],
        "eta_provider": eta_service.provider_status(),
        "routing_provider": routing_service.provider_status(),
        "triage_provider": triage_service.provider_status(),
    }


@app.get("/api/hospitals")
def get_hospitals():
    return list(state.hospitals.values())


@app.post("/api/hospitals/{hospital_id}/capacity")
async def update_capacity(hospital_id: str, update: CapacityUpdate):
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
            h["accepting_status"] = update.accepting_status
            changed["accepting_status"] = h["accepting_status"]
        if update.specialist_type is not None and update.specialist_on_duty is not None:
            for s in h["specialists"]:
                if s["type"] == update.specialist_type:
                    s["on_duty"] = update.specialist_on_duty
                    changed[f"specialist:{update.specialist_type}"] = update.specialist_on_duty
        h["last_capacity_update_at"] = _now_iso()

        state.log("capacity_update", {"hospital_id": hospital_id, "changed": changed})
        en_route_ids = [t["id"] for t in state.trips.values() if t["status"] == "en_route"]

    await _broadcast_hospitals()
    # Capacity is a shared resource - ANY en-route trip might now need a
    # reroute, not just "the" trip (this was a real gap before multi-trip
    # support: only a single global trip was ever re-checked).
    for tid in en_route_ids:
        await _check_for_reroute(tid)

    return {"ok": True, "hospital": h}


@app.post("/api/trips")
async def create_trip(req: NewTripRequest):
    async with STATE_LOCK:
        trip = _new_trip(req.ambulance_label, req.incident_location, req.autopilot)
        state.log("trip_created", {"trip_id": trip["id"], "ambulance_label": trip["ambulance_label"], "autopilot": trip["autopilot"]})
    await manager.broadcast({"type": "trips_list_updated", "trips": _public_trip_list()})
    if trip["autopilot"]:
        asyncio.create_task(_run_autopilot(trip["id"]))
    return trip


@app.get("/api/trips")
def list_trips():
    return list(state.trips.values())


@app.get("/api/trips/{trip_id}")
def get_trip(trip_id: str):
    return _get_trip_or_404(trip_id)


@app.delete("/api/trips/{trip_id}")
async def discard_trip(trip_id: str):
    async with STATE_LOCK:
        trip = state.trips.get(trip_id)
        if not trip:
            raise HTTPException(status_code=404, detail="trip not found")
        if trip["dest_hospital_id"] and trip["status"] not in ("arrived",):
            _release_hospital(trip["dest_hospital_id"], trip["condition_code"])
        trip["status"] = "cancelled"
        trip["finished_at"] = _now_iso()
        db.upsert_trip(trip)
        del state.trips[trip_id]
        state.log("trip_cancelled", {"trip_id": trip_id})

    await _broadcast_hospitals()
    await manager.broadcast({"type": "trip_removed", "trip_id": trip_id})
    await manager.broadcast({"type": "trips_list_updated", "trips": _public_trip_list()})
    return {"ok": True}


@app.post("/api/trips/{trip_id}/triage")
async def suggest_triage(trip_id: str, req: TriageRequest):
    """AI Triage Assist: classifies req.note into a suggested condition_code
    (does not itself change the trip's condition - see TriageRequest)."""
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        conditions = state.severity_rules["conditions"]
        suggestion = triage_service.classify(req.note, conditions)
        trip["last_triage_suggestion"] = suggestion
        trip["last_triage_note"] = req.note

        state.log("triage_suggested", {
            "trip_id": trip_id,
            "note": req.note,
            "suggested_condition_code": suggestion["condition_code"],
            "confidence": suggestion["confidence"],
            "source": suggestion["source"],
        })

    return suggestion


@app.post("/api/trips/{trip_id}/case")
async def create_case(trip_id: str, req: CaseRequest):
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        if req.condition_code not in state.condition_by_code:
            raise HTTPException(status_code=400, detail="unknown condition_code")

        trip["condition_code"] = req.condition_code
        origin = trip["ambulance_pos"]
        snapshot = compute_recommendation(origin["lat"], origin["lng"], req.condition_code, trip_id)
        trip["recommendation"] = snapshot
        trip["status"] = "recommendation_ready"

        state.log("recommendation_generated", {
            "trip_id": trip_id,
            "condition_code": req.condition_code,
            "recommended_hospital_id": snapshot["recommended_hospital_id"],
        })

    await _broadcast_trip(trip)
    return trip


@app.post("/api/trips/{trip_id}/select")
async def select_hospital(trip_id: str, req: SelectRequest):
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
        )

        prealert = {
            "id": str(uuid.uuid4())[:8],
            "hospital_id": req.hospital_id,
            "sent_at": _now_iso(),
            "condition_label": snapshot["condition_label"],
            "eta_min": eta_min,
            "handover_note": handover["summary"],
            "handover_source": handover["source"],
            "acknowledged_at": None,
        }
        trip["prealert"] = prealert
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


@app.post("/api/trips/{trip_id}/prealert/acknowledge")
async def acknowledge_prealert(trip_id: str):
    async with STATE_LOCK:
        trip = _get_trip_or_404(trip_id)
        if trip["prealert"]:
            trip["prealert"]["acknowledged_at"] = _now_iso()
            state.log("prealert_acknowledged", {"trip_id": trip_id, "hospital_id": trip["prealert"]["hospital_id"]})
    await _broadcast_trip(trip)
    return trip


@app.post("/api/trips/{trip_id}/reroute-response")
async def reroute_response(trip_id: str, req: RerouteResponse):
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
                "acknowledged_at": None,
            }
            trip["prealert"] = prealert
            state.log("reroute_accepted", {"trip_id": trip_id, "new_hospital_id": new_hospital_id})
            restart_movement = True
        else:
            state.log("reroute_declined", {"trip_id": trip_id, "reason": alert.get("reason")})
            restart_movement = False

        trip["reroute_alert"] = None

    await _broadcast_hospitals()
    await _broadcast_trip(trip)
    if restart_movement:
        asyncio.create_task(_run_movement_loop(trip_id, my_run_id))
    return trip


@app.post("/api/reset")
async def reset_demo():
    async with STATE_LOCK:
        state.reset(wipe_history=True)
    await _broadcast_hospitals()
    await manager.broadcast({"type": "trips_list_updated", "trips": []})
    return {"ok": True}


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
    prealerts_ack = [e for e in events if e["event_type"] == "prealert_acknowledged"]
    return {
        "total_cases": len(recs),
        "total_selections": len(selections),
        "override_count": len(overrides),
        "override_rate": (len(overrides) / len(selections)) if selections else 0,
        "reroute_count": len(reroutes),
        "prealerts_sent": len(prealerts_sent),
        "prealerts_acknowledged": len(prealerts_ack),
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
# Ambulance movement simulation + dynamic rerouting check (per trip)
# ---------------------------------------------------------------------------

async def _run_movement_loop(trip_id: str, run_id: int):
    """run_id guards against a subtle race: a reroute starts a NEW movement
    loop for the same trip, but the OLD loop instance is still alive (its
    own while-loop hasn't noticed yet) and would otherwise keep computing
    positions from a stale origin/destination in parallel with the new one.
    Each loop only keeps running while it's still the trip's current
    (latest) run - an older one recognizes it's been superseded and exits."""
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

            # Follows the actual road-route geometry (routing_service.py)
            # instead of lerping straight between origin and destination
            # lat/lng, which used to cut a diagonal line across the map
            # ignoring roads entirely. Falls back to a straight 2-point path
            # (identical to the old behavior) when no live routing provider
            # is available - see routing_service.py's docstring.
            dest_h = state.hospitals[trip["dest_hospital_id"]]
            path = trip.get("route_points") or [
                {"lat": trip["origin_pos"]["lat"], "lng": trip["origin_pos"]["lng"]},
                {"lat": dest_h["lat"], "lng": dest_h["lng"]},
            ]
            trip["ambulance_pos"] = interpolate_along_path(path, frac)

            arrived = frac >= 1.0
            if arrived:
                trip["status"] = "arrived"
                trip["finished_at"] = _now_iso()
                state.log("arrived", {"trip_id": trip_id, "hospital_id": trip["dest_hospital_id"]})
                db.upsert_trip(trip)

        if arrived:
            await _broadcast_trip(trip)
            return

        await manager.broadcast({"type": "position_updated", "trip_id": trip_id, "trip": trip})

        # Throttled - each check calls the live traffic/ETA API for every
        # hospital, so checking too often burns API quota fast for no
        # benefit (see code review notes).
        if time.time() - started_run > REROUTE_CHECK_EVERY_SEC:
            started_run = time.time()
            await _check_for_reroute(trip_id)


async def _check_for_reroute(trip_id: str):
    async with STATE_LOCK:
        trip = state.trips.get(trip_id)
        if not trip or trip["status"] != "en_route" or trip.get("reroute_alert"):
            return
        pos = trip["ambulance_pos"]
        snapshot = compute_recommendation(pos["lat"], pos["lng"], trip["condition_code"], trip_id)
        current_id = trip["dest_hospital_id"]
        current_candidate = next((c for c in snapshot["candidates"] if c["hospital_id"] == current_id), None)
        best_id = snapshot["recommended_hospital_id"]

        reason = None
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
            state.log("reroute_suggested", {"trip_id": trip_id, "reason": reason, "suggested_hospital_id": best_id})

    if reason:
        if trip.get("autopilot"):
            # The demo "second ambulance" drives itself - auto-accept a
            # reroute the same way a real dispatcher would after being
            # shown the same alert a human crew sees.
            await reroute_response(trip_id, RerouteResponse(accept=True))
        else:
            await manager.broadcast({"type": "reroute_suggested", "trip_id": trip_id, "trip": trip})


# ---------------------------------------------------------------------------
# Autopilot - drives a demo "second ambulance" end-to-end with no user input,
# so multi-ambulance behavior (hospitals as a shared, contested resource) is
# visible in a live demo without needing a second person/browser.
# ---------------------------------------------------------------------------

async def _run_autopilot(trip_id: str):
    await asyncio.sleep(1.0)
    async with STATE_LOCK:
        trip = state.trips.get(trip_id)
        if not trip:
            return
        condition_code = random.choice(list(state.condition_by_code.keys()))
    case_result = await create_case(trip_id, CaseRequest(condition_code=condition_code))
    recommended_id = case_result["recommendation"]["recommended_hospital_id"]
    if not recommended_id:
        return
    await asyncio.sleep(0.5)
    await select_hospital(trip_id, SelectRequest(hospital_id=recommended_id))


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
