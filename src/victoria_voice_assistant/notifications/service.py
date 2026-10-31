"""The notification service (architecture §10.3): everything the assistant tells us unprompted.

Every notification is stored in SQLite before delivery. Sessions attach an `Outlet` for their
device; a short tick checks each device with undelivered notifications and announces them when
its outlets are idle (grouping whatever is ready), shows them silently during quiet hours or
do-not-disturb, and after a request adds "By the way, you have a message from Hermes." once per
notification. Unacknowledged notifications show as a badge until read, dismissed or expired.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Protocol

from victoria_voice_assistant.config.schema import NotificationsConfig, NotifyStyle
from victoria_voice_assistant.tools.outcome import PendingQuestion

logger = logging.getLogger(__name__)

Priority = Literal["urgent", "normal", "low"]
NotificationState = Literal["pending", "delivered", "acknowledged", "expired"]
Kind = Literal["result", "question", "error", "notice"]
UNREAD = ("pending", "delivered")

# Notifications posted this close together are announced as one group.
GROUP_WINDOW_S = 1.5
TICK_S = 0.5
NUMBERS = {2: "Two", 3: "Three", 4: "Four", 5: "Five"}


@dataclass
class Notification:
    id: str
    source: str
    kind: Kind
    headline: str  # one sentence announcing it: "Hermes has finished."
    summary: str  # what's read out on request
    full: str  # the complete text, for screens
    priority: Priority
    target: str  # the device that started it
    style: NotifyStyle
    state: NotificationState = "pending"
    task_id: str | None = None
    mentioned: bool = False
    created_at: float = 0.0
    expires_at: float = 0.0
    updated_at: float = 0.0

    @property
    def unread(self) -> bool:
        return self.state in UNREAD

    def snapshot(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class Announcement:
    """What one outlet is asked to deliver. `question` is set when the room is asked "Want to hear it?"."""

    text: str
    items: list[Notification]
    sound: bool = True
    question: PendingQuestion | None = None
    spoken: bool = False  # a speaker in the room says it, so screens needn't repeat the text
    full: list[Notification] = field(default_factory=list)  # items whose full text goes to screens


class Outlet(Protocol):
    """A session's view of its device: chat (screen) or voice (speaker)."""

    device: str
    speaks: bool

    def busy(self) -> bool: ...

    def speaking(self) -> bool:
        """The assistant is talking there right now (urgent items wait only for this)."""
        ...

    def requests(self) -> int:
        """How many requests this session has started (to add "By the way" after one)."""
        ...

    async def announce(self, announcement: Announcement) -> None: ...

    async def show(self, unread: list[Notification]) -> None: ...


def _label(name: str) -> str:
    return name[:1].upper() + name[1:]


def _names(items: list[Notification]) -> str:
    names = list(dict.fromkeys(_label(item.source) for item in items))
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _minutes(clock_time: str) -> int:
    hours, minutes = clock_time.split(":")
    return int(hours) * 60 + int(minutes)


