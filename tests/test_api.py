from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user
from app.database import Base, get_db
from app.main import app
from app.models import User


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    owner_id = uuid4()
    app.state.test_sessionmaker = TestingSession
    test_db = TestingSession()
    with TestingSession() as db:
        owner = User(auth_subject=f"supabase-{owner_id}", display_name="Host", sex="M")
        db.add(owner)
        db.commit()
        app.state.actor_id = owner.id

    def override_db():
        yield test_db
        if test_db.in_transaction():
            test_db.rollback()

    def request_user(db=Depends(override_db)):
        return db.get(User, UUID(str(app.state.actor_id)))

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = request_user
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    test_db.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def create_user(client: TestClient, name: str, sex: str | None = "M") -> str:
    with app.state.test_sessionmaker() as db:
        user = User(auth_subject=f"supabase-{uuid4()}", display_name=name, sex=sex)
        db.add(user)
        db.commit()
        return str(user.id)


def event_body(*, visibility: str = "public", invited_group_ids: list[str] | None = None, courts: list[dict] | None = None):
    return {
        "title": "Sunday pickup",
        "locationName": "North court",
        "address": "100 Main St",
        "city": "Austin",
        "state": "TX",
        "zip": "78701",
        "dateTime": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        "setting": "sand",
        "netsAvailable": 2,
        "netsNeeded": 1,
        "visibility": visibility,
        "invitedGroupIds": invited_group_ids or [],
        "allowAddCourts": False,
        "courts": courts or [],
    }


def set_actor(client: TestClient, user_id: str) -> None:
    app.state.actor_id = user_id


def test_private_event_visibility_tracks_current_group_membership(client: TestClient):
    owner_id = str(app.state.actor_id)
    invited_id = create_user(client, "Invited")
    outsider_id = create_user(client, "Outsider")

    group_response = client.post("/v1/groups", json={"name": "Neighbors"})
    assert group_response.status_code == 201
    group_id = group_response.json()["id"]
    assert client.post(
        f"/v1/groups/{group_id}/members",
        json={"userId": invited_id},
    ).status_code == 201

    response = client.post(
        "/v1/events",
        json=event_body(visibility="private", invited_group_ids=[group_id]),
    )
    assert response.status_code == 201
    event_id = response.json()["id"]
    assert response.json()["ownerId"] == owner_id

    set_actor(client, outsider_id)
    hidden = client.get(f"/v1/events/{event_id}")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "EVENT_NOT_FOUND"

    set_actor(client, invited_id)
    assert client.get(f"/v1/events/{event_id}").status_code == 200

    set_actor(client, owner_id)
    assert client.delete(
        f"/v1/groups/{group_id}/members",
        params={"userId": invited_id},
    ).status_code == 200
    set_actor(client, invited_id)
    assert client.get(f"/v1/events/{event_id}").status_code == 404


def test_capacity_one_court_per_event_and_waitlist_promotion(client: TestClient):
    first_id = create_user(client, "First")
    queued_id = create_user(client, "Queued")
    owner_id = str(app.state.actor_id)
    body = event_body(
        courts=[
            {"category": "Men's", "type": "Doubles", "maxPlayers": 1},
            {"category": "Co-ed", "type": "Fours", "maxPlayers": 8},
        ]
    )
    created = client.post("/v1/events", json=body)
    assert created.status_code == 201
    event = created.json()
    event_id = event["id"]
    first_court_id = next(court["id"] for court in event["courts"] if court["category"] == "Men's")
    other_court_id = next(court["id"] for court in event["courts"] if court["category"] == "Co-ed")

    set_actor(client, first_id)
    assert client.post(
        f"/v1/events/{event_id}/courts/{first_court_id}/membership"
    ).status_code == 200
    assert client.post(
        f"/v1/events/{event_id}/courts/{other_court_id}/membership"
    ).status_code == 409

    set_actor(client, queued_id)
    full = client.post(f"/v1/events/{event_id}/courts/{first_court_id}/membership")
    assert full.status_code == 409
    assert full.json()["error"]["code"] == "COURT_FULL"
    queued = client.post(f"/v1/events/{event_id}/courts/{first_court_id}/waitlist")
    assert queued.status_code == 201

    set_actor(client, first_id)
    left = client.delete(f"/v1/events/{event_id}/courts/{first_court_id}/membership")
    assert left.status_code == 200
    assert left.json()["promotedUserId"] == queued_id
    promoted_court = next(
        court for court in left.json()["event"]["courts"] if court["id"] == first_court_id
    )
    assert promoted_court["currentPlayers"] == 1
    assert promoted_court["waitlistCount"] == 0
    assert owner_id != first_id


def test_event_updates_require_matching_version_and_occupied_court_is_immutable(client: TestClient):
    created = client.post(
        "/v1/events",
        json=event_body(
            courts=[{"category": "Co-ed", "type": "Doubles", "maxPlayers": 4}]
        ),
    )
    event = created.json()
    event_id = event["id"]
    court = event["courts"][0]
    owner_id = str(app.state.actor_id)
    assert client.patch(f"/v1/events/{event_id}", json={"title": "Changed"}).status_code == 428

    user_id = create_user(client, "Player")
    set_actor(client, user_id)
    assert client.post(
        f"/v1/events/{event_id}/courts/{court['id']}/membership"
    ).status_code == 200

    set_actor(client, owner_id)
    # The fixture actor is the event owner; the current version is returned in the event body.
    updated = client.patch(
        f"/v1/events/{event_id}",
        headers={"If-Match": str(event["version"])},
        json={
            "courts": [
                {
                    "id": court["id"],
                    "category": "Men's",
                    "type": "Doubles",
                    "maxPlayers": 4,
                }
            ]
        },
    )
    assert updated.status_code == 409
    assert updated.json()["error"]["code"] == "COURT_OCCUPIED"


