import asyncio
import logging
from datetime import timedelta
from uuid import UUID

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Notification, PushDelivery, PushDevice, utcnow


logger = logging.getLogger("pickup_api.push_worker")
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
MAX_ATTEMPTS = 8


def claim_delivery() -> tuple[str, str, str, str, str] | None:
    now = utcnow()
    with SessionLocal() as db:
        delivery = db.scalar(
            select(PushDelivery)
            .where(
                PushDelivery.status.in_(("pending", "sending")),
                PushDelivery.next_attempt_at <= now,
            )
            .order_by(PushDelivery.next_attempt_at, PushDelivery.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if delivery is None:
            return None
        notification = db.get(Notification, delivery.notification_id)
        device = db.get(PushDevice, delivery.device_id)
        if notification is None or device is None or not device.active:
            delivery.status = "failed"
            delivery.last_error = "RESOURCE_UNAVAILABLE"
            db.commit()
            return None
        delivery.status = "sending"
        delivery.next_attempt_at = now + timedelta(minutes=5)
        db.commit()
        return (
            str(delivery.id),
            device.token,
            notification.title,
            notification.body,
            str(notification.id),
        )


def complete_delivery(delivery_id: str, *, ticket_id: str | None = None, error: str | None = None) -> None:
    with SessionLocal() as db:
        delivery = db.get(PushDelivery, UUID(delivery_id))
        if delivery is None:
            return
        delivery.attempts += 1
        if error is None:
            delivery.status = "sent"
            delivery.ticket_id = ticket_id
            delivery.last_error = None
        elif error == "DeviceNotRegistered":
            delivery.status = "failed"
            delivery.last_error = error
            device = db.get(PushDevice, delivery.device_id)
            if device is not None:
                device.active = False
        elif delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = "failed"
            delivery.last_error = error
        else:
            delivery.status = "pending"
            delivery.last_error = error
            delivery.next_attempt_at = utcnow() + timedelta(
                seconds=min(3600, 5 * (2 ** delivery.attempts))
            )
        db.commit()


async def send_delivery(client: httpx.AsyncClient, delivery: tuple[str, str, str, str, str]) -> None:
    delivery_id, token, title, body, notification_id = delivery
    headers = {"Content-Type": "application/json"}
    access_token = get_settings().expo_access_token
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    payload = {
        "to": token,
        "title": title,
        "body": body,
        "sound": "default",
        "data": {"notificationId": notification_id},
    }
    try:
        response = await client.post(EXPO_PUSH_URL, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid push-service response.")
        ticket = result.get("data")
        if isinstance(ticket, list):
            ticket = ticket[0] if ticket else {}
        if not isinstance(ticket, dict):
            raise ValueError("Invalid push-service response.")
        if ticket.get("status") == "ok":
            complete_delivery(delivery_id, ticket_id=ticket.get("id"))
            return
        details = ticket.get("details")
        error = details.get("error") if isinstance(details, dict) else None
        complete_delivery(delivery_id, error=error if isinstance(error, str) else "PUSH_REJECTED")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Push delivery failed (delivery_id=%s, reason=%s)", delivery_id, type(exc).__name__)
        complete_delivery(delivery_id, error=type(exc).__name__)


async def run() -> None:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            delivery = claim_delivery()
            if delivery is None:
                await asyncio.sleep(settings.push_poll_seconds)
                continue
            await send_delivery(client, delivery)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
