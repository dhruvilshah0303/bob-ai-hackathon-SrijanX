"""
Persistence layer - SQLite by default (zero setup for local dev), or a real
Postgres database when DATABASE_URL is set (what you want the moment this
is deployed anywhere that wipes local disk on redeploy, which is most
hosting platforms).

This is deliberately still small (SQLAlchemy Core, no ORM, no migrations
framework) - the point is portability between SQLite and Postgres with one
codebase, not building a full data layer.

What's persisted here (all of it now - this used to only cover the
append-only audit log and finished/cancelled trips, which meant an
in-progress trip or a hospital's live bed count was lost on every restart;
that gap is closed):
  - audit_log: the append-only event log
  - trips: EVERY trip, upserted on every state change (not just at
    finish/cancel) so an in-flight trip survives a server restart
  - hospitals: live bed/specialist/accepting-status capacity, upserted on
    every change so a capacity edit survives a restart instead of being
    silently reset back to the seed data in data/hospitals.json
  - users / sessions: real login accounts and their active session tokens
"""
import json
import logging
from pathlib import Path

from sqlalchemy import (
    Column, MetaData, String, Table, Text, create_engine, delete, select
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import config

logger = logging.getLogger(__name__)

_SQLITE_PATH = Path(__file__).parent / "coordinator.db"


def _resolve_database_url() -> str:
    url = config.DATABASE_URL
    if not url:
        return f"sqlite:///{_SQLITE_PATH}"
    # Some platforms (Render, Heroku-style) hand out "postgres://", but
    # SQLAlchemy 2.x requires the "postgresql://" scheme.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


DATABASE_URL = _resolve_database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# pool_pre_ping: issue a cheap "SELECT 1" before handing out a pooled
# connection, so a connection that a cloud Postgres provider silently closed
# (idle timeout, failover, restart) gets transparently replaced instead of
# surfacing as a confusing "server closed the connection unexpectedly" error
# on the next real query. Meaningless for SQLite (no connection pool to
# stale-check), so only applied on the Postgres branch.
_engine_kwargs = (
    {"connect_args": {"check_same_thread": False}}
    if IS_SQLITE
    else {"pool_pre_ping": True}
)
engine = create_engine(DATABASE_URL, **_engine_kwargs)
metadata = MetaData()

audit_log_table = Table(
    "audit_log", metadata,
    Column("id", String(64), primary_key=True),
    Column("event_type", String(128), nullable=False),
    Column("payload", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
)

trips_table = Table(
    "trips", metadata,
    Column("id", String(64), primary_key=True),
    Column("ambulance_label", String(64)),
    Column("condition_code", String(64)),
    Column("dest_hospital_id", String(64)),
    Column("selection_type", String(32)),
    Column("status", String(32)),
    Column("created_at", String(64)),
    Column("finished_at", String(64)),
    Column("data", Text, nullable=False),
)

hospitals_table = Table(
    "hospitals", metadata,
    Column("id", String(64), primary_key=True),
    Column("data", Text, nullable=False),
    Column("updated_at", String(64)),
)

users_table = Table(
    "users", metadata,
    Column("id", String(64), primary_key=True),
    Column("username", String(128), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("role", String(32), nullable=False),  # "admin" | "dispatcher" | "hospital"
    Column("hospital_id", String(64)),  # set only for role="hospital"
    Column("created_at", String(64)),
)

sessions_table = Table(
    "sessions", metadata,
    Column("token", String(128), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("username", String(128), nullable=False),
    Column("role", String(32), nullable=False),
    Column("hospital_id", String(64)),
    Column("created_at", String(64)),
    Column("expires_at", String(64), nullable=False),
)


def init_db():
    metadata.create_all(engine)
    logger.info("Database ready (%s)", "SQLite" if IS_SQLITE else "Postgres")


def _upsert(table, values: dict, conflict_col: str = "id"):
    insert_fn = sqlite_insert if IS_SQLITE else pg_insert
    stmt = insert_fn(table).values(**values)
    update_cols = {c: stmt.excluded[c] for c in values if c != conflict_col}
    stmt = stmt.on_conflict_do_update(index_elements=[conflict_col], set_=update_cols)
    with engine.begin() as conn:
        conn.execute(stmt)


# --------------------------------------------------------------- audit ----

def insert_audit_event(event: dict):
    _upsert(audit_log_table, {
        "id": event["id"],
        "event_type": event["event_type"],
        "payload": json.dumps(event["payload"]),
        "created_at": event["created_at"],
    })


def load_audit_log(limit: int = 1000) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(audit_log_table).order_by(audit_log_table.c.created_at.asc()).limit(limit)
        ).mappings().all()
    return [
        {"id": r["id"], "event_type": r["event_type"], "payload": json.loads(r["payload"]), "created_at": r["created_at"]}
        for r in rows
    ]


# ---------------------------------------------------------------- trips ---

def upsert_trip(trip: dict):
    """Persist a snapshot of a trip. Called on every meaningful state change
    (not just when it finishes) so an in-progress trip - its condition,
    destination, live position, pre-alert - survives a server restart
    instead of vanishing because it only lived in the in-memory state.trips
    dict."""
    _upsert(trips_table, {
        "id": trip["id"],
        "ambulance_label": trip.get("ambulance_label"),
        "condition_code": trip.get("condition_code"),
        "dest_hospital_id": trip.get("dest_hospital_id"),
        "selection_type": trip.get("selection_type"),
        "status": trip.get("status"),
        "created_at": trip.get("created_at"),
        "finished_at": trip.get("finished_at"),
        "data": json.dumps(trip),
    })


def load_trips(limit: int = 500) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(trips_table.c.data).order_by(trips_table.c.created_at.desc()).limit(limit)
        ).all()
    return [json.loads(r[0]) for r in rows]


def load_active_trips() -> list[dict]:
    """Trips that hadn't reached a terminal state (arrived/cancelled) the
    last time they were persisted - reloaded into memory at startup so an
    in-flight emergency isn't lost across a restart/redeploy."""
    trips = load_trips()
    return [t for t in trips if t.get("status") not in ("arrived", "cancelled")]


def delete_trip(trip_id: str):
    with engine.begin() as conn:
        conn.execute(delete(trips_table).where(trips_table.c.id == trip_id))


# ------------------------------------------------------------ hospitals ---

def upsert_hospital(hospital: dict):
    """Persist a hospital's current capacity/status. Called on every
    capacity mutation (manual edit, bed reservation on selection, bed
    release on cancel/reroute) so live hospital state survives a restart
    instead of silently reverting to the seed data in data/hospitals.json
    on every process start."""
    _upsert(hospitals_table, {
        "id": hospital["id"],
        "data": json.dumps(hospital),
        "updated_at": hospital.get("last_capacity_update_at"),
    })


def load_hospitals() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(hospitals_table.c.data)).all()
    return [json.loads(r[0]) for r in rows]


# ----------------------------------------------------------------- users --

def create_user(user: dict):
    with engine.begin() as conn:
        conn.execute(users_table.insert().values(**user))


def get_user_by_username(username: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(users_table).where(users_table.c.username == username)
        ).mappings().first()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(select(users_table).where(users_table.c.id == user_id)).mappings().first()
    return dict(row) if row else None


def count_users() -> int:
    with engine.connect() as conn:
        return len(conn.execute(select(users_table.c.id)).all())


def list_users() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(users_table)).mappings().all()
    return [dict(r) for r in rows]


# -------------------------------------------------------------- sessions --

def create_session(session: dict):
    with engine.begin() as conn:
        conn.execute(sessions_table.insert().values(**session))


def get_session(token: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(select(sessions_table).where(sessions_table.c.token == token)).mappings().first()
    return dict(row) if row else None


def delete_session(token: str):
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.token == token))


def purge_expired_sessions(now_iso: str):
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.expires_at < now_iso))


# ----------------------------------------------------------------- reset --

def reset_operational_state():
    """Wipe trip/audit/hospital-capacity state - NOT user accounts or
    sessions. Used internally (directly, not via an HTTP route - see the
    removed /api/reset endpoint) to give each test its own clean slate.
    There is no production/API path that calls this: a real coordination
    system should never expose a button that erases live trip and audit
    history."""
    with engine.begin() as conn:
        conn.execute(delete(audit_log_table))
        conn.execute(delete(trips_table))
        conn.execute(delete(hospitals_table))
