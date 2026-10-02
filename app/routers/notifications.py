from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.errors import ApiError
from app.models import Notification, PushDevice, User, utcnow
from app.schemas import DeviceUpsert
from app.services import as_utc, decode_cursor, encode_cursor


router = APIRouter(prefix="/v1/me", tags=["notifications"])


def notification_payload(notification: Notification) -> dict:
    return {
        "id": str(notification.id),
        "category": notification.category,
        "title": notification.title,
        "body": notification.body,
        "relatedResourceType": notification.related_resource_type,
        "relatedResourceId": notification.related_resource_id,
        "createdAt": as_utc(notification.created_at).isoformat().replace("+00:00", "Z"),
        "readAt": as_utc(notification.read_at).isoformat().replace("+00:00", "Z") if notification.read_at else None,
    }


@router.get("/notifications")
def list_notifications(
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Notification).where(Notification.recipient_id == user.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    if cursor:
        instant, identifier = decode_cursor(cursor)
        query = query.where(
            (Notification.created_at < instant)
            | ((Notification.created_at == instant) & (Notification.id < identifier))
        )
    items = db.scalars(
        query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1)
    ).all()
    has_more = len(items) > limit
    items = items[:limit]
    return {
        "items": [notification_payload(item) for item in items],
        "nextCursor": encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None,
        "unreadCount": db.scalar(
            select(func.count(Notification.id)).where(
                Notification.recipient_id == user.id,
                Notification.read_at.is_(None),
            )
        ) or 0,
    }


@router.patch("/notifications/{notification_id}")
def mark_notification_read(
    notification_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    notification = db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.recipient_id == user.id,
        )
    )
    if notification is None:
        raise ApiError(404, "NOTIFICATION_NOT_FOUND", "Notification not found.")
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.commit()
        db.refresh(notification)
    return notification_payload(notification)


@router.post("/notifications/mark-all-read")
def mark_all_read(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    notifications = db.scalars(
        select(Notification).where(
            Notification.recipient_id == user.id,
            Notification.read_at.is_(None),
        )
    ).all()
    now = utcnow()
    for notification in notifications:
        notification.read_at = now
    db.commit()
    return {"updatedCount": len(notifications)}


@router.delete("/notifications/{notification_id}", status_code=204)
def delete_notification(
    notification_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    notification = db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.recipient_id == user.id,
        )
    )
    if notification is None:
        raise ApiError(404, "NOTIFICATION_NOT_FOUND", "Notification not found.")
    db.delete(notification)
    db.commit()
    return None


@router.delete("/notifications", status_code=204)
def clear_notifications(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    db.query(Notification).filter(Notification.recipient_id == user.id).delete(synchronize_session=False)
    db.commit()
    return None


@router.put("/devices/{device_id}")
def upsert_device(
    device_id: str,
    values: DeviceUpsert,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not device_id or len(device_id) > 128:
        raise ApiError(422, "INVALID_DEVICE_ID", "deviceId must be between 1 and 128 characters.")
    device = db.scalar(
        select(PushDevice).where(PushDevice.user_id == user.id, PushDevice.device_id == device_id)
    )
    if device is None:
        device = PushDevice(
            user_id=user.id,
            device_id=device_id,
            platform=values.platform,
            token=values.token,
            active=True,
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(device)
    else:
        device.platform = values.platform
        device.token = values.token
        device.active = True
        device.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return {"deviceId": device_id, "active": True}


@router.delete("/devices/{device_id}", status_code=204)
def remove_device(
    device_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = db.scalar(
        select(PushDevice).where(PushDevice.user_id == user.id, PushDevice.device_id == device_id)
    )
    if device is not None:
        device.active = False
        db.commit()
    return None