class NotificationService:
    def __init__(
        self,
        config: NotificationsConfig | None = None,
        database_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or NotificationsConfig()
        self.database_path = database_path
        self._clock = clock
        self._items: dict[str, Notification] = {}
        self._outlets: list[Outlet] = []
        self._ready_at: dict[str, float] = {}  # device -> when its pending items may be announced
        self._seen_requests: dict[int, int] = {}  # outlet id -> request count at the last check
        self._dnd_until: float | None = None
        self._ticker: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    # Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Load unread notifications kept from before a restart, and start the delivery tick."""
        if self.database_path is not None:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    f"SELECT * FROM notifications WHERE state IN ({', '.join('?' * len(UNREAD))})", UNREAD
                ).fetchall()
            for row in rows:
                data = dict(row)
                data["mentioned"] = bool(data["mentioned"])
                item = Notification(**data)
                self._items[item.id] = item
                if item.kind == "question":
                    self._set(item, state="expired")  # its task ended with the restart
                    continue
                if item.state == "pending":
                    self._ready_at[item.target] = self._clock()
        self._ticker = asyncio.get_running_loop().create_task(self._tick_forever(), name="notifications")

    async def close(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except asyncio.CancelledError:
                pass

    def update_settings(self, config: NotificationsConfig) -> None:
        self.config = config

    # Outlets -------------------------------------------------------------

    def attach(self, outlet: Outlet) -> None:
        self._outlets.append(outlet)
        self._seen_requests[id(outlet)] = outlet.requests()
        self._spawn(outlet.show(self.unread(outlet.device)))
        if any(item.state == "pending" and item.target == outlet.device for item in self._items.values()):
            self._ready_at.setdefault(outlet.device, self._clock())

    def detach(self, outlet: Outlet) -> None:
        if outlet in self._outlets:
            self._outlets.remove(outlet)
        self._seen_requests.pop(id(outlet), None)

    def outlets(self, device: str) -> list[Outlet]:
        return [outlet for outlet in self._outlets if outlet.device == device]

    # Posting and reading -------------------------------------------------

    def post(
        self,
        source: str,
        kind: Kind,
        headline: str,
        summary: str,
        full: str | None,
        target: str,
        priority: Priority = "normal",
        style: NotifyStyle | None = None,
        task_id: str | None = None,
    ) -> Notification:
        now = self._clock()
        item = Notification(
            id=uuid.uuid4().hex[:12],
            source=source,
            kind=kind,
            headline=headline,
            summary=summary,
            full=full or summary,
            priority=priority,
            target=target,
            style=style or self.config.default_style,
            task_id=task_id,
            created_at=now,
            expires_at=now + self.config.expire_after_h * 3600,
            updated_at=now,
        )
        self._items[item.id] = item
        self._save(item)
        logger.info(
            "Notification posted",
            extra={"event": "notification_posted", "source": source, "kind": kind, "priority": priority, "device": target},
        )
        if priority == "urgent":
            self._ready_at[target] = now
        elif priority != "low":
            # Wait a moment so notifications that arrive together are announced together.
            self._ready_at.setdefault(target, now + GROUP_WINDOW_S)
        self._refresh(target)
        return item

    def unread(self, device: str | None = None) -> list[Notification]:
        """Unread notifications, newest first."""
        items = [i for i in self._items.values() if i.unread and (device is None or i.target == device)]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def get(self, notification_id: str) -> Notification | None:
        return self._items.get(notification_id)

    def last_from(self, source: str) -> Notification | None:
        """The newest result or error from a source, read or not ("What did Hermes say?")."""
        if self.database_path is not None:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM notifications WHERE source = ? AND kind IN ('result', 'error') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (source,),
                ).fetchone()
            if row is not None:
                return self._items.get(row["id"]) or Notification(**{**dict(row), "mentioned": bool(row["mentioned"])})
            return None
        found = [i for i in self._items.values() if i.source == source and i.kind in ("result", "error")]
        return max(found, key=lambda item: item.created_at, default=None)

    def question_for(self, task_id: str) -> Notification | None:
        return next((i for i in self._items.values() if i.kind == "question" and i.task_id == task_id and i.unread), None)

    def close_question(self, task_id: str, answered: bool) -> None:
        """The agent's question was answered (read) or timed out (expired): off the badge either way."""
        item = self.question_for(task_id)
        if item is not None:
            self._set(item, state="acknowledged" if answered else "expired")
            self._refresh(item.target)

    def acknowledge(self, items: list[Notification]) -> None:
        devices = set()
        for item in items:
            if item.unread:
                self._set(item, state="acknowledged")
                devices.add(item.target)
        for device in devices:
            self._refresh(device)

    # Quiet hours and do-not-disturb --------------------------------------

    def now(self) -> float:
        return self._clock()

    def set_dnd(self, until: float | None) -> None:
        self._dnd_until = until
        logger.info("Do not disturb", extra={"event": "do_not_disturb", "until": until})

    @property
    def dnd_until(self) -> float | None:
        if self._dnd_until is not None and self._dnd_until <= self._clock():
            self._dnd_until = None
        return self._dnd_until

    def in_quiet_hours(self) -> bool:
        hours = self.config.quiet_hours
        if hours is None:
            return False
        now = datetime.fromtimestamp(self._clock())
        minute = now.hour * 60 + now.minute
        start, end = _minutes(hours.start), _minutes(hours.end)
        if start == end:
            return False
        return start <= minute < end if start < end else minute >= start or minute < end

    def quiet(self) -> bool:
        return self.dnd_until is not None or self.in_quiet_hours()

    # Delivery ------------------------------------------------------------

    async def _tick_forever(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one bad delivery must not stop the service
                logger.exception("Notification delivery failed", extra={"event": "notification_failed"})

    async def tick(self) -> None:
        now = self._clock()
        expired = {item.target for item in self._items.values() if item.unread and item.expires_at <= now}
        for item in list(self._items.values()):
            if item.unread and item.expires_at <= now:
                self._set(item, state="expired")
        for device in expired:
            self._refresh(device)
        for device in list(self._ready_at):
            if self._ready_at[device] <= now:
                if await self._deliver(device):
                    self._ready_at.pop(device, None)
        if self.config.mention_pending_after_reply:
            await self._mention()

    async def _deliver(self, device: str) -> bool:
        """Announce the device's pending notifications. False means try again later (busy, or nobody there)."""
        pending = [i for i in self.unread(device) if i.state == "pending" and i.priority != "low"]
        if not pending:
            return True
        outlets = self.outlets(device)
        if not outlets:
            return False  # kept for when a client connects (and for phone push, later)
        questions = [i for i in pending if i.kind == "question"]
        if questions and not await self._ask(questions, outlets):
            return False
        items = [i for i in pending if i.kind != "question"]
        if not items:
            return True
        items.reverse()  # oldest first when spoken
        if self.quiet():
            # No sound: the screen and the badge only.
            announcement = Announcement(text=self._headlines(items), items=items, sound=False, full=items)
            for outlet in outlets:
                if not outlet.speaks:
                    await outlet.announce(announcement)
            self._delivered(items, mentioned=False)
            logger.info("Notifications shown quietly", extra={"event": "notification_quiet", "count": len(items)})
            return True
        if any(outlet.busy() for outlet in outlets):
            return False  # someone is mid-conversation there: wait until the turn ends
        styles = {item.style for item in items}
        speaker = any(outlet.speaks for outlet in outlets)
        if "ask_first" in styles and speaker:
            many = len(items) > 1
            text = f"{self._headlines(items)} Want to hear {'them' if many else 'it'}?"
            question = PendingQuestion(
                kind="yes_no", question=text, on_yes=("notifications", {"action": "read", "ids": [i.id for i in items]})
            )
            announcement = Announcement(text=text, items=items, question=question, full=items)
            acknowledged = False
        elif styles == {"chime_only"}:
            announcement = Announcement(text=self._headlines(items), items=items, full=items)
            acknowledged = False
        else:
            # speak_summary, or a screen-only room where the full text is shown with the headline.
            text = " ".join(f"{item.headline} {item.summary}".strip() for item in items) if speaker else self._headlines(items)
            announcement = Announcement(text=text, items=items, full=items)
            acknowledged = speaker
        chime_only = styles == {"chime_only"}
        announcement.spoken = speaker and not chime_only
        for outlet in outlets:
            if outlet.speaks and chime_only:
                continue  # chime only: the screen shows it; nothing is spoken
            await outlet.announce(announcement)
        if acknowledged:
            for item in items:
                self._set(item, state="acknowledged", mentioned=True)
            self._refresh(device)
        else:
            self._delivered(items, mentioned=True)
        logger.info(
            "Notifications announced",
            extra={"event": "notification_announced", "count": len(items), "device": device, "style": sorted(styles)},
        )
        return True

    async def _ask(self, questions: list[Notification], outlets: list[Outlet]) -> bool:
        """Agent questions are urgent: asked as soon as the assistant isn't speaking there, one at a time.
        During quiet hours they only wait in the list (Bill, 2026-10-31): no sound, no announcement."""
        if self.quiet():
            for item in questions:
                self._set(item, state="delivered", mentioned=True)  # never "by the way"d either
            self._refresh(questions[0].target)
            logger.info("Agent question held for quiet hours", extra={"event": "notification_quiet", "count": len(questions)})
            return True
        if any(outlet.speaking() for outlet in outlets):
            return False
        for item in reversed(questions):  # oldest first
            text = f"{item.headline} {item.summary}"
            announcement = Announcement(
                text=text,
                items=[item],
                question=PendingQuestion(kind="answer", question=text, task_id=item.task_id),
                full=[item],
                spoken=any(outlet.speaks for outlet in outlets),
            )
            for outlet in outlets:
                await outlet.announce(announcement)
            self._set(item, state="delivered", mentioned=True)
        self._refresh(questions[0].target)
        logger.info("Agent question asked", extra={"event": "notification_question", "count": len(questions)})
        return True

    def _headlines(self, items: list[Notification]) -> str:
        if len(items) == 1:
            return items[0].headline
        count = NUMBERS.get(len(items), str(len(items)))
        return f"{count} updates, from {_names(items)}."

    def _delivered(self, items: list[Notification], mentioned: bool) -> None:
        for item in items:
            self._set(item, state="delivered", mentioned=mentioned or item.mentioned)
        if items:
            self._refresh(items[0].target)

    async def _mention(self) -> None:
        """After a request in a room, mention unread notifications there once ("By the way, ...")."""
        for outlet in list(self._outlets):
            count = outlet.requests()
            previous = self._seen_requests.get(id(outlet), count)
            if count == previous or outlet.busy():
                continue
            self._seen_requests[id(outlet)] = count
            items = [i for i in self.unread(outlet.device) if i.state == "delivered" and not i.mentioned]
            if not items:
                continue
            text = f"By the way, you have {'a message' if len(items) == 1 else 'messages'} from {_names(items)}."
            targets = self.outlets(outlet.device)
            mention = Announcement(text=text, items=items, sound=False, spoken=any(t.speaks for t in targets))
            for target in targets:
                await target.announce(mention)
            for item in items:
                self._set(item, mentioned=True)
            for other in self.outlets(outlet.device):
                self._seen_requests[id(other)] = other.requests()
            logger.info("Pending notifications mentioned", extra={"event": "notification_mentioned", "count": len(items)})

    def _refresh(self, device: str) -> None:
        """Tell the device's screens the unread list changed (badge and list)."""
        unread = self.unread(device)
        for outlet in self.outlets(device):
            self._spawn(outlet.show(unread))

    def _spawn(self, coroutine) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coroutine.close()
            return
        task = loop.create_task(coroutine)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # Persistence ---------------------------------------------------------

    def _set(self, item: Notification, **fields: object) -> None:
        for key, value in fields.items():
            setattr(item, key, value)
        item.updated_at = self._clock()
        self._save(item)

    def _connect(self) -> sqlite3.Connection:
        assert self.database_path is not None
        return sqlite3.connect(self.database_path)

    def _save(self, item: Notification) -> None:
        if self.database_path is None:
            return
        row = item.snapshot()
        row["mentioned"] = int(item.mentioned)
        columns = ", ".join(row)
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO notifications ({columns}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
