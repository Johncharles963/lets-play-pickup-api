from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, verify_supabase_token
from app.database import get_db
from app.errors import ApiError
from app.models import (
    Friendship,
    GroupMembership,
    PickupGroup,
    Sex,
    User,
)
from app.schemas import (
    DeviceUpsert,
    FriendRequestCreate,
    GroupCreate,
    GroupPatch,
    PreferencesPatch,
    ProfilePatch,
    UserIdBody,
)
from app.services import as_utc, create_notification, decode_cursor, encode_cursor


router = APIRouter(prefix="/v1", tags=["profiles, friends, and groups"])


def user_profile(user: User, *, include_preferences: bool = False) -> dict:
    result = {
        "id": str(user.id),
        "displayName": user.display_name,
        "sex": user.sex.value if user.sex else None,
        "position": user.position,
        "ownsNet": user.owns_net,
        "profileVisibility": user.profile_visibility,
        "profileComplete": bool(user.display_name and user.display_name.strip() and user.sex),
    }
    if include_preferences:
        result["preferences"] = {
            "gameUpdates": user.game_updates,
            "gameReminders": user.game_reminders,
            "friendRequests": user.friend_requests,
            "profileVisibility": user.profile_visibility,
        }
    return result


def get_group(db: Session, group_id: UUID, user: User) -> PickupGroup:
    group = db.get(PickupGroup, group_id)
    if group is None:
        raise ApiError(404, "GROUP_NOT_FOUND", "Group not found.")
    member = db.scalar(
        select(GroupMembership.id).where(
            GroupMembership.group_id == group_id,
            GroupMembership.user_id == user.id,
        )
    )
    if member is None:
        raise ApiError(404, "GROUP_NOT_FOUND", "Group not found.")
    return group


def group_payload(
    db: Session,
    group: PickupGroup,
    user: User,
    *,
    cursor: UUID | None = None,
    limit: int = 30,
) -> dict:
    member_count = db.scalar(
        select(func.count(GroupMembership.id)).where(GroupMembership.group_id == group.id)
    ) or 0
    member_query = (
        select(User)
        .join(GroupMembership, GroupMembership.user_id == User.id)
        .where(GroupMembership.group_id == group.id)
    )
    if cursor:
        member_query = member_query.where(User.id > cursor)
    members = db.scalars(member_query.order_by(User.id).limit(limit + 1)).all()
    has_more = len(members) > limit
    members = members[:limit]
    return {
        "id": str(group.id),
        "name": group.name,
        "ownerId": str(group.owner_id),
        "members": [user_profile(member) for member in members],
        "nextMemberCursor": str(members[-1].id) if has_more and members else None,
        "memberCount": member_count,
        "createdAt": as_utc(group.created_at).isoformat().replace("+00:00", "Z"),
        "isOwner": group.owner_id == user.id,
    }


@router.post("/auth/session")
def exchange_auth_session(
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "AUTHENTICATION_REQUIRED", "A bearer access token is required.")
    token = authorization[7:].strip()
    claims = verify_supabase_token(token)
    user = db.scalar(select(User).where(User.auth_subject == claims["sub"]))
    if user is None:
        metadata = claims.get("user_metadata") or {}
        display_name = metadata.get("display_name") or metadata.get("full_name")
        if display_name is not None and not isinstance(display_name, str):
            display_name = None
        user = User(auth_subject=claims["sub"], display_name=display_name)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            user = db.scalar(select(User).where(User.auth_subject == claims["sub"]))
            if user is None:
                raise
    return {"user": user_profile(user, include_preferences=True)}


@router.get("/me")
def get_me(user: User = Depends(get_current_user)):
    return {"user": user_profile(user, include_preferences=True)}


