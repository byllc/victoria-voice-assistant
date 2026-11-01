"""Web Push to installed web clients (architecture §10.3 "Phone push").

A browser that turns on alerts sends its push subscription over the chat websocket; it is kept
in SQLite with the browser's client id. When a notification is posted, every subscription for
its device whose browser isn't connected right now gets a push, so a phone hears about a result
with the page closed. Nothing is pushed during quiet hours or do-not-disturb, and `low` items
never are. The payload is encrypted for the browser (RFC 8291) and signed with our VAPID key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode

from victoria_voice_assistant.config.schema import NotificationsConfig

logger = logging.getLogger(__name__)


@dataclass
class Subscription:
    endpoint: str
    p256dh: str
    auth: str
    client_id: str
    device: str
    created_at: float = 0.0
    last_sent_at: float | None = None

    def info(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


# (subscription info, JSON payload, VAPID key, claims, ttl) -> HTTP status. Replaced in tests.
Sender = Callable[[dict[str, Any], str, Vapid, dict[str, Any], int], Awaitable[int]]


async def send_webpush(info: dict[str, Any], data: str, key: Vapid, claims: dict[str, Any], ttl: int) -> int:
    from pywebpush import WebPushException, webpush_async

    try:
        response = await webpush_async(info, data=data, vapid_private_key=key, vapid_claims=dict(claims), ttl=ttl, timeout=10)
    except WebPushException as exc:
        return exc.response.status if exc.response is not None else 0
    return response.status  # type: ignore[union-attr]


def generate_private_key() -> str:
    """A new VAPID private key, as the raw base64url string kept with the other secrets."""
    vapid = Vapid()
    vapid.generate_keys()
    number = vapid.private_key.private_numbers().private_value
    return b64urlencode(number.to_bytes(32, "big"))


def public_key(private_key: str) -> str:
    """The browser's `applicationServerKey` for a private key: the uncompressed P-256 point, base64url."""
    point = Vapid.from_raw(private_key.encode()).public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64urlencode(point)


class PushService:
    def __init__(
        self,
        config: NotificationsConfig,
        database_path: Path | None = None,
        sender: Sender = send_webpush,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = database_path
        self._sender = sender
        self._clock = clock
        self._subscriptions: dict[str, Subscription] = {}
        self._pending: set[asyncio.Task] = set()
        self.update_settings(config)
        if database_path is not None:
            with sqlite3.connect(database_path) as connection:
                connection.row_factory = sqlite3.Row
                for row in connection.execute("SELECT * FROM push_subscriptions"):
                    subscription = Subscription(**dict(row))
                    self._subscriptions[subscription.endpoint] = subscription

    def update_settings(self, config: NotificationsConfig) -> None:
        self.config = config
        self._key: Vapid | None = None
        self.public_key: str | None = None
        self.problem: str | None = None
        if not config.web_push:
            self.problem = "web_push is off"
        elif not config.vapid_private_key:
            self.problem = "no VAPID key (run `assistant push keys`)"
        elif not config.vapid_subject:
            self.problem = "no vapid_subject (a mailto: or https: contact)"
        else:
            try:
                self._key = Vapid.from_raw(config.vapid_private_key.encode())
                self.public_key = public_key(config.vapid_private_key)
            except Exception:  # noqa: BLE001 - a bad key turns push off, it doesn't stop the service
                self.problem = "the VAPID key isn't valid"
        if self.problem and config.web_push:
            logger.warning("Web Push is off", extra={"event": "push_unavailable", "reason": self.problem})

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def subscriptions(self, device: str | None = None) -> list[Subscription]:
        return [s for s in self._subscriptions.values() if device is None or s.device == device]

    def subscribe(self, client_id: str, device: str, info: dict[str, Any]) -> bool:
        """Keep a browser's subscription. Only https push services are accepted (the endpoint is called later)."""
        endpoint = str(info.get("endpoint") or "")
        keys = info.get("keys") or {}
        if urlparse(endpoint).scheme != "https" or not keys.get("p256dh") or not keys.get("auth") or not client_id:
            logger.warning("Push subscription rejected", extra={"event": "push_subscribe_rejected"})
            return False
        subscription = Subscription(endpoint, str(keys["p256dh"]), str(keys["auth"]), client_id, device, self._clock())
        self._subscriptions[endpoint] = subscription
        self._save(subscription)
        logger.info("Push subscription saved", extra={"event": "push_subscribed", "device": device})
        return True

    def unsubscribe(self, endpoint: str) -> None:
        self._subscriptions.pop(endpoint, None)
        if self.database_path is not None:
            with sqlite3.connect(self.database_path) as connection:
                connection.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def push(self, device: str, title: str, body: str, tag: str, connected: set[str], ttl_s: int) -> int:
        """Push to the device's subscriptions whose browsers aren't connected. Returns how many were sent."""
        if not self.enabled:
            return 0
        targets = [s for s in self.subscriptions(device) if s.client_id not in connected]
        payload = json.dumps({"title": title, "body": body, "tag": tag, "url": "/"})
        for subscription in targets:
            task = asyncio.get_running_loop().create_task(self._send(subscription, payload, ttl_s))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        return len(targets)

    async def _send(self, subscription: Subscription, payload: str, ttl_s: int) -> None:
        assert self._key is not None
        claims = {"sub": self.config.vapid_subject}
        status = await self._sender(subscription.info(), payload, self._key, claims, ttl_s)
        host = urlparse(subscription.endpoint).netloc
        if status in (404, 410):
            # The browser dropped the subscription (alerts turned off, app removed).
            self.unsubscribe(subscription.endpoint)
            logger.info("Push subscription expired", extra={"event": "push_expired", "host": host})
        elif 200 <= status < 300:
            subscription.last_sent_at = self._clock()
            self._save(subscription)
            logger.info("Push sent", extra={"event": "push_sent", "host": host})
        else:
            logger.warning("Push failed", extra={"event": "push_failed", "host": host, "status": status})

    def _save(self, subscription: Subscription) -> None:
        if self.database_path is None:
            return
        row = subscription.__dict__
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO push_subscriptions ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
