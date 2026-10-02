import base64
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import (
    Court,
    CourtMembership,
    Event,
    EventGroup,
    GroupMembership,
    Notification,
    PushDelivery,
    PushDevice,
    User,
    Visibility,
    WaitlistEntry,
)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def can_view_event(db: Session, event: Event, user: User) -> bool:
    if event.owner_id == user.id or event.visibility == Visibility.public:
        return True
    return db.scalar(
        select(EventGroup.event_id)
        .join(GroupMembership, GroupMembership.group_id == EventGroup.group_id)
        .where(EventGroup.event_id == event.id, GroupMembership.user_id == user.id)
        .limit(1)
    ) is not None


def require_event(db: Session, event_id: UUID, user: User) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found.")
    if not can_view_event(db, event, user):
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found.")
    return event


def court_payload(db: Session, court: Court, current_user_id: UUID) -> dict:
    count = db.scalar(select(func.count(CourtMembership.id)).where(CourtMembership.court_id == court.id)) or 0
    members = db.execute(
        select(CourtMembership, User)
        .join(User, User.id == CourtMembership.user_id)
        .where(CourtMembership.court_id == court.id)
        .order_by(CourtMembership.created_at, CourtMembership.id)
    ).all()
    wait_count = db.scalar(select(func.count(WaitlistEntry.id)).where(WaitlistEntry.court_id == court.id)) or 0
    my_membership = db.scalar(
        select(CourtMembership).where(
            CourtMembership.court_id == court.id,
            CourtMembership.user_id == current_user_id,
        )
    )
    my_waitlist = db.scalar(
        select(WaitlistEntry).where(
            WaitlistEntry.court_id == court.id,
            WaitlistEntry.user_id == current_user_id,
        )
    )
    return {
        "id": str(court.id),
        "category": court.category.value,
        "type": court.format.value,
        "maxPlayers": court.max_players,
        "currentPlayers": count,
        "openSpots": max(0, court.max_players - count),
        "waitlistCount": wait_count,
        "members": [
            {
                "userId": str(member.user_id),
                "displayName": profile.display_name,
                "sex": profile.sex.value if profile.sex else None,
                "position": profile.position,
                "ownsNet": profile.owns_net,
            }
            for member, profile in members
        ],
        "isMember": my_membership is not None,
        "isWaitlisted": my_waitlist is not None,
    }


def event_payload(db: Session, event: Event, user: User) -> dict:
    courts = db.scalars(select(Court).where(Court.event_id == event.id).order_by(Court.id)).all()
    group_ids = db.scalars(select(EventGroup.group_id).where(EventGroup.event_id == event.id)).all()
    participant_count = db.scalar(
        select(func.count(func.distinct(CourtMembership.user_id))).where(CourtMembership.event_id == event.id)
    ) or 0
    return {
        "id": str(event.id),
        "ownerId": str(event.owner_id),
        "title": event.title,
        "locationName": event.location_name,
        "address": event.address,
        "city": event.city,
        "state": event.state,
        "zip": event.zip,
        "dateTime": as_utc(event.date_time).isoformat().replace("+00:00", "Z"),
        "setting": event.setting.value,
        "netsAvailable": event.nets_available,
        "netsNeeded": event.nets_needed,
        "visibility": event.visibility.value,
        "invitedGroupIds": [str(group_id) for group_id in group_ids] if event.owner_id == user.id else [],
        "allowAddCourts": event.allow_add_courts,
        "status": event.status.value,
        "version": event.version,
        "participantCount": participant_count,
        "courts": [court_payload(db, court, user.id) for court in courts],
        "createdAt": as_utc(event.created_at).isoformat().replace("+00:00", "Z"),
        "updatedAt": as_utc(event.updated_at).isoformat().replace("+00:00", "Z"),
    }


def decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if not cursor:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
        instant, identifier = decoded.split("|", 1)
        return datetime.fromisoformat(instant), UUID(identifier)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "INVALID_CURSOR", "The pagination cursor is invalid.") from exc


def encode_cursor(instant: datetime, identifier: UUID) -> str:
    raw = f"{as_utc(instant).isoformat()}|{identifier}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def create_notification(
    db: Session,
    recipient: User,
    *,
    category: str,
    title: str,
    body: str,
    resource_type: str | None = None,
    resource_id: UUID | None = None,
    push_enabled: bool,
) -> Notification:
    notification = Notification(
        recipient_id=recipient.id,
        category=category,
        title=title,
        body=body,
        related_resource_type=resource_type,
        related_resource_id=str(resource_id) if resource_id else None,
    )
    db.add(notification)
    db.flush()
    if push_enabled:
        devices = db.scalars(
            select(PushDevice).where(PushDevice.user_id == recipient.id, PushDevice.active.is_(True))
        ).all()
        for device in devices:
            db.add(PushDelivery(notification_id=notification.id, device_id=device.id))
    return notification