@router.patch("/me/profile")
def update_profile(
    values: ProfilePatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    changes = values.model_dump(exclude_unset=True)
    if changes.get("owns_net", False) is None:
        raise ApiError(422, "NULL_NOT_ALLOWED", "ownsNet cannot be null.")
    for key, value in changes.items():
        if key == "sex" and value is not None:
            value = Sex(value)
        setattr(user, key, value)
    db.commit()
    db.refresh(user)
    return {"user": user_profile(user, include_preferences=True)}


@router.patch("/me/preferences")
def update_preferences(
    values: PreferencesPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    changes = values.model_dump(exclude_unset=True)
    if "profile_visibility" in changes and changes["profile_visibility"] not in {"public", "private"}:
        raise ApiError(422, "INVALID_PROFILE_VISIBILITY", "profileVisibility must be public or private.")
    if any(value is None for value in changes.values()):
        raise ApiError(422, "NULL_NOT_ALLOWED", "Preference values cannot be null.")
    for key, value in changes.items():
        setattr(user, key, value)
    db.commit()
    db.refresh(user)
    return {"user": user_profile(user, include_preferences=True)}


@router.get("/users")
def search_users(
    q: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(User).where(User.id != user.id)
    if q and q.strip():
        query = query.where(User.display_name.ilike(f"%{q.strip()}%"))
    if cursor:
        try:
            cursor_id = UUID(cursor)
        except ValueError as exc:
            raise ApiError(400, "INVALID_CURSOR", "The pagination cursor is invalid.") from exc
        query = query.where(User.id > cursor_id)
    users = db.scalars(query.order_by(User.id).limit(limit + 1)).all()
    has_more = len(users) > limit
    users = users[:limit]
    return {
        "items": [user_profile(profile) for profile in users],
        "nextCursor": str(users[-1].id) if has_more and users else None,
    }


@router.get("/users/{user_id}")
def get_user_profile(
    user_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    profile = db.get(User, user_id)
    if profile is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    return {"user": user_profile(profile)}


@router.get("/me/groups")
def list_my_groups(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = (
        select(PickupGroup)
        .join(GroupMembership, GroupMembership.group_id == PickupGroup.id)
        .where(GroupMembership.user_id == user.id)
        .order_by(PickupGroup.id)
    )
    if cursor:
        try:
            query = query.where(PickupGroup.id > UUID(cursor))
        except ValueError as exc:
            raise ApiError(400, "INVALID_CURSOR", "The pagination cursor is invalid.") from exc
    groups = db.scalars(query.limit(limit + 1)).all()
    has_more = len(groups) > limit
    groups = groups[:limit]
    return {
        "items": [group_payload(db, group, user) for group in groups],
        "nextCursor": str(groups[-1].id) if has_more and groups else None,
    }


@router.post("/groups", status_code=201)
def create_group(
    values: GroupCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    group = PickupGroup(name=values.name, owner_id=user.id)
    db.add(group)
    db.flush()
    db.add(GroupMembership(group_id=group.id, user_id=user.id))
    db.commit()
    db.refresh(group)
    return group_payload(db, group, user)


@router.get("/groups/{group_id}")
def get_group_details(
    group_id: UUID,
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cursor_id = None
    if cursor:
        try:
            cursor_id = UUID(cursor)
        except ValueError as exc:
            raise ApiError(400, "INVALID_CURSOR", "The pagination cursor is invalid.") from exc
    return group_payload(db, get_group(db, group_id, user), user, cursor=cursor_id, limit=limit)


@router.get("/groups/{group_id}/members")
def list_group_members(
    group_id: UUID,
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    group = get_group(db, group_id, user)
    cursor_id = None
    if cursor:
        try:
            cursor_id = UUID(cursor)
        except ValueError as exc:
            raise ApiError(400, "INVALID_CURSOR", "The pagination cursor is invalid.") from exc
    details = group_payload(db, group, user, cursor=cursor_id, limit=limit)
    return {
        "items": details["members"],
        "nextCursor": details["nextMemberCursor"],
        "memberCount": details["memberCount"],
    }


@router.patch("/groups/{group_id}")
def update_group(
    group_id: UUID,
    values: GroupPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    group = get_group(db, group_id, user)
    if group.owner_id != user.id:
        raise ApiError(403, "GROUP_OWNER_REQUIRED", "Only the group owner can update this group.")
    if values.name is not None:
        group.name = values.name
    db.commit()
    db.refresh(group)
    return group_payload(db, group, user)


@router.delete("/groups/{group_id}", status_code=204)
def delete_group(
    group_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    group = get_group(db, group_id, user)
    if group.owner_id != user.id:
        raise ApiError(403, "GROUP_OWNER_REQUIRED", "Only the group owner can delete this group.")
    db.delete(group)
    db.commit()
    return None


@router.post("/groups/{group_id}/members", status_code=201)
def add_group_member(
    group_id: UUID,
    values: UserIdBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    group = get_group(db, group_id, user)
    if group.owner_id != user.id:
        raise ApiError(403, "GROUP_OWNER_REQUIRED", "Only the group owner can add members.")
    if db.get(User, values.user_id) is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    existing = db.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == group.id,
            GroupMembership.user_id == values.user_id,
        )
    )
    if existing is None:
        db.add(GroupMembership(group_id=group.id, user_id=values.user_id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(GroupMembership).where(
                    GroupMembership.group_id == group.id,
                    GroupMembership.user_id == values.user_id,
                )
            )
            if existing is None:
                raise ApiError(409, "GROUP_MEMBERSHIP_CONFLICT", "Group membership changed; refresh and retry.")
    return group_payload(db, group, user)


@router.delete("/groups/{group_id}/members")
def remove_group_member(
    group_id: UUID,
    user_id: UUID | None = Query(default=None, alias="userId"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    group = get_group(db, group_id, user)
    target_id = user_id or user.id
    if target_id != user.id and group.owner_id != user.id:
        raise ApiError(403, "GROUP_OWNER_REQUIRED", "Only the group owner may remove another member.")
    if target_id == group.owner_id:
        raise ApiError(409, "GROUP_OWNER_CANNOT_LEAVE", "The group owner must delete the group or transfer ownership.")
    membership = db.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == group.id,
            GroupMembership.user_id == target_id,
        )
    )
    if membership:
        db.delete(membership)
        db.commit()
    return group_payload(db, group, user)


def canonical_pair(left: UUID, right: UUID) -> tuple[UUID, UUID]:
    return (left, right) if left.int < right.int else (right, left)


def friendship_payload(friendship: Friendship, user_id: UUID) -> dict:
    return {
        "id": str(friendship.id),
        "userId": str(friendship.requester_id if friendship.recipient_id == user_id else friendship.recipient_id),
        "requesterId": str(friendship.requester_id),
        "recipientId": str(friendship.recipient_id),
        "status": friendship.status,
        "createdAt": as_utc(friendship.created_at).isoformat().replace("+00:00", "Z"),
    }


@router.get("/me/friends")
def list_friends(
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = (
        select(Friendship)
        .where(or_(Friendship.requester_id == user.id, Friendship.recipient_id == user.id))
        .order_by(Friendship.created_at.desc(), Friendship.id)
    )
    parsed_cursor = decode_cursor(cursor)
    if parsed_cursor:
        instant, identifier = parsed_cursor
        query = query.where(
            (Friendship.created_at < as_utc(instant))
            | ((Friendship.created_at == as_utc(instant)) & (Friendship.id < identifier))
        )
    friendships = db.scalars(query.limit(limit + 1)).all()
    has_more = len(friendships) > limit
    friendships = friendships[:limit]
    return {
        "items": [friendship_payload(friendship, user.id) for friendship in friendships],
        "nextCursor": encode_cursor(friendships[-1].created_at, friendships[-1].id)
        if has_more and friendships
        else None,
    }


@router.post("/me/friends/requests", status_code=201)
def create_friend_request(
    values: FriendRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    target = db.get(User, values.user_id)
    if target is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    if target.id == user.id:
        raise ApiError(422, "SELF_FRIEND_REQUEST", "You cannot send a friend request to yourself.")
    low, high = canonical_pair(user.id, target.id)
    existing = db.scalar(
        select(Friendship).where(Friendship.user_low_id == low, Friendship.user_high_id == high)
    )
    if existing:
        if existing.status == "pending" and existing.requester_id == user.id:
            return friendship_payload(existing, user.id)
        raise ApiError(409, "FRIEND_REQUEST_EXISTS", "A request or friendship already exists for these users.")
    friendship = Friendship(
        user_low_id=low,
        user_high_id=high,
        requester_id=user.id,
        recipient_id=target.id,
        status="pending",
    )
    db.add(friendship)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(Friendship).where(Friendship.user_low_id == low, Friendship.user_high_id == high)
        )
        if existing and existing.status == "pending" and existing.requester_id == user.id:
            return friendship_payload(existing, user.id)
        raise ApiError(409, "FRIEND_REQUEST_EXISTS", "A request or friendship already exists for these users.")
    create_notification(
        db,
        target,
        category="friend_request",
        title="New friend request",
        body=f"{user.display_name or 'A player'} sent you a friend request.",
        resource_type="friendship",
        resource_id=friendship.id,
        push_enabled=target.friend_requests,
    )
    db.commit()
    db.refresh(friendship)
    return friendship_payload(friendship, user.id)


@router.post("/me/friends/requests/{request_id}/accept")
def accept_friend_request(
    request_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    friendship = db.get(Friendship, request_id)
    if friendship is None or friendship.recipient_id != user.id:
        raise ApiError(404, "FRIEND_REQUEST_NOT_FOUND", "Friend request not found.")
    if friendship.status == "pending":
        friendship.status = "accepted"
        db.commit()
        db.refresh(friendship)
    return friendship_payload(friendship, user.id)


@router.delete("/me/friends/{relationship_id}")
def delete_friendship(
    relationship_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    friendship = db.get(Friendship, relationship_id)
    if friendship is None or user.id not in {friendship.requester_id, friendship.recipient_id}:
        raise ApiError(404, "FRIENDSHIP_NOT_FOUND", "Friend relationship not found.")
    db.delete(friendship)
    db.commit()
    return {"deleted": True}
