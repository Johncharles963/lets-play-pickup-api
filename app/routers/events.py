from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.errors import ApiError
from app.models import (
    Court,
    CourtCategory,
    CourtFormat,
    CourtMembership,
    Event,
    EventGroup,
    EventStatus,
    GroupMembership,
    PickupGroup,
    Setting,
    User,
    Visibility,
    WaitlistEntry,
)
from app.schemas import CourtInput, EventCreate, EventPatch
from app.services import (
    as_utc,
    can_view_event,
    create_notification,
    decode_cursor,
    encode_cursor,
    event_payload,
    require_event,
)


router = APIRouter(prefix="/v1", tags=["events"])


def event_query_access(user: User):
    invited = exists(
        select(EventGroup.event_id)
        .join(GroupMembership, GroupMembership.group_id == EventGroup.group_id)
        .where(EventGroup.event_id == Event.id, GroupMembership.user_id == user.id)
    )
    return or_(Event.visibility == Visibility.public, Event.owner_id == user.id, invited)


def court_from_input(event_id: UUID, values: CourtInput) -> Court:
    return Court(
        event_id=event_id,
        category=CourtCategory(values.category),
        format=CourtFormat(values.court_format),
        max_players=values.max_players,
    )


def validate_invited_groups(db: Session, user: User, group_ids: list[UUID]) -> list[PickupGroup]:
    unique_ids = set(group_ids)
    if len(unique_ids) != len(group_ids):
        raise ApiError(422, "DUPLICATE_GROUP", "An invited group may be listed only once.")
    if not unique_ids:
        return []
    groups = db.scalars(
        select(PickupGroup).where(
            PickupGroup.id.in_(unique_ids),
            PickupGroup.owner_id == user.id,
        )
    ).all()
    if len(groups) != len(unique_ids):
        raise ApiError(422, "INVALID_INVITED_GROUP", "You may invite only groups that you own.")
    return groups


def validate_patch_values(changes: dict) -> None:
    nullable = {"location_name"}
    for key, value in changes.items():
        if value is None and key not in nullable:
            raise ApiError(422, "NULL_NOT_ALLOWED", f"{key} cannot be null.")


def add_update_notifications(db: Session, event: Event, title: str, body: str) -> None:
    recipient_ids = set(
        db.scalars(select(CourtMembership.user_id).where(CourtMembership.event_id == event.id)).all()
    )
    recipient_ids.update(
        db.scalars(select(WaitlistEntry.user_id).where(WaitlistEntry.event_id == event.id)).all()
    )
    recipients = db.scalars(select(User).where(User.id.in_(recipient_ids))).all() if recipient_ids else []
    for recipient in recipients:
        create_notification(
            db,
            recipient,
            category="game_update",
            title=title,
            body=body,
            resource_type="event",
            resource_id=event.id,
            push_enabled=recipient.game_updates,
        )


def apply_court_configuration(db: Session, event: Event, configurations: list[CourtInput]) -> None:
    current_courts = db.scalars(select(Court).where(Court.event_id == event.id)).all()
    by_id = {court.id: court for court in current_courts}
    retained: set[UUID] = set()
    requested_ids = [config.id for config in configurations if config.id is not None]
    if len(requested_ids) != len(set(requested_ids)):
        raise ApiError(422, "DUPLICATE_COURT", "A court may appear only once in the event configuration.")
    for config in configurations:
        if config.id is None:
            db.add(court_from_input(event.id, config))
            continue
        court = by_id.get(config.id)
        if court is None:
            raise ApiError(422, "INVALID_COURT", "A configured court does not belong to this event.")
        retained.add(court.id)
        occupied = db.scalar(
            select(func.count(CourtMembership.id)).where(CourtMembership.court_id == court.id)
        ) or 0
        waiting = db.scalar(
            select(func.count(WaitlistEntry.id)).where(WaitlistEntry.court_id == court.id)
        ) or 0
        changed_setup = config.category != court.category or config.court_format != court.format
        if (occupied or waiting) and changed_setup:
            raise ApiError(
                409,
                "COURT_OCCUPIED",
                "A court with players or waitlisted users cannot change category or format.",
                {"courtId": str(court.id)},
            )
        if config.max_players < occupied:
            raise ApiError(
                409,
                "CAPACITY_BELOW_MEMBERS",
                "Court capacity cannot be lower than its current player count.",
                {"courtId": str(court.id), "currentPlayers": occupied},
            )
        court.category = CourtCategory(config.category)
        court.format = CourtFormat(config.court_format)
        court.max_players = config.max_players
    for court in current_courts:
        if court.id in retained:
            continue
        occupied = db.scalar(
            select(func.count(CourtMembership.id)).where(CourtMembership.court_id == court.id)
        ) or 0
        waiting = db.scalar(
            select(func.count(WaitlistEntry.id)).where(WaitlistEntry.court_id == court.id)
        ) or 0
        if occupied or waiting:
            raise ApiError(
                409,
                "COURT_OCCUPIED",
                "A court with players or waitlisted users cannot be removed.",
                {"courtId": str(court.id)},
            )
        db.delete(court)


