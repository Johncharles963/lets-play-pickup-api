import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Sex(str, enum.Enum):
    M = "M"
    F = "F"


class Setting(str, enum.Enum):
    sand = "sand"
    grass = "grass"
    indoor = "indoor"


class Visibility(str, enum.Enum):
    public = "public"
    private = "private"


class EventStatus(str, enum.Enum):
    active = "active"
    cancelled = "cancelled"


class CourtCategory(str, enum.Enum):
    rco = "RCO"
    co_ed = "Co-ed"
    mens = "Men's"
    womens = "Women's"


class CourtFormat(str, enum.Enum):
    doubles = "Doubles"
    fours = "Fours"
    sixes = "Sixes"


def enum_column(enum_type: type[enum.Enum]) -> Enum:
    return Enum(enum_type, values_callable=lambda values: [member.value for member in values])


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    auth_subject: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(80))
    sex: Mapped[Sex | None] = mapped_column(enum_column(Sex), nullable=True)
    position: Mapped[str | None] = mapped_column(String(80))
    owns_net: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    profile_visibility: Mapped[str] = mapped_column(String(16), default="public", nullable=False)
    game_updates: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    game_reminders: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    friend_requests: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (CheckConstraint("nets_available >= 0"), CheckConstraint("nets_needed >= 0"))

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    location_name: Mapped[str | None] = mapped_column(String(160))
    address: Mapped[str] = mapped_column(String(240), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(2), nullable=False)
    zip: Mapped[str] = mapped_column(String(10), nullable=False)
    date_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    setting: Mapped[Setting] = mapped_column(enum_column(Setting), nullable=False)
    nets_available: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    nets_needed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    visibility: Mapped[Visibility] = mapped_column(enum_column(Visibility), default=Visibility.public, nullable=False)
    allow_add_courts: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[EventStatus] = mapped_column(enum_column(EventStatus), default=EventStatus.active, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    courts: Mapped[list["Court"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    invitations: Mapped[list["EventGroup"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class Court(Base):
    __tablename__ = "courts"
    __table_args__ = (CheckConstraint("max_players > 0"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    category: Mapped[CourtCategory] = mapped_column(enum_column(CourtCategory), nullable=False)
    format: Mapped[CourtFormat] = mapped_column("type", enum_column(CourtFormat), nullable=False)
    max_players: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[Event] = relationship(back_populates="courts")


class CourtMembership(Base):
    __tablename__ = "court_memberships"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_membership_event_user"),
        UniqueConstraint("court_id", "user_id", name="uq_membership_court_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    court_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WaitlistEntry(Base):
    __tablename__ = "court_waitlist_entries"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_waitlist_event_user"),
        Index("ix_waitlist_court_created_id", "court_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    court_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PickupGroup(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GroupMembership(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_member"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventGroup(Base):
    __tablename__ = "event_groups"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event: Mapped[Event] = relationship(back_populates="invitations")


class Friendship(Base):
    __tablename__ = "friendships"
    __table_args__ = (
        UniqueConstraint("user_low_id", "user_high_id", name="uq_friend_pair"),
        CheckConstraint("user_low_id <> user_high_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_low_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    user_high_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    requester_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    recipient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_recipient_created", "recipient_id", "created_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recipient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    related_resource_type: Mapped[str | None] = mapped_column(String(32))
    related_resource_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushDevice(Base):
    __tablename__ = "push_devices"
    __table_args__ = (UniqueConstraint("user_id", "device_id", name="uq_push_device_user_device"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushDelivery(Base):
    __tablename__ = "push_deliveries"
    __table_args__ = (
        UniqueConstraint("notification_id", "device_id", name="uq_push_delivery_notification_device"),
        Index("ix_push_delivery_pending", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    notification_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notifications.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("push_devices.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(80))
    ticket_id: Mapped[str | None] = mapped_column(String(128))
