"""Command history: every request to the assistant, and the `history` tool that looks it up.

Each request (typed or spoken, from any device) is saved when it begins, with its device and room;
the tools it calls and its reply are added as they happen. Everything stays in the database, with
nothing archived or deleted, so we can see how fast it grows (Bill, 2026-11-11: "lets just save
everythign and we'll see how fast the databae grows, no archive"). The `history` tool looks back
`history.search_days` (7); `assistant history search` reads all of it.

History stays in the house: only the router model's tool reads it, never an agent.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from victoria_voice_assistant.tools.outcome import ToolOutcome

logger = logging.getLogger(__name__)

# A reply counts toward a request only this long after it began (a later one is something else).
REPLY_WINDOW_S = 120.0
# The most requests one lookup gives the model.
LOOKUP_LIMIT = 10
WHEN = ("last_hour", "today", "this_morning", "this_afternoon", "this_evening", "last_night", "yesterday", "this_week")


@dataclass
class Entry:
    id: int
    at: float
    device: str
    room: str | None
    request: str
    tools: list[dict[str, Any]]
    reply: str

    def record(self) -> dict[str, Any]:
        """For printing: the time in local ISO form."""
        return {
            "id": self.id,
            "at": datetime.fromtimestamp(self.at).astimezone().isoformat(timespec="seconds"),
            "device": self.device,
            "room": self.room,
            "request": self.request,
            "tools": self.tools,
            "reply": self.reply,
        }


def _entry(row: sqlite3.Row) -> Entry:
    try:
        tools = json.loads(row["tools"])
    except ValueError:
        tools = []
    return Entry(row["id"], row["at"], row["device"], row["room"], row["request"], tools, row["reply"])


def window(when: str | None, now: datetime | None = None) -> tuple[float | None, float | None]:
    """The span a spoken time names, in local time, as Unix times (None: open-ended)."""
    now = now or datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def at(day: datetime, hour: int) -> float:
        return (day + timedelta(hours=hour)).timestamp()

    yesterday = midnight - timedelta(days=1)
    spans: dict[str, tuple[float | None, float | None]] = {
        "last_hour": ((now - timedelta(hours=1)).timestamp(), None),
        "today": (midnight.timestamp(), None),
        "this_morning": (midnight.timestamp(), at(midnight, 12)),
        "this_afternoon": (at(midnight, 12), at(midnight, 17)),
        "this_evening": (at(midnight, 17), None),
        "last_night": (at(yesterday, 17), at(midnight, 6)),
        "yesterday": (yesterday.timestamp(), midnight.timestamp()),
        "this_week": ((midnight - timedelta(days=6)).timestamp(), None),
    }
    return spans.get(when or "", (None, None))


def _matches(entry: Entry, words: list[str], room: str | None) -> bool:
    if room and (entry.room or entry.device).lower() != room.lower() and entry.device.lower() != room.lower():
        return False
    text = f"{entry.request} {entry.reply}".lower()
    return all(word in text for word in words)


def _words(text: str | None) -> list[str]:
    return [word for word in (text or "").lower().split() if word]


class HistoryStore:
    """The `history` table."""

    def __init__(self, database: Path, search_days: int = 7) -> None:
        self.database = database
        self.search_days = search_days  # how far back the tool looks; everything is kept

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    # Saving -------------------------------------------------------------------

    def start(self, device: str, room: str | None, request: str, at: float | None = None) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO history (at, device, room, request) VALUES (?, ?, ?, ?)",
                (time.time() if at is None else at, device, room, request),
            )
            return int(cursor.lastrowid or 0)

    def add_tool(self, entry_id: int, name: str, arguments: dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT tools FROM history WHERE id = ?", (entry_id,)).fetchone()
            if row is None:
                return
            tools = json.loads(row["tools"] or "[]")
            tools.append({"name": name, "arguments": arguments})
            connection.execute("UPDATE history SET tools = ? WHERE id = ?", (json.dumps(tools, default=str), entry_id))

    def add_reply(self, entry_id: int, text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE history SET reply = CASE WHEN reply = '' THEN ? ELSE reply || ' ' || ? END WHERE id = ?",
                (text, text, entry_id),
            )

    # Looking up -----------------------------------------------------------------

    def recent(self, since: float | None = None, until: float | None = None, exclude: int | None = None) -> list[Entry]:
        """Saved requests, newest first."""
        query, params = "SELECT * FROM history WHERE 1 = 1", []
        if since is not None:
            query, params = query + " AND at >= ?", [*params, since]
        if until is not None:
            query, params = query + " AND at < ?", [*params, until]
        if exclude is not None:
            query, params = query + " AND id != ?", [*params, exclude]
        with self._connect() as connection:
            return [_entry(row) for row in connection.execute(query + " ORDER BY at DESC, id DESC", params)]

    def search(
        self,
        words: str | None = None,
        room: str | None = None,
        when: str | None = None,
        exclude: int | None = None,
        limit: int = LOOKUP_LIMIT,
        now: datetime | None = None,
    ) -> list[Entry]:
        since, until = window(when, now)
        floor = (now or datetime.now().astimezone()).timestamp() - self.search_days * 86400
        since = floor if since is None else max(since, floor)
        wanted = _words(words)
        found = [entry for entry in self.recent(since, until, exclude) if _matches(entry, wanted, room)]
        return found[:limit]

    async def handle(self, arguments: dict[str, Any], exclude: int | None = None) -> ToolOutcome:
        """The `history` tool."""
        when = arguments.get("when")
        if when is not None and when not in WHEN:
            when = None
        found = self.search(arguments.get("words"), arguments.get("room"), when, exclude=exclude)
        if not found:
            return ToolOutcome(result={"found": [], "note": f"nothing in the last {self.search_days} days matches"})
        return ToolOutcome(
            result={
                "found": [
                    {
                        "when": datetime.fromtimestamp(entry.at).astimezone().strftime("%A %-I:%M %p"),
                        "room": entry.room or entry.device,
                        "said": entry.request,
                        "reply": entry.reply,
                    }
                    for entry in found
                ]
            }
        )

    def search_all(self, words: str | None = None, since: datetime | None = None, room: str | None = None) -> list[Entry]:
        """Everything saved, oldest first, for `assistant history search` (no `search_days` limit)."""
        wanted = _words(words)
        found = self.recent(since.timestamp() if since else None)
        return [entry for entry in reversed(found) if _matches(entry, wanted, room)]

    def stats(self) -> dict[str, Any]:
        """How much is saved: for watching how fast the database grows."""
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count, MIN(at) AS oldest FROM history").fetchone()
            size = connection.execute(
                "SELECT SUM(pgsize) FROM dbstat WHERE name IN ('history', 'history_at')"
            ).fetchone()[0] if _has_dbstat(connection) else None
        return {"count": row["count"], "oldest": row["oldest"], "table_bytes": size, "database_bytes": self.database.stat().st_size}


def _has_dbstat(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1 FROM dbstat LIMIT 1")
        return True
    except sqlite3.Error:
        return False


class HistoryRecorder:
    """One session's link to the store: the request in progress, its tool calls and reply.

    Saving never gets in the way of a request: a failure is logged and the request goes on.
    """

    def __init__(self, store: HistoryStore, device: Callable[[], str], room: Callable[[str], str | None]) -> None:
        self.store = store
        self._device = device
        self._room = room
        self.current: int | None = None
        self._began = 0.0

    def begin(self, text: str) -> None:
        if not text.strip():
            return
        device = self._device()
        try:
            self.current = self.store.start(device, self._room(device), text.strip())
            self._began = time.monotonic()
        except sqlite3.Error:
            self.current = None
            logger.exception("Couldn't save the request to history", extra={"event": "history_failed"})

    def _open(self) -> int | None:
        if self.current is None or time.monotonic() - self._began > REPLY_WINDOW_S:
            return None
        return self.current

    def tool(self, name: str, arguments: dict[str, Any]) -> None:
        entry_id = self._open()
        if entry_id is None:
            return
        try:
            self.store.add_tool(entry_id, name, arguments)
        except sqlite3.Error:
            logger.exception("Couldn't save a tool call to history", extra={"event": "history_failed"})

    def reply(self, text: str) -> None:
        entry_id = self._open()
        if entry_id is None or not text.strip():
            return
        try:
            self.store.add_reply(entry_id, text.strip())
        except sqlite3.Error:
            logger.exception("Couldn't save a reply to history", extra={"event": "history_failed"})