def validate_court_eligibility(court: Court, user: User) -> None:
    if court.category in {CourtCategory.mens, CourtCategory.womens} and user.sex is None:
        raise ApiError(409, "PROFILE_INCOMPLETE", "Select Male or Female before joining this court.")
    if court.category == CourtCategory.mens and user.sex is not None and user.sex.value != "M":
        raise ApiError(409, "COURT_INELIGIBLE", "Only male players may join a Men's court.")
    if court.category == CourtCategory.womens and user.sex is not None and user.sex.value != "F":
        raise ApiError(409, "COURT_INELIGIBLE", "Only female players may join a Women's court.")


def lock_event(db: Session, event_id: UUID, user: User) -> Event:
    event = db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if event is None or not can_view_event(db, event, user):
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found.")
    return event


def get_event_court(db: Session, event_id: UUID, court_id: UUID) -> Court:
    court = db.scalar(select(Court).where(Court.id == court_id, Court.event_id == event_id))
    if court is None:
        raise ApiError(404, "COURT_NOT_FOUND", "Court not found.")
    return court


@router.get("/events")
def discover_events(
    q: str | None = None,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    setting: Setting | None = None,
    has_open_spots: bool | None = Query(default=None, alias="hasOpenSpots"),
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Event).where(
        Event.status == EventStatus.active,
        Event.date_time >= datetime.now(timezone.utc),
        event_query_access(user),
    )
    if q:
        term = f"%{q.strip()}%"
        query = query.where(
            or_(
                Event.title.ilike(term),
                Event.location_name.ilike(term),
                Event.address.ilike(term),
                Event.city.ilike(term),
                Event.state.ilike(term),
                Event.zip.ilike(term),
            )
        )
    if from_time:
        if from_time.tzinfo is None or from_time.utcoffset() is None:
            raise ApiError(422, "TIMEZONE_REQUIRED", "from must include a timezone offset.")
        query = query.where(Event.date_time >= as_utc(from_time))
    if to_time:
        if to_time.tzinfo is None or to_time.utcoffset() is None:
            raise ApiError(422, "TIMEZONE_REQUIRED", "to must include a timezone offset.")
        query = query.where(Event.date_time <= as_utc(to_time))
    if from_time and to_time and as_utc(from_time) > as_utc(to_time):
        raise ApiError(422, "INVALID_TIME_RANGE", "from must be earlier than or equal to to.")
    if setting:
        query = query.where(Event.setting == setting)
    if has_open_spots is not None:
        current_count = (
            select(func.count(CourtMembership.id))
            .where(CourtMembership.court_id == Court.id)
            .correlate(Court)
            .scalar_subquery()
        )
        has_spots = exists(
            select(Court.id).where(Court.event_id == Event.id, Court.max_players > current_count)
        )
        query = query.where(has_spots if has_open_spots else ~has_spots)
    parsed_cursor = decode_cursor(cursor)
    if parsed_cursor:
        instant, identifier = parsed_cursor
        query = query.where(
            or_(
                Event.date_time > as_utc(instant),
                and_(Event.date_time == as_utc(instant), Event.id > identifier),
            )
        )
    events = db.scalars(query.order_by(Event.date_time, Event.id).limit(limit + 1)).all()
    has_more = len(events) > limit
    events = events[:limit]
    return {
        "items": [event_payload(db, event, user) for event in events],
        "nextCursor": encode_cursor(events[-1].date_time, events[-1].id) if has_more and events else None,
    }


