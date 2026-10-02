from datetime import datetime
import re
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models import CourtCategory, CourtFormat, Sex, Setting, Visibility


US_STATE_CODES = {
    "AL", "AK", "AS", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FM", "FL", "GA",
    "GU", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MH", "MD", "MA",
    "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND",
    "MP", "OH", "OK", "OR", "PW", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VI", "VA", "WA", "WV", "WI", "WY",
}


def normalize_state(value: str) -> str:
    normalized = value.upper()
    if normalized not in US_STATE_CODES:
        raise ValueError("state must be a supported US state or territory code.")
    return normalized


def validate_zip(value: str) -> str:
    if re.fullmatch(r"\d{5}(?:-\d{4})?", value) is None:
        raise ValueError("zip must be a 5-digit ZIP code or ZIP+4.")
    return value


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, from_attributes=True, use_enum_values=True)


class CourtInput(ApiModel):
    id: UUID | None = None
    category: CourtCategory
    court_format: CourtFormat = Field(alias="type")
    max_players: int = Field(gt=0, alias="maxPlayers")


class EventFields(ApiModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    location_name: str | None = Field(default=None, max_length=160, alias="locationName")
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    city: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    state: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=2)]
    zip: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=10)]
    date_time: datetime = Field(alias="dateTime")
    setting: Setting
    nets_available: int = Field(default=0, ge=0, alias="netsAvailable")
    nets_needed: int = Field(default=0, ge=0, alias="netsNeeded")
    visibility: Visibility = Visibility.public
    allow_add_courts: bool = Field(default=False, alias="allowAddCourts")

    @field_validator("date_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dateTime must include a timezone offset.")
        return value

    @field_validator("state")
    @classmethod
    def uppercase_state(cls, value: str) -> str:
        return normalize_state(value)

    @field_validator("zip")
    @classmethod
    def validate_zip(cls, value: str) -> str:
        return validate_zip(value)


class EventCreate(EventFields):
    invited_group_ids: list[UUID] = Field(default_factory=list, alias="invitedGroupIds")
    courts: list[CourtInput] = Field(default_factory=list)


class EventPatch(ApiModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    location_name: str | None = Field(default=None, max_length=160, alias="locationName")
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)] | None = None
    city: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    state: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=2)] | None = None
    zip: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=10)] | None = None
    date_time: datetime | None = Field(default=None, alias="dateTime")
    setting: Setting | None = None
    nets_available: int | None = Field(default=None, ge=0, alias="netsAvailable")
    nets_needed: int | None = Field(default=None, ge=0, alias="netsNeeded")
    visibility: Visibility | None = None
    allow_add_courts: bool | None = Field(default=None, alias="allowAddCourts")
    invited_group_ids: list[UUID] | None = Field(default=None, alias="invitedGroupIds")
    courts: list[CourtInput] | None = None

    @field_validator("date_time")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("dateTime must include a timezone offset.")
        return value

    @field_validator("state")
    @classmethod
    def uppercase_state(cls, value: str | None) -> str | None:
        return normalize_state(value) if value is not None else None

    @field_validator("zip")
    @classmethod
    def valid_zip(cls, value: str | None) -> str | None:
        return validate_zip(value) if value is not None else None


class ProfilePatch(ApiModel):
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None = Field(default=None, alias="displayName")
    sex: Sex | None = None
    position: str | None = Field(default=None, max_length=80)
    owns_net: bool | None = Field(default=None, alias="ownsNet")


class PreferencesPatch(ApiModel):
    game_updates: bool | None = Field(default=None, alias="gameUpdates")
    game_reminders: bool | None = Field(default=None, alias="gameReminders")
    friend_requests: bool | None = Field(default=None, alias="friendRequests")
    profile_visibility: str | None = Field(default=None, alias="profileVisibility")


class GroupCreate(ApiModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class GroupPatch(ApiModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None


class UserIdBody(ApiModel):
    user_id: UUID = Field(alias="userId")


class FriendRequestCreate(UserIdBody):
    pass


class DeviceUpsert(ApiModel):
    platform: str
    token: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, value: str) -> str:
        value = value.lower()
        if value not in {"ios", "android"}:
            raise ValueError("platform must be ios or android.")
        return value
