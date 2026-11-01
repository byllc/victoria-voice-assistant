from __future__ import annotations

import asyncio
import base64
import json
import socket
import stat
import threading
import time
from datetime import datetime
from pathlib import Path

import http_ece
import pytest
import uvicorn
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request, Response

from victoria_voice_assistant.cli import main
from victoria_voice_assistant.config.schema import NotificationsConfig
from victoria_voice_assistant.notifications.push import PushService, generate_private_key, public_key, send_webpush
from victoria_voice_assistant.notifications.service import NotificationService
from victoria_voice_assistant.storage.db import migrate

AFTERNOON = datetime(2026, 11, 1, 14, 0).timestamp()
NIGHT = datetime(2026, 11, 1, 2, 0).timestamp()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def push_config(**overrides) -> NotificationsConfig:
    settings = {"web_push": True, "vapid_private_key": generate_private_key(), "vapid_subject": "mailto:home@example.com"}
    settings.update(overrides)
    return NotificationsConfig(**settings)


class Browser:
    """A browser's side of a push subscription: its key pair and auth secret."""

    def __init__(self, endpoint: str) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        point = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.info = {"endpoint": endpoint, "keys": {"p256dh": b64url(point), "auth": b64url(self.auth)}}

    def decrypt(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"))


def test_keys() -> None:
    private = generate_private_key()
    point = unb64url(public_key(private))
    assert len(unb64url(private)) == 32 and len(point) == 65 and point[0] == 4


def test_a_real_push_is_encrypted_for_the_browser_and_signed() -> None:
    """send_webpush (pywebpush) against a stand-in push service: the browser can decrypt it, the VAPID JWT verifies."""
    received: list[tuple[dict[str, str], bytes]] = []
    app = FastAPI()

    @app.post("/push/abc")
    async def push(request: Request) -> Response:
        received.append((dict(request.headers), await request.body()))
        return Response(status_code=201)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        config = push_config()
        service = PushService(config)
        browser = Browser(f"http://127.0.0.1:{port}/push/abc")
        payload = json.dumps({"title": "Hermes has finished.", "body": "The plan is ready.", "tag": "n1", "url": "/"})
        status = asyncio.run(send_webpush(browser.info, payload, service._key, {"sub": config.vapid_subject}, 3600))
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    assert status == 201
    [(headers, body)] = received
    assert browser.decrypt(body)["title"] == "Hermes has finished."
    assert headers["content-encoding"] == "aes128gcm" and headers["ttl"] == "3600"
    # Authorization: vapid t=<JWT>, k=<our public key>
    scheme, _, rest = headers["authorization"].partition(" ")
    parts = dict(part.strip().split("=", 1) for part in rest.split(","))
    assert scheme == "vapid" and parts["k"] == service.public_key
    header, claims, signature = parts["t"].split(".")
    claims_data = json.loads(unb64url(claims))
    assert claims_data["sub"] == "mailto:home@example.com" and claims_data["aud"] == f"http://127.0.0.1:{port}"
    signer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64url(service.public_key))
    raw = unb64url(signature)
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    signer.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if not ours


class FakeSender:
    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.sent: list[tuple[str, dict]] = []

    async def __call__(self, info, data, key, claims, ttl) -> int:
        self.sent.append((info["endpoint"], json.loads(data)))
        return self.status


class Screen:
    speaks = False

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.device = "web"

    def busy(self) -> bool:
        return False

    def speaking(self) -> bool:
        return False

    def requests(self) -> int:
        return 0

    async def announce(self, announcement) -> None:
        pass

    async def show(self, unread) -> None:
        pass


def _setup(now: float, status: int = 201, database: Path | None = None, **settings):
    config = push_config(**settings)
    sender = FakeSender(status)
    push = PushService(config, database, sender=sender, clock=lambda: now)
    notifications = NotificationService(config, database, clock=lambda: now, push=push)
    phone = Browser("https://push.example.net/phone")
    laptop = Browser("https://push.example.net/laptop")
    assert push.subscribe("phone-id", "web", phone.info) and push.subscribe("laptop-id", "web", laptop.info)
    return notifications, push, sender


