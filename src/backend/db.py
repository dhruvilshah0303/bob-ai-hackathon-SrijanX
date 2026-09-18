"""
Database connection - SQLite by default (zero setup for local dev), or a
real Postgres database when DATABASE_URL is set (what you want the moment
this is deployed anywhere that wipes local disk on redeploy, which is most
hosting platforms).

Just the engine/URL resolution lives here; the actual schema is
models.py's SQLAlchemy ORM models, migrated with Alembic (see alembic/) -
this module has no tables of its own.
"""
import logging

from sqlalchemy import create_engine

import config

logger = logging.getLogger(__name__)


def _resolve_database_url() -> str:
    url = config.DATABASE_URL
    if not url:
        from pathlib import Path
        sqlite_path = Path(__file__).parent / "coordinator.db"
        return f"sqlite:///{sqlite_path}"
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
