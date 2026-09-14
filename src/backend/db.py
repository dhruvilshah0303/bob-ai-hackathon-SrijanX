"""
Persistence for the audit log and trip history - SQLite by default (zero
setup for local dev), or a real Postgres database when DATABASE_URL is set
(what you want the moment this is deployed anywhere that wipes local disk
on redeploy, which is most hosting platforms).

This is deliberately still small (SQLAlchemy Core, no ORM, no migrations
framework) - the point is portability between SQLite and Postgres with one
codebase, not building a full data layer. Active, in-transit trip state
(position, live recommendation) still lives in memory in app.py for speed;
only the append-only audit log and finished/cancelled trips are persisted
here, matching the single-instance architecture documented in the README.
"""
import json
import logging
from pathlib import Path

from sqlalchemy import (
    Column, DateTime, MetaData, String, Table, Text, create_engine, delete, select
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

_engine_kwargs = {"connect_args": {"check_same_thread": False}} if IS_SQLITE else {}
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


def upsert_trip(trip: dict):
    """Persist a snapshot of a trip - called when it finishes (arrived) or
    is discarded, so trip history/outcomes survive a restart even though
    the live, in-transit version stays in memory for speed."""
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


def load_trips(limit: int = 200) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(trips_table.c.data).order_by(trips_table.c.created_at.desc()).limit(limit)
        ).all()
    return [json.loads(r[0]) for r in rows]


def reset_all():
    """Wipe persisted history - used by the demo's full-reset button so a
    fresh run doesn't mix with a previous run's analytics."""
    with engine.begin() as conn:
        conn.execute(delete(audit_log_table))
        conn.execute(delete(trips_table))
