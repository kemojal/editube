from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import DevicePushToken, Notification, User
from app.utils.security import get_current_user
from app.websocket_manager import notifications_ws_manager

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"],
)


class NotificationResponse(BaseModel):
    id: int
    user_id: int
    type: str
    read: bool
    project_id: Optional[int] = None
    video_id: Optional[int] = None
    comment_id: Optional[int] = None
    workspace_id: Optional[int] = None
    workspace_invite_id: Optional[int] = None
    invite_token: Optional[str] = None
    message: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class NotificationsPageResponse(BaseModel):
    items: list[NotificationResponse]
    total: int
    unread_count: int
    limit: int
    has_more: bool
    next_cursor_created_at: Optional[datetime] = None
    next_cursor_id: Optional[int] = None


class DevicePushTokenUpsert(BaseModel):
    token: str
    platform: str
    device_name: Optional[str] = None
    app_version: Optional[str] = None


def _serialize_notification(notification: Notification) -> NotificationResponse:
    return NotificationResponse.model_validate(notification)


async def _emit_notification_event(user_id: int, event: str, payload: dict) -> None:
    await notifications_ws_manager.send_to_user(
        user_id,
        {"event": event, "payload": payload},
    )


@router.get("/", response_model=NotificationsPageResponse)
def get_notifications(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor_created_at: Optional[datetime] = Query(default=None),
    cursor_id: Optional[int] = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if "offset" in request.query_params:
        raise HTTPException(
            status_code=400,
            detail="offset pagination is deprecated; use cursor_created_at + cursor_id",
        )

    if (cursor_created_at is None) != (cursor_id is None):
        raise HTTPException(
            status_code=400,
            detail="cursor_created_at and cursor_id must be provided together",
        )

    base_query = db.query(Notification).filter(Notification.user_id == current_user.id)
    total = base_query.count()
    unread_count = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id, Notification.read == False)  # noqa: E712
        .count()
    )
    ordered_query = base_query.order_by(Notification.created_at.desc(), Notification.id.desc())
    if cursor_created_at is not None and cursor_id is not None:
        ordered_query = ordered_query.filter(
            or_(
                Notification.created_at < cursor_created_at,
                and_(
                    Notification.created_at == cursor_created_at,
                    Notification.id < cursor_id,
                ),
            )
        )
    notifications = ordered_query.limit(limit).all()
    has_more = len(notifications) == limit

    next_cursor_created_at: Optional[datetime] = None
    next_cursor_id: Optional[int] = None
    if notifications and has_more:
        last_item = notifications[-1]
        next_cursor_created_at = last_item.created_at
        next_cursor_id = last_item.id

    return NotificationsPageResponse(
        items=[_serialize_notification(notification) for notification in notifications],
        total=total,
        unread_count=unread_count,
        limit=limit,
        has_more=has_more,
        next_cursor_created_at=next_cursor_created_at,
        next_cursor_id=next_cursor_id,
    )


@router.get("/summary")
def get_notifications_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    unread_count = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id, Notification.read == False)  # noqa: E712
        .count()
    )
    return {"unread_count": unread_count}


@router.post("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_as_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    notification = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.user_id == current_user.id)
        .first()
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    if not notification.read:
        notification.read = True
        db.commit()
        db.refresh(notification)
        await _emit_notification_event(
            current_user.id,
            "notification.read",
            {"id": notification.id, "read": True},
        )

    return _serialize_notification(notification)


@router.post("/read-all")
async def mark_all_notifications_as_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id, Notification.read == False)  # noqa: E712
        .all()
    )
    changed_ids = [notification.id for notification in notifications]
    if changed_ids:
        for notification in notifications:
            notification.read = True
        db.commit()
        await _emit_notification_event(
            current_user.id,
            "notification.read_all",
            {"ids": changed_ids},
        )
    return {"ok": True, "updated_count": len(changed_ids)}


@router.post("/push-tokens")
def upsert_push_token(
    body: DevicePushTokenUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    normalized = body.token.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="token is required")
    token_row = db.query(DevicePushToken).filter(DevicePushToken.token == normalized).first()
    if token_row is None:
        token_row = DevicePushToken(
            user_id=current_user.id,
            token=normalized,
            platform=(body.platform or "unknown").strip().lower(),
            device_name=body.device_name,
            app_version=body.app_version,
            enabled=True,
        )
        db.add(token_row)
    else:
        token_row.user_id = current_user.id
        token_row.platform = (body.platform or token_row.platform or "unknown").strip().lower()
        token_row.device_name = body.device_name
        token_row.app_version = body.app_version
        token_row.enabled = True
    db.commit()
    db.refresh(token_row)
    return {"ok": True, "id": token_row.id}


@router.delete("/push-tokens")
def revoke_push_token(
    token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(DevicePushToken)
        .filter(
            DevicePushToken.user_id == current_user.id,
            DevicePushToken.token == token.strip(),
        )
        .first()
    )
    if not row:
        return {"ok": True, "revoked": False}
    row.enabled = False
    db.commit()
    return {"ok": True, "revoked": True}