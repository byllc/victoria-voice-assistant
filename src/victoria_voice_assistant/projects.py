"""Projects and chat contexts (feature 20261112-023414).

A context is one saved chat transcript; a project groups contexts. Everything is kept: archiving
hides a context or project from the list but never deletes it. Typed chat only; satellites and
browser voice keep their own short conversation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

GENERAL = "General"
NAME_WORDS = 6
NAME_CHARS = 48
RESTORE_MESSAGES = 20  # how much of a long context goes back to the model; the screen shows all of it


def context_name(request: str) -> str:
    """A context's name from its first request: "Carriage house shelving ideas"."""
    words = request.strip().split()[:NAME_WORDS]
    name = " ".join(words).rstrip(".?!,;:")
    if len(name) > NAME_CHARS:
        name = name[:NAME_CHARS].rsplit(" ", 1)[0] + "…"
    return name[:1].upper() + name[1:] if name else "New chat"


class ProjectStore:
    """The `projects`, `contexts` and `context_messages` tables."""

    def __init__(self, database: Path) -> None:
        self.database = database

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    # Projects -----------------------------------------------------------------

    def general(self) -> int:
        """The "General" project, made the first time it's needed."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM projects WHERE name = ? AND archived_at IS NULL ORDER BY id LIMIT 1", (GENERAL,)
            ).fetchone()
            if row is not None:
                return int(row["id"])
            cursor = connection.execute("INSERT INTO projects (name, created_at) VALUES (?, ?)", (GENERAL, time.time()))
            return int(cursor.lastrowid or 0)

    def create_project(self, name: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute("INSERT INTO projects (name, created_at) VALUES (?, ?)", (name.strip(), time.time()))
            return int(cursor.lastrowid or 0)

    def rename_project(self, project_id: int, name: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE projects SET name = ? WHERE id = ?", (name.strip(), project_id))

    def archive_project(self, project_id: int, archived: bool = True) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE projects SET archived_at = ? WHERE id = ?", (time.time() if archived else None, project_id))

    def project_of(self, context_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute("SELECT project_id FROM contexts WHERE id = ?", (context_id,)).fetchone()
            return int(row["project_id"]) if row else None

    def project_exists(self, project_id: int) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None

    # Contexts -----------------------------------------------------------------

    def create_context(self, project_id: int, at: float | None = None) -> int:
        now = time.time() if at is None else at
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO contexts (project_id, created_at, updated_at) VALUES (?, ?, ?)", (project_id, now, now)
            )
            return int(cursor.lastrowid or 0)

    def context(self, context_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM contexts WHERE id = ?", (context_id,)).fetchone()
            return _context_view(row) if row else None

    def rename_context(self, context_id: int, name: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE contexts SET name = ? WHERE id = ?", (name.strip(), context_id))

    def archive_context(self, context_id: int, archived: bool = True) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE contexts SET archived_at = ? WHERE id = ?", (time.time() if archived else None, context_id))

    def move_context(self, context_id: int, project_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE contexts SET project_id = ? WHERE id = ?", (project_id, context_id))

    def latest_context(self) -> int | None:
        """The context used most recently, in a project that isn't archived."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.id FROM contexts c JOIN projects p ON p.id = c.project_id "
                "WHERE c.archived_at IS NULL AND p.archived_at IS NULL ORDER BY c.updated_at DESC, c.id DESC LIMIT 1"
            ).fetchone()
            return int(row["id"]) if row else None

    def last_opened(self, client_id: str) -> int | None:
        """The context this browser last had open, if it's still in the list."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.id FROM client_contexts x JOIN contexts c ON c.id = x.context_id JOIN projects p ON p.id = c.project_id "
                "WHERE x.client_id = ? AND c.archived_at IS NULL AND p.archived_at IS NULL",
                (client_id,),
            ).fetchone()
            return int(row["id"]) if row else None

    def remember_opened(self, client_id: str, context_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO client_contexts (client_id, context_id) VALUES (?, ?) "
                "ON CONFLICT (client_id) DO UPDATE SET context_id = excluded.context_id",
                (client_id, context_id),
            )

    def listing(self) -> list[dict[str, Any]]:
        """Every project with its contexts (archived ones flagged), newest first."""
        with self._connect() as connection:
            projects = connection.execute("SELECT * FROM projects ORDER BY (name = ?) DESC, created_at", (GENERAL,)).fetchall()
            contexts = connection.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM context_messages m WHERE m.context_id = c.id) AS count "
                "FROM contexts c ORDER BY c.updated_at DESC, c.id DESC"
            ).fetchall()
        return [
            {
                "id": int(project["id"]),
                "name": project["name"],
                "archived": project["archived_at"] is not None,
                "contexts": [_context_view(row) for row in contexts if row["project_id"] == project["id"] and row["count"] > 0],
            }
            for project in projects
        ]

    # Messages -----------------------------------------------------------------

    def add_message(self, context_id: int, role: str, text: str, at: float | None = None) -> int:
        now = time.time() if at is None else at
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO context_messages (context_id, at, role, text) VALUES (?, ?, ?, ?)", (context_id, now, role, text)
            )
            if role == "user":
                connection.execute("UPDATE contexts SET name = ? WHERE id = ? AND name IS NULL", (context_name(text), context_id))
            connection.execute("UPDATE contexts SET updated_at = ? WHERE id = ?", (now, context_id))
            return int(cursor.lastrowid or 0)

    def add_tool(self, message_id: int, name: str) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT tools FROM context_messages WHERE id = ?", (message_id,)).fetchone()
            if row is None:
                return
            tools = json.loads(row["tools"] or "[]")
            if name not in tools:
                tools.append(name)
            connection.execute("UPDATE context_messages SET tools = ? WHERE id = ?", (json.dumps(tools), message_id))

    def messages(self, context_id: int) -> list[dict[str, Any]]:
        """The whole transcript, for the screen."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT at, role, text, tools FROM context_messages WHERE context_id = ? ORDER BY id", (context_id,)
            ).fetchall()
        return [{"at": row["at"], "role": row["role"], "text": row["text"], "tools": json.loads(row["tools"] or "[]")} for row in rows]

    def conversation(self, context_id: int, stale_tools: set[str], limit: int = RESTORE_MESSAGES) -> list[dict[str, str]]:
        """What goes back to the model: the recent requests and replies, as plain messages.

        No tool calls or results, and no exchange whose request called a tool whose answers go out
        of date (`forget_after_s`), so an old time or weather answer is never repeated.
        """
        kept: list[dict[str, str]] = []
        skipping = False
        for message in self.messages(context_id):
            if message["role"] == "user":
                skipping = bool(stale_tools & set(message["tools"]))
            if not skipping:
                kept.append({"role": message["role"], "content": message["text"]})
        kept = kept[-limit:]
        while kept and kept[0]["role"] != "user":  # start on a request
            kept.pop(0)
        return kept