def _post(notifications: NotificationService, **kwargs):
    fields = {"source": "hermes", "kind": "result", "headline": "Hermes has finished.", "summary": "The plan is ready.", "full": None, "target": "web"}
    fields.update(kwargs)
    return notifications.post(**fields)


def test_pushes_go_to_browsers_that_are_not_open() -> None:
    async def scenario():
        notifications, push, sender = _setup(AFTERNOON)
        notifications.attach(Screen("laptop-id"))  # the laptop has the page open
        _post(notifications)
        await asyncio.sleep(0.01)
        return sender, push

    sender, push = asyncio.run(scenario())
    [(endpoint, payload)] = sender.sent
    assert endpoint == "https://push.example.net/phone"
    assert payload == {"title": "Hermes has finished.", "body": "The plan is ready.", "tag": payload["tag"], "url": "/"}
    assert push.subscriptions()[0].last_sent_at is not None or push.subscriptions()[1].last_sent_at is not None


def test_no_push_in_quiet_hours_or_for_low_priority() -> None:
    async def scenario():
        night, _, night_sender = _setup(NIGHT)
        _post(night, kind="question", headline="Hermes has a question.", summary="Which folder?", priority="urgent")
        day, _, day_sender = _setup(AFTERNOON)
        _post(day, source="system", kind="notice", priority="low")
        await asyncio.sleep(0.01)
        return night_sender.sent, day_sender.sent

    assert asyncio.run(scenario()) == ([], [])


def test_a_gone_subscription_is_dropped() -> None:
    async def scenario():
        notifications, push, _ = _setup(AFTERNOON, status=410)
        _post(notifications)
        await asyncio.sleep(0.01)
        return push.subscriptions()

    assert asyncio.run(scenario()) == []


def test_subscriptions_are_kept_and_checked(tmp_path: Path) -> None:
    database = migrate(tmp_path / "a.sqlite3")
    _, push, _ = _setup(AFTERNOON, database=database)
    assert not push.subscribe("x", "web", {"endpoint": "http://evil.example/", "keys": {"p256dh": "a", "auth": "b"}})
    assert not push.subscribe("x", "web", {"endpoint": "https://push.example.net/1", "keys": {}})
    again = PushService(push.config, database)
    assert sorted(s.client_id for s in again.subscriptions()) == ["laptop-id", "phone-id"]


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        ({"web_push": False}, "web_push is off"),
        ({"vapid_private_key": None}, "no VAPID key (run `assistant push keys`)"),
        ({"vapid_subject": None}, "no vapid_subject (a mailto: or https: contact)"),
        ({"vapid_private_key": "not-a-key"}, "the VAPID key in config/.env isn't valid"),
    ],
)
def test_push_off_says_why(settings, problem) -> None:
    service = PushService(push_config(**settings))
    assert not service.enabled and service.problem == problem and service.public_key is None


def test_the_serializer_passes_push_messages() -> None:
    from victoria_voice_assistant.chat.serializer import ChatJsonSerializer

    serializer = ChatJsonSerializer()

    async def scenario():
        return [
            (await serializer.deserialize(text)).message
            for text in [
                '{"type": "hello", "client_id": "abc"}',
                '{"type": "push_subscribe", "subscription": {"endpoint": "https://x", "keys": {}}}',
                '{"type": "push_unsubscribe", "endpoint": "https://x"}',
            ]
        ]

    assert [m["type"] for m in asyncio.run(scenario())] == ["hello", "push_subscribe", "push_unsubscribe"]


def test_push_keys_command_writes_env_without_printing_the_key(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    (tmp_path / ".env").write_text("LMS_API_KEY=x")
    assert main(["push", "keys", "--config-dir", str(tmp_path)]) == 0
    env = (tmp_path / ".env").read_text()
    key = next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("VAPID_PRIVATE_KEY="))
    out = capsys.readouterr().out
    assert env.startswith("LMS_API_KEY=x\nVAPID_PRIVATE_KEY=") and key not in out and public_key(key) in out
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert main(["push", "keys", "--config-dir", str(tmp_path)]) == 0
    assert (tmp_path / ".env").read_text() == env  # never replaced
