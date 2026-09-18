"""FastAPI dependency for a per-request SQLAlchemy ORM session (models.py's
Base/SessionLocal) - separate from db.py's short-lived Core connections,
which keep serving the legacy audit_log/trips tables untouched."""
from models import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