def test_notifications_are_user_scoped_and_clear_is_permanent(client: TestClient):
    target_id = create_user(client, "Recipient")
    other_id = create_user(client, "Other")
    sender_id = str(app.state.actor_id)
    request = client.post("/v1/me/friends/requests", json={"userId": target_id})
    assert request.status_code == 201

    set_actor(client, target_id)
    inbox = client.get("/v1/me/notifications")
    assert inbox.status_code == 200
    assert len(inbox.json()["items"]) == 1
    notification_id = inbox.json()["items"][0]["id"]
    assert inbox.json()["items"][0]["category"] == "friend_request"

    set_actor(client, other_id)
    assert client.patch(f"/v1/me/notifications/{notification_id}").status_code == 404
    set_actor(client, target_id)
    assert client.delete("/v1/me/notifications").status_code == 204
    assert client.get("/v1/me/notifications").json()["items"] == []
    assert sender_id != target_id


def test_gendered_court_requires_profile_sex_but_coed_does_not(client: TestClient):
    incomplete_id = create_user(client, "New player", sex=None)
    created = client.post(
        "/v1/events",
        json=event_body(
            courts=[
                {"category": "Women's", "type": "Doubles", "maxPlayers": 4},
                {"category": "Co-ed", "type": "Doubles", "maxPlayers": 4},
            ]
        ),
    )
    event_id = created.json()["id"]
    courts = {court["category"]: court["id"] for court in created.json()["courts"]}
    women_court_id = courts["Women's"]
    coed_court_id = courts["Co-ed"]

    set_actor(client, incomplete_id)
    blocked = client.post(f"/v1/events/{event_id}/courts/{women_court_id}/membership")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "PROFILE_INCOMPLETE"
    allowed = client.post(f"/v1/events/{event_id}/courts/{coed_court_id}/membership")
    assert allowed.status_code == 200


def test_profile_and_event_patches_return_enum_backed_values(client: TestClient):
    user_id = str(app.state.actor_id)
    profile = client.patch("/v1/me/profile", json={"sex": "F", "position": "Setter"})
    assert profile.status_code == 200
    assert profile.json()["user"]["sex"] == "F"

    created = client.post(
        "/v1/events",
        json=event_body(courts=[{"category": "Co-ed", "type": "Doubles", "maxPlayers": 4}]),
    )
    event = created.json()
    court = event["courts"][0]
    updated = client.patch(
        f"/v1/events/{event['id']}",
        headers={"If-Match": str(event["version"])},
        json={
            "setting": "indoor",
            "courts": [
                {
                    "id": court["id"],
                    "category": "Women's",
                    "type": "Fours",
                    "maxPlayers": 8,
                }
            ],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["setting"] == "indoor"
    assert updated.json()["courts"][0]["category"] == "Women's"
    assert user_id == updated.json()["ownerId"]


def test_event_time_change_creates_inbox_notification_for_participants(client: TestClient):
    owner_id = str(app.state.actor_id)
    player_id = create_user(client, "Player")
    created = client.post(
        "/v1/events",
        json=event_body(courts=[{"category": "Co-ed", "type": "Doubles", "maxPlayers": 4}]),
    )
    event = created.json()
    court_id = event["courts"][0]["id"]
    set_actor(client, player_id)
    assert client.post(
        f"/v1/events/{event['id']}/courts/{court_id}/membership"
    ).status_code == 200

    set_actor(client, owner_id)
    updated = client.patch(
        f"/v1/events/{event['id']}",
        headers={"If-Match": str(event["version"])},
        json={"dateTime": (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()},
    )
    assert updated.status_code == 200

    set_actor(client, player_id)
    notifications = client.get("/v1/me/notifications").json()["items"]
    assert len(notifications) == 1
    assert notifications[0]["category"] == "game_update"


def test_protected_routes_require_bearer_auth(client: TestClient):
    auth_override = app.dependency_overrides.pop(get_current_user)
    try:
        response = client.get("/v1/me")
    finally:
        app.dependency_overrides[get_current_user] = auth_override
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_supabase_token_checks_signature_issuer_audience_and_expiry(monkeypatch):
    import time

    import jwt

    from app.auth import verify_supabase_token
    from app.config import get_settings
    from app.errors import ApiError

    monkeypatch.setenv("SUPABASE_URL", "https://pickup-test.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret-for-unit-tests-32-bytes-minimum")
    get_settings.cache_clear()
    claims = {
        "sub": "verified-user",
        "aud": "authenticated",
        "iss": "https://pickup-test.supabase.co/auth/v1",
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(claims, "test-secret-for-unit-tests-32-bytes-minimum", algorithm="HS256")
    assert verify_supabase_token(token)["sub"] == "verified-user"

    expired = jwt.encode(
        {**claims, "exp": int(time.time()) - 1},
        "test-secret-for-unit-tests-32-bytes-minimum",
        algorithm="HS256",
    )
    with pytest.raises(ApiError) as error:
        verify_supabase_token(expired)
    assert error.value.status_code == 401

    get_settings.cache_clear()
