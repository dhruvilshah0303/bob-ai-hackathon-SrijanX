"""Creates Notification rows - the DB is the source of truth a client can
always reload from (GET /api/notifications, see notification_routes.py).
Deliberately synchronous and broadcast-free: the async route handlers that
call this (trip_routes.py) broadcast NOTIFICATION_CREATED themselves right
after, the same way hospital_routes.py/emergency_routes.py broadcast their
own events - keeping this helper usable from any context (sync or async)
without needing its own fragile cross-thread event-loop handling."""
from sqlalchemy.orm import Session

from models import Notification


def notify(db: Session, user_id: str, notif_type: str, title: str, message: str) -> Notification:
    notification = Notification(user_id=user_id, type=notif_type, title=title, message=message)
    db.add(notification)
    db.flush()
    return notification
