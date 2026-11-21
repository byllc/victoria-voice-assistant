"""The dashboard's projects and chat contexts, for one chat connection (feature 20261112-023414).

Opening a context shows its whole transcript and puts its recent requests and replies back into
the model's conversation, so the chat carries on where it left off. A new chat starts empty.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Awaitable, Callable

from pipecat.processors.aggregators.llm_context import LLMContext

from victoria_voice_assistant.chat.tools import SessionState, ToolRuntime
from victoria_voice_assistant.projects import ProjectStore, Transcript

logger = logging.getLogger(__name__)

# What the dashboard may send about projects and contexts (chat/serializer.py checks the shapes).
PROJECT_MESSAGES = {
    "open_context",
    "new_chat",
    "rename_context",
    "archive_context",
    "new_project",
    "rename_project",
    "archive_project",
}

Send = Callable[[dict[str, Any]], Awaitable[None]]


class ChatContexts:
    def __init__(
        self,
        store: ProjectStore,
        state: SessionState,
        runtime: ToolRuntime,
        context: LLMContext,
        send: Send,
        broadcast: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.state = state
        self.runtime = runtime
        self.context = context
        self.send = send
        self.broadcast = broadcast  # tells every open dashboard the lists changed
        self.transcript = Transcript(store)
        self.transcript.on_created = self._created
        runtime.transcript = self.transcript
        self._tasks: set[asyncio.Task] = set()

    # From the dashboard -------------------------------------------------------------

    async def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        try:
            if kind == "hello":
                await self._reopen()
                return
            if kind in ("open_context", "new_chat") and not self.state.idle():
                await self.send({"type": "notice", "title": "Chats", "text": "Wait for the reply first."})
                return
            if kind == "open_context":
                await self.open(int(message["id"]))
            elif kind == "new_chat":
                project = message.get("project_id")
                await self.new_chat(int(project) if isinstance(project, int) and self.store.project_exists(project) else None)
            elif kind == "rename_context":
                self.store.rename_context(int(message["id"]), str(message["name"]))
                if message["id"] == self.transcript.context_id:
                    await self._send_current(with_messages=False)
                await self._changed()
            elif kind == "archive_context":
                archived = message.get("archived", True) is not False
                self.store.archive_context(int(message["id"]), archived)
                if archived and message["id"] == self.transcript.context_id:
                    await self.new_chat(self.transcript.project_id)
                await self._changed()
            elif kind == "new_project":
                project_id = self.store.create_project(str(message["name"]))
                await self.new_chat(project_id)
                await self._changed()
            elif kind == "rename_project":
                self.store.rename_project(int(message["id"]), str(message["name"]))
                await self._changed()
            elif kind == "archive_project":
                archived = message.get("archived", True) is not False
                self.store.archive_project(int(message["id"]), archived)
                if archived and message["id"] == self.transcript.project_id:
                    await self.new_chat(None)
                await self._changed()
        except sqlite3.Error:
            logger.exception("Couldn't change projects", extra={"event": "context_save_failed"})
            await self.send({"type": "notice", "title": "Chats", "text": "That didn't save."})

    async def _reopen(self) -> None:
        """A page (re)loaded: the context this browser had open, else the latest one, else a new chat."""
        client = self.state.client_id
        context_id = (self.store.last_opened(client) if client else None) or self.store.latest_context()
        if context_id is None:
            await self.new_chat(None)
        else:
            await self.open(context_id)

    # Switching -------------------------------------------------------------------

    async def open(self, context_id: int) -> None:
        view = self.store.context(context_id)
        if view is None:
            await self.new_chat(None)
            return
        self.transcript.context_id = context_id
        self.transcript.project_id = view["project_id"]
        if self.state.client_id:
            self.store.remember_opened(self.state.client_id, context_id)
        stale = {entry.name for entry in self.runtime.registry.entries if entry.forget_after_s is not None}
        self._restart([*self.store.conversation(context_id, stale)])
        logger.info("Chat context opened", extra={"event": "context_opened", "count": len(self.context.get_messages()) - 1})
        await self._send_current(with_messages=True)
        await self.send_projects()

    async def new_chat(self, project_id: int | None) -> None:
        self.transcript.context_id = None
        self.transcript.project_id = project_id or self.store.general()
        self._restart([])
        await self.send(
            {"type": "context", "context": {"id": None, "project_id": self.transcript.project_id, "name": "New chat"}, "messages": []}
        )
        await self.send_projects()

    def _restart(self, conversation: list[dict[str, str]]) -> None:
        """The model's conversation becomes the system prompt and ``conversation``; nothing else carries over."""
        system = [message for message in self.context.get_messages()[:1] if message.get("role") == "system"]
        self.context.set_messages([*system, *conversation])
        self.runtime.forget.clear()
        self.state.pending = None

    # Telling the dashboard -----------------------------------------------------------

    async def _send_current(self, with_messages: bool) -> None:
        if self.transcript.context_id is None:
            return
        view = self.store.context(self.transcript.context_id)
        message: dict[str, Any] = {"type": "context", "context": view}
        if with_messages:
            message["messages"] = [
                {"role": item["role"], "text": item["text"], "at": item["at"]} for item in self.store.messages(self.transcript.context_id)
            ]
        await self.send(message)

    async def send_projects(self) -> None:
        await self.send({"type": "projects", "projects": self.store.listing()})

    async def _changed(self) -> None:
        if self.broadcast is not None:
            await self.broadcast()
        else:
            await self.send_projects()

    def _created(self, context_id: int) -> None:
        """A new chat's first request made its context: show its name, and remember it for a refresh."""
        if self.state.client_id:
            self.store.remember_opened(self.state.client_id, context_id)

        async def tell() -> None:
            await self._send_current(with_messages=False)
            await self._changed()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # not in a running chat (tests): nothing to tell
            return
        task = loop.create_task(tell())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
