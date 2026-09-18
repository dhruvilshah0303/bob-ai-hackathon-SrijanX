"""GET /api/notifications (own notifications only) and mark-as-read - the
write side (creating notifications) is notification_service.notify(),
called by trip_routes.py at the points that need to alert someone."""
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth_service import get_current_user
from db_session import get_db
from models import Notification, User

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class NotificationPublic(BaseModel):
    id: str
    type: str
    title: str
    message: str
    read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=List[NotificationPublic])
def list_notifications(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(200)
        .all()
    )
    return [NotificationPublic.model_validate(r) for r in rows]


@router.post("/{notification_id}/read", response_model=NotificationPublic)
def mark_read(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notification = db.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        raise HTTPException(status_code=404, detail="notification not found")
    notification.read = True
    db.commit()
    db.refresh(notification)
    return NotificationPublic.model_validate(notification)