def _context_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "name": row["name"] or "New chat",
        "updated_at": row["updated_at"],
        "archived": row["archived_at"] is not None,
    }


class Transcript:
    """One chat session's link to its open context: saves each request, the tools it called and the replies.

    A new chat has no context yet; it's made with the first request, so empty chats never pile up.
    Saving never gets in the way of a request: a failure is logged and the request goes on.
    """

    def __init__(self, store: ProjectStore) -> None:
        self.store = store
        self.context_id: int | None = None
        self.project_id: int | None = None  # where a new chat's context goes (General when not set)
        self.on_created: Callable[[int], None] | None = None
        self._request: int | None = None

    def begin(self, text: str) -> None:
        if not text.strip():
            return
        try:
            if self.context_id is None:
                self.context_id = self.store.create_context(self.project_id or self.store.general())
                if self.on_created is not None:
                    self.on_created(self.context_id)
            self._request = self.store.add_message(self.context_id, "user", text.strip())
        except sqlite3.Error:
            self._request = None
            logger.exception("Couldn't save the chat message", extra={"event": "context_save_failed"})

    def tool(self, name: str) -> None:
        if self._request is None:
            return
        try:
            self.store.add_tool(self._request, name)
        except sqlite3.Error:
            logger.exception("Couldn't save the chat's tool call", extra={"event": "context_save_failed"})

    def reply(self, text: str) -> None:
        if self.context_id is None or not text.strip():
            return
        try:
            self.store.add_message(self.context_id, "assistant", text.strip())
        except sqlite3.Error:
            logger.exception("Couldn't save the chat reply", extra={"event": "context_save_failed"})