@router.get("/me/events")
def my_events(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = (
        select(Event)
        .join(CourtMembership, CourtMembership.event_id == Event.id)
        .where(CourtMembership.user_id == user.id)
        .where(event_query_access(user))
        .distinct()
        .order_by(Event.date_time, Event.id)
    )
    parsed_cursor = decode_cursor(cursor)
    if parsed_cursor:
        instant, identifier = parsed_cursor
        query = query.where(
            or_(
                Event.date_time > as_utc(instant),
                and_(Event.date_time == as_utc(instant), Event.id > identifier),
            )
        )
    events = db.scalars(query.limit(limit + 1)).all()
    has_more = len(events) > limit
    events = events[:limit]
    return {
        "items": [event_payload(db, event, user) for event in events],
        "nextCursor": encode_cursor(events[-1].date_time, events[-1].id) if has_more and events else None,
    }


@router.post("/events", status_code=201)
def create_event(
    values: EventCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if as_utc(values.date_time) <= datetime.now(timezone.utc):
        raise ApiError(422, "EVENT_TIME_NOT_FUTURE", "Event start time must be in the future.")
    if values.visibility == Visibility.private and not values.invited_group_ids:
        raise ApiError(422, "INVITATION_REQUIRED", "A private event must invite at least one group.")
    groups = validate_invited_groups(db, user, values.invited_group_ids)
    if any(court.id is not None for court in values.courts):
        raise ApiError(422, "COURT_ID_NOT_ALLOWED", "Court IDs are assigned by the server.")
    event = Event(
        owner_id=user.id,
        title=values.title,
        location_name=values.location_name,
        address=values.address,
        city=values.city,
        state=values.state,
        zip=values.zip,
        date_time=as_utc(values.date_time),
        setting=values.setting,
        nets_available=values.nets_available,
        nets_needed=values.nets_needed,
        visibility=values.visibility,
        allow_add_courts=values.allow_add_courts,
    )
    db.add(event)
    db.flush()
    for court in values.courts:
        db.add(court_from_input(event.id, court))
    for group in groups:
        db.add(EventGroup(event_id=event.id, group_id=group.id))
    db.commit()
    db.refresh(event)
    return event_payload(db, event, user)


@router.get("/events/{event_id}")
def get_event(
    event_id: UUID,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = require_event(db, event_id, user)
    response.headers["ETag"] = f'"{event.version}"'
    return event_payload(db, event, user)


@router.patch("/events/{event_id}")
def update_event(
    event_id: UUID,
    values: EventPatch,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = lock_event(db, event_id, user)
    if event.owner_id != user.id:
        raise ApiError(403, "EVENT_OWNER_REQUIRED", "Only the event owner can perform this action.")
    if event.status != EventStatus.active:
        raise ApiError(409, "EVENT_CANCELLED", "Cancelled events cannot be edited.")
    if if_match is None:
        raise ApiError(428, "VERSION_REQUIRED", "Send the current event version in the If-Match header.")
    try:
        expected_version = int(if_match.strip().removeprefix("W/").strip('"'))
    except ValueError as exc:
        raise ApiError(400, "INVALID_VERSION", "If-Match must contain the numeric event version.") from exc
    if expected_version != event.version:
        raise ApiError(
            409,
            "EVENT_VERSION_CONFLICT",
            "The event changed since it was loaded. Refresh before editing.",
            {"currentVersion": event.version},
        )

    changes = values.model_dump(exclude_unset=True)
    invited_ids = changes.pop("invited_group_ids", None)
    court_changes = changes.pop("courts", None)
    if court_changes is not None:
        court_changes = [CourtInput.model_validate(court) for court in court_changes]
    validate_patch_values(changes)
    time_or_location_changed = False
    for key in ("date_time", "location_name", "address", "city", "state", "zip"):
        if key not in changes:
            continue
        new_value = changes[key]
        old_value = getattr(event, key)
        if key == "date_time" and new_value is not None:
            new_value = as_utc(new_value)
            old_value = as_utc(old_value)
            changes[key] = new_value
        if new_value != old_value:
            time_or_location_changed = True
    for key, value in changes.items():
        if key == "setting" and value is not None:
            value = Setting(value)
        elif key == "visibility" and value is not None:
            value = Visibility(value)
        setattr(event, key, value)
    if invited_ids is not None:
        groups = validate_invited_groups(db, user, invited_ids)
        db.query(EventGroup).filter(EventGroup.event_id == event.id).delete(synchronize_session=False)
        for group in groups:
            db.add(EventGroup(event_id=event.id, group_id=group.id))
    group_count = db.scalar(select(func.count(EventGroup.group_id)).where(EventGroup.event_id == event.id)) or 0
    if event.visibility == Visibility.private and (invited_ids == [] or (invited_ids is None and group_count == 0)):
        raise ApiError(422, "INVITATION_REQUIRED", "A private event must invite at least one group.")
    if court_changes is not None:
        apply_court_configuration(db, event, court_changes)
    event.version += 1
    if time_or_location_changed:
        add_update_notifications(
            db,
            event,
            "Game details changed",
            f"{event.title} has updated time or location details.",
        )
    db.commit()
    db.refresh(event)
    response.headers["ETag"] = f'"{event.version}"'
    return event_payload(db, event, user)


@router.post("/events/{event_id}/cancel")
def cancel_event(
    event_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = lock_event(db, event_id, user)
    if event.owner_id != user.id:
        raise ApiError(403, "EVENT_OWNER_REQUIRED", "Only the event owner can perform this action.")
    if event.status == EventStatus.cancelled:
        return event_payload(db, event, user)
    add_update_notifications(db, event, "Game cancelled", f"{event.title} has been cancelled.")
    db.query(WaitlistEntry).filter(WaitlistEntry.event_id == event.id).delete(synchronize_session=False)
    event.status = EventStatus.cancelled
    event.version += 1
    db.commit()
    db.refresh(event)
    return event_payload(db, event, user)


@router.post("/events/{event_id}/courts", status_code=201)
def add_court(
    event_id: UUID,
    values: CourtInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = lock_event(db, event_id, user)
    if event.status != EventStatus.active:
        raise ApiError(409, "EVENT_CANCELLED", "Courts cannot be added to a cancelled event.")
    if event.owner_id != user.id and not event.allow_add_courts:
        raise ApiError(403, "COURT_ADD_NOT_ALLOWED", "The event owner does not allow participants to add courts.")
    if values.id is not None:
        raise ApiError(422, "COURT_ID_NOT_ALLOWED", "Court IDs are assigned by the server.")
    court = court_from_input(event.id, values)
    db.add(court)
    event.version += 1
    db.commit()
    db.refresh(court)
    return {
        "id": str(court.id),
        "category": court.category.value,
        "type": court.format.value,
        "maxPlayers": court.max_players,
        "currentPlayers": 0,
        "openSpots": court.max_players,
        "waitlistCount": 0,
        "members": [],
        "isMember": False,
        "isWaitlisted": False,
    }


@router.post("/events/{event_id}/courts/{court_id}/membership")
def join_court(
    event_id: UUID,
    court_id: UUID,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = lock_event(db, event_id, user)
    if event.status != EventStatus.active:
        raise ApiError(409, "EVENT_CANCELLED", "Cancelled events do not accept RSVPs.")
    court = get_event_court(db, event.id, court_id)
    validate_court_eligibility(court, user)
    existing = db.scalar(
        select(CourtMembership).where(CourtMembership.event_id == event.id, CourtMembership.user_id == user.id)
    )
    if existing:
        if existing.court_id != court.id:
            raise ApiError(409, "ALREADY_JOINED_EVENT", "You have already joined another court in this event.")
        return {"event": event_payload(db, event, user), "membership": {"courtId": str(court.id)}, "promotedUserId": None}
    waiting = db.scalar(
        select(WaitlistEntry).where(WaitlistEntry.event_id == event.id, WaitlistEntry.user_id == user.id)
    )
    count = db.scalar(select(func.count(CourtMembership.id)).where(CourtMembership.court_id == court.id)) or 0
    if count >= court.max_players:
        raise ApiError(409, "COURT_FULL", "This court no longer has open spots.")
    membership = CourtMembership(event_id=event.id, court_id=court.id, user_id=user.id)
    db.add(membership)
    if waiting:
        db.delete(waiting)
    db.commit()
    db.refresh(event)
    response.headers["ETag"] = f'"{event.version}"'
    return {"event": event_payload(db, event, user), "membership": {"courtId": str(court.id)}, "promotedUserId": None}


@router.delete("/events/{event_id}/courts/{court_id}/membership")
def leave_court(
    event_id: UUID,
    court_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = lock_event(db, event_id, user)
    court = get_event_court(db, event.id, court_id)
    membership = db.scalar(
        select(CourtMembership).where(
            CourtMembership.event_id == event.id,
            CourtMembership.court_id == court.id,
            CourtMembership.user_id == user.id,
        )
    )
    if membership is None:
        return {"event": event_payload(db, event, user), "left": False, "promotedUserId": None}
    db.delete(membership)
    promoted_id: UUID | None = None
    if event.status == EventStatus.active and as_utc(event.date_time) > datetime.now(timezone.utc):
        queue = db.scalars(
            select(WaitlistEntry)
            .where(WaitlistEntry.court_id == court.id)
            .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
            .with_for_update()
        ).all()
        for entry in queue:
            candidate = db.get(User, entry.user_id)
            eligible = candidate is not None and can_view_event(db, event, candidate)
            if eligible:
                try:
                    validate_court_eligibility(court, candidate)
                except ApiError:
                    eligible = False
            already_joined = db.scalar(
                select(CourtMembership.id).where(
                    CourtMembership.event_id == event.id,
                    CourtMembership.user_id == entry.user_id,
                )
            )
            if not eligible or already_joined:
                db.delete(entry)
                continue
            promoted_id = entry.user_id
            db.add(CourtMembership(event_id=event.id, court_id=court.id, user_id=entry.user_id))
            db.delete(entry)
            break
    db.commit()
    db.refresh(event)
    return {
        "event": event_payload(db, event, user),
        "left": True,
        "promotedUserId": str(promoted_id) if promoted_id else None,
    }


@router.post("/events/{event_id}/courts/{court_id}/waitlist", status_code=201)
def join_waitlist(
    event_id: UUID,
    court_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = lock_event(db, event_id, user)
    if event.status != EventStatus.active:
        raise ApiError(409, "EVENT_CANCELLED", "Cancelled events do not accept waitlist entries.")
    court = get_event_court(db, event.id, court_id)
    validate_court_eligibility(court, user)
    membership = db.scalar(
        select(CourtMembership).where(CourtMembership.event_id == event.id, CourtMembership.user_id == user.id)
    )
    if membership:
        raise ApiError(409, "ALREADY_JOINED_EVENT", "You have already joined a court in this event.")
    existing = db.scalar(
        select(WaitlistEntry).where(WaitlistEntry.event_id == event.id, WaitlistEntry.user_id == user.id)
    )
    if existing:
        if existing.court_id != court.id:
            raise ApiError(409, "ALREADY_WAITLISTED_EVENT", "You are already waitlisted for another court in this event.")
        return {"event": event_payload(db, event, user), "waitlistEntryId": str(existing.id)}
    count = db.scalar(select(func.count(CourtMembership.id)).where(CourtMembership.court_id == court.id)) or 0
    if count < court.max_players:
        raise ApiError(409, "COURT_HAS_OPEN_SPOTS", "Join the court directly while it has open spots.")
    entry = WaitlistEntry(event_id=event.id, court_id=court.id, user_id=user.id)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"event": event_payload(db, event, user), "waitlistEntryId": str(entry.id)}


@router.delete("/events/{event_id}/waitlist")
def leave_waitlist(
    event_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = lock_event(db, event_id, user)
    entry = db.scalar(
        select(WaitlistEntry).where(WaitlistEntry.event_id == event.id, WaitlistEntry.user_id == user.id)
    )
    if entry is None:
        return {"event": event_payload(db, event, user), "left": False}
    db.delete(entry)
    db.commit()
    return {"event": event_payload(db, event, user), "left": True}
