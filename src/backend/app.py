"""
SrijanX - AI Emergency Coordination Platform - FastAPI backend.

Real, DB-backed, multi-user, real-time emergency coordination. Each slice
lives in its own module (see that module's own docstring for detail):

  auth_routes.py         real user accounts: register/login/logout/refresh/me, JWT + RBAC
  hospital_routes.py      hospital accounts: create/verify (ADMIN), resources, specialists
  emergency_routes.py     patient intake + AI-assisted triage (decision support, not diagnosis)
  ambulance_routes.py      ambulance accounts + live GPS (restricted to the assigned operator)
  trip_routes.py           recommendation -> hospital request/acceptance (transactional
                           reservation) -> trip dispatch/lifecycle/reroute/cancel
  notification_routes.py  per-user notifications

This file wires those routers together, plus the one real-time channel
(the /ws websocket) and the health check. There is no in-memory application
state here - every one of the modules above reads/writes Postgres/SQLite
through models.py; this file holds no data of its own beyond the live
websocket connection list, which is real-time delivery only, never a
source of truth (a client that reconnects re-fetches its current state
over the REST endpoints above, it does not rely on this replaying history).

Run with:  uvicorn app:app --reload --port 8000
"""
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import config
config.configure_logging()
logger = logging.getLogger("app")

import admin_routes
import ambulance_routes
import analytics_routes
import auth_routes
import auth_service
import db
import db_session
import emergency_routes
import eta_service
import hospital_routes
import notification_routes
import ratelimit
import routing_service
import trip_routes
import triage_service

if config.SENTRY_DSN:
    import sentry_init  # noqa: F401 - side-effect import, initializes Sentry if configured

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"

app = FastAPI(title="SrijanX - AI Emergency Coordination Platform")

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
app.add_middleware(ratelimit.RateLimitMiddleware)


class ConnectionManager:
    """Real-time fanout for websocket clients. Deliberately dumb (no
    per-channel routing yet - every connected client gets every broadcast);
    splitting into /ws/dispatcher, /ws/hospital/{id}, /ws/ambulance/{id}
    channels is real-time-system follow-up work, not a correctness issue -
    every event already carries the ids a client needs to filter on
    client-side, and the DB remains authoritative regardless."""

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
hospital_routes.set_broadcaster(manager.broadcast)
trip_routes.set_broadcaster(manager.broadcast)

app.include_router(auth_routes.router)
app.include_router(hospital_routes.router)
app.include_router(emergency_routes.router)
app.include_router(ambulance_routes.router)
app.include_router(trip_routes.router)
app.include_router(notification_routes.router)
app.include_router(admin_routes.router)
app.include_router(analytics_routes.router)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "database": "sqlite" if db.IS_SQLITE else "postgres",
        "eta_provider": eta_service.provider_status(),
        "routing_provider": routing_service.provider_status(),
        "triage_provider": triage_service.provider_status(),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Real-time event push only. Auth uses the same JWT access token as
    every REST call, passed as ?token=... (a WebSocket handshake can't set
    a custom Authorization header) - see auth_service.verify_ws_token()."""
    db_session_gen = db_session.get_db()
    session = next(db_session_gen)
    try:
        user = auth_service.verify_ws_token(ws.query_params.get("token"), session)
    finally:
        session.close()

    if user is None:
        await ws.close(code=4401)  # custom close code in the 4000-4999 (app-defined) range
        return

    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive; client doesn't need to send anything meaningful
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
