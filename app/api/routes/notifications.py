from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.db.models import Notification, User
from app.utils.security import get_current_user
from app.websocket_manager import manager

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"],
)

@router.get("/")
def get_notifications(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    notifications = db.query(Notification).filter(Notification.user_id == current_user.id).all()
    return notifications

@router.post("/{notification_id}/read")
async def mark_notification_as_read(notification_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    notification = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == current_user.id).first()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.read = True
    db.commit()
    db.refresh(notification)
    # Broadcast the update to the connected WebSocket clients
    await manager.broadcast(f"Notification {notification_id} marked as read by user {current_user.id}")

    return notification