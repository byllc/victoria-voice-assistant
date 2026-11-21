from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import InputTransportMessageFrame
from pipecat.processors.aggregators.llm_context import LLMContext

from victoria_voice_assistant.chat.contexts import ChatContexts
from victoria_voice_assistant.chat.serializer import ChatJsonSerializer
from victoria_voice_assistant.chat.tools import SessionState, ToolRuntime
from victoria_voice_assistant.config.schema import AgentsConfig, TimersConfig, ToolsConfig, WeatherConfig
from victoria_voice_assistant.history import HistoryRecorder, HistoryStore
from victoria_voice_assistant.projects import ProjectStore, Transcript, context_name
from victoria_voice_assistant.storage.db import migrate
from victoria_voice_assistant.tools.registry import Services, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

AGENTS = AgentsConfig.model_validate({"default": "helper", "agents": [{"name": "helper", "kind": "model", "model": "fast"}]})
SYSTEM = {"role": "system", "content": "You are Victoria."}


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return migrate(tmp_path / "assistant.sqlite3")


@pytest.fixture
def store(database: Path) -> ProjectStore:
    return ProjectStore(database)


def _chat(store: ProjectStore, context_id: int, *pairs: tuple[str, str], tools: list[str] | None = None) -> None:
    for request, reply in pairs:
        message = store.add_message(context_id, "user", request)
        for name in tools or []:
            store.add_tool(message, name)
        store.add_message(context_id, "assistant", reply)


# Names ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_text", "name"),
    [
        ("what shelving would suit the carriage house loft?", "What shelving would suit the carriage"),
        ("hi", "Hi"),
        ("", "New chat"),
        ("pneumonoultramicroscopicsilicovolcanoconiosis-and-more-words-joined-together-here", "Pneumonoultramicroscopicsilicovolcanoconiosis-an…"),
    ],
)
def test_a_context_is_named_from_its_first_request(request_text: str, name: str) -> None:
    assert context_name(request_text) == name


def test_the_first_request_names_the_context_and_later_ones_dont(store: ProjectStore) -> None:
    context = store.create_context(store.general())
    _chat(store, context, ("Carriage house shelving", "Sure."), ("Something else entirely", "Okay."))
    assert store.context(context)["name"] == "Carriage house shelving"
    store.rename_context(context, "Loft shelves")
    store.add_message(context, "user", "more")
    assert store.context(context)["name"] == "Loft shelves"


# Listing ----------------------------------------------------------------------------


def test_general_is_made_once_and_listed_first(store: ProjectStore) -> None:
    other = store.create_project("Carriage house")
    general = store.general()
    assert store.general() == general
    assert [p["name"] for p in store.listing()] == ["General", "Carriage house"]
    assert other != general


def test_listing_hides_empty_contexts_and_flags_archived(store: ProjectStore) -> None:
    project = store.general()
    empty = store.create_context(project)
    kept = store.create_context(project, at=1.0)
    old = store.create_context(project, at=2.0)
    _chat(store, kept, ("one", "a"))
    _chat(store, old, ("two", "b"))
    store.archive_context(old)
    contexts = store.listing()[0]["contexts"]
    assert {c["id"] for c in contexts} == {kept, old}  # the empty one isn't listed
    assert {c["id"]: c["archived"] for c in contexts} == {kept: False, old: True}
    assert empty not in {c["id"] for c in contexts}
    store.archive_context(old, archived=False)
    assert not any(c["archived"] for c in store.listing()[0]["contexts"])


def test_archived_projects_are_flagged_and_kept(store: ProjectStore) -> None:
    project = store.create_project("Garage")
    store.archive_project(project)
    assert next(p for p in store.listing() if p["id"] == project)["archived"] is True
    store.rename_project(project, "Garage 2")
    assert next(p for p in store.listing() if p["id"] == project)["name"] == "Garage 2"


# What the model gets back -----------------------------------------------------------------


def test_the_conversation_is_requests_and_replies_only(store: ProjectStore) -> None:
    context = store.create_context(store.general())
    _chat(store, context, ("my sister is called Ann", "Noted."), ("what's my sister called?", "Ann."))
    assert store.conversation(context, set()) == [
        {"role": "user", "content": "my sister is called Ann"},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "what's my sister called?"},
        {"role": "assistant", "content": "Ann."},
    ]


def test_exchanges_with_out_of_date_tools_are_left_out(store: ProjectStore) -> None:
    context = store.create_context(store.general())
    _chat(store, context, ("what time is it?", "It's 10:24 AM."), tools=["clock"])
    _chat(store, context, ("my sister is called Ann", "Noted."), tools=["remember"])
    _chat(store, context, ("what's the weather?", "Sunny, 54."), tools=["weather"])
    assert store.conversation(context, {"clock", "weather"}) == [
        {"role": "user", "content": "my sister is called Ann"},
        {"role": "assistant", "content": "Noted."},
    ]
    assert len(store.messages(context)) == 6  # the screen still shows everything


def test_a_long_conversation_keeps_its_recent_turns_and_starts_on_a_request(store: ProjectStore) -> None:
    context = store.create_context(store.general())
    _chat(store, context, *[(f"q{i}", f"a{i}") for i in range(30)])
    kept = store.conversation(context, set(), limit=5)
    assert kept[0] == {"role": "user", "content": "q28"}
    assert [m["content"] for m in kept] == ["q28", "a28", "q29", "a29"]


# Which context a browser reopens ------------------------------------------------------------


def test_a_browser_reopens_its_own_context_else_the_latest(store: ProjectStore) -> None:
    project = store.general()
    first = store.create_context(project, at=1.0)
    second = store.create_context(project, at=2.0)
    _chat(store, first, ("one", "a"))
    _chat(store, second, ("two", "b"))
    store.remember_opened("kitchen-ipad", first)
    assert store.last_opened("kitchen-ipad") == first
    assert store.last_opened("phone") is None
    assert store.latest_context() == second
    store.archive_context(first)
    assert store.last_opened("kitchen-ipad") is None  # archived: falls back to the latest
    store.archive_project(project)
    assert store.latest_context() is None


# The transcript ------------------------------------------------------------------------------


def test_a_new_chat_gets_its_context_with_its_first_request(store: ProjectStore) -> None:
    created: list[int] = []
    transcript = Transcript(store)
    transcript.on_created = created.append
    transcript.reply("nothing yet")  # no context: nothing saved
    transcript.begin("set a pasta timer")
    transcript.tool("timer")
    transcript.tool("timer")
    transcript.reply("Pasta timer set.")
    assert created == [transcript.context_id]
    assert transcript.context_id is not None
    assert store.context(transcript.context_id)["project_id"] == store.general()
    assert store.messages(transcript.context_id) == [
        {"at": pytest.approx(store.messages(transcript.context_id)[0]["at"]), "role": "user", "text": "set a pasta timer", "tools": ["timer"]},
        {"at": pytest.approx(store.messages(transcript.context_id)[1]["at"]), "role": "assistant", "text": "Pasta timer set.", "tools": []},
    ]


def test_a_new_chat_in_a_project_goes_there(store: ProjectStore) -> None:
    project = store.create_project("Garage")
    transcript = Transcript(store)
    transcript.project_id = project
    transcript.begin("measure the door")
    assert store.context(transcript.context_id)["project_id"] == project


def test_saving_failures_dont_stop_the_request(store: ProjectStore, tmp_path: Path) -> None:
    broken = Transcript(ProjectStore(tmp_path / "missing" / "nothing.sqlite3"))
    broken.begin("hello")  # logged, not raised
    broken.tool("clock")
    broken.reply("hi")
    assert broken.context_id is None


def test_the_history_notes_the_context(database: Path, store: ProjectStore) -> None:
    history = HistoryStore(database)
    transcript = Transcript(store)
    recorder = HistoryRecorder(history, device=lambda: "web", room=lambda _d: None, context=lambda: transcript.context_id)
    transcript.begin("what time is it?")
    recorder.begin("what time is it?")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT context_id FROM history").fetchone()[0] == transcript.context_id


# A chat connection ------------------------------------------------------------------------------


class Session:
    def __init__(self, store: ProjectStore, client_id: str | None = "browser-1") -> None:
        self.sent: list[dict[str, Any]] = []
        self.state = SessionState(device="web")
        self.state.client_id = client_id
        services = Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})))
        tools = ToolsConfig.model_validate(
            {"tools": [{"name": "clock", "description": "The time.", "kind": "builtin", "service": "weather", "forget_after_s": 0}]}
        )
        self.runtime = ToolRuntime(build_registry(tools, AGENTS, services), self.state, notify=self._send)
        self.context = LLMContext(messages=[dict(SYSTEM)])
        self.contexts = ChatContexts(store, self.state, self.runtime, self.context, self._send)

    async def _send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def handle(self, message: dict[str, Any]) -> None:
        asyncio.run(self.contexts.handle(message))

    def last(self, kind: str) -> dict[str, Any]:
        return next(m for m in reversed(self.sent) if m["type"] == kind)


def test_the_first_visit_starts_a_new_chat_in_general(store: ProjectStore) -> None:
    session = Session(store)
    session.handle({"type": "hello", "client_id": "browser-1"})
    current = session.last("context")
    assert current["context"]["id"] is None and current["messages"] == []
    assert current["context"]["project_id"] == store.general()
    assert session.context.get_messages() == [SYSTEM]


def test_a_refresh_reopens_the_chat_and_the_model_gets_it_back(store: ProjectStore) -> None:
    context = store.create_context(store.general())
    _chat(store, context, ("my sister is called Ann", "Noted."))
    _chat(store, context, ("what time is it?", "It's 10:24 AM."), tools=["clock"])
    store.remember_opened("browser-1", context)
    session = Session(store)
    session.state.pending = object()  # type: ignore[assignment] - an old question doesn't survive the switch
    session.handle({"type": "hello", "client_id": "browser-1"})
    current = session.last("context")
    assert current["context"]["id"] == context
    assert [m["text"] for m in current["messages"]] == ["my sister is called Ann", "Noted.", "what time is it?", "It's 10:24 AM."]
    # The model gets the fact, not the out-of-date clock answer (clock has forget_after_s).
    assert session.context.get_messages() == [
        SYSTEM,
        {"role": "user", "content": "my sister is called Ann"},
        {"role": "assistant", "content": "Noted."},
    ]
    assert session.state.pending is None
    assert session.last("projects")["projects"][0]["contexts"][0]["id"] == context


def test_another_device_opens_the_latest_chat(store: ProjectStore) -> None:
    context = store.create_context(store.general())
    _chat(store, context, ("hello", "Hi."))
    session = Session(store, client_id="phone")
    session.handle({"type": "hello", "client_id": "phone"})
    assert session.last("context")["context"]["id"] == context
    assert store.last_opened("phone") == context


def test_new_chat_empties_the_conversation_and_switching_keeps_chats_apart(store: ProjectStore) -> None:
    first = store.create_context(store.general())
    _chat(store, first, ("my sister is called Ann", "Noted."))
    session = Session(store)
    session.handle({"type": "open_context", "id": first})
    session.handle({"type": "new_chat"})
    assert session.context.get_messages() == [SYSTEM]
    assert session.contexts.transcript.context_id is None
    session.contexts.transcript.begin("my brother is called Tom")  # the next request makes the second chat
    second = session.contexts.transcript.context_id
    assert second not in (None, first)
    session.contexts.transcript.reply("Noted.")
    session.handle({"type": "open_context", "id": first})
    assert "Tom" not in json.dumps(session.context.get_messages())
    session.handle({"type": "open_context", "id": second})
    assert "Ann" not in json.dumps(session.context.get_messages())
    assert store.last_opened("browser-1") == second


def test_archiving_the_open_chat_starts_a_new_one_in_its_project(store: ProjectStore) -> None:
    project = store.create_project("Garage")
    context = store.create_context(project)
    _chat(store, context, ("measure the door", "Okay."))
    session = Session(store)
    session.handle({"type": "open_context", "id": context})
    session.handle({"type": "archive_context", "id": context, "archived": True})
    assert session.contexts.transcript.context_id is None
    assert session.last("context")["context"] == {"id": None, "project_id": project, "name": "New chat"}
    assert store.context(context)["archived"] is True
    session.handle({"type": "archive_context", "id": context, "archived": False})
    assert store.context(context)["archived"] is False


def test_projects_are_made_renamed_and_archived(store: ProjectStore) -> None:
    session = Session(store)
    session.handle({"type": "new_project", "name": "Carriage house"})
    project = next(p for p in session.last("projects")["projects"] if p["name"] == "Carriage house")
    assert session.contexts.transcript.project_id == project["id"]  # a new project opens a new chat in it
    session.handle({"type": "rename_project", "id": project["id"], "name": "Carriage House"})
    session.handle({"type": "archive_project", "id": project["id"], "archived": True})
    listed = next(p for p in session.last("projects")["projects"] if p["id"] == project["id"])
    assert listed == {"id": project["id"], "name": "Carriage House", "archived": True, "contexts": []}
    assert session.contexts.transcript.project_id == store.general()  # the open project was archived


def test_renaming_the_open_chat_updates_its_name(store: ProjectStore) -> None:
    context = store.create_context(store.general())
    _chat(store, context, ("hello", "Hi."))
    session = Session(store)
    session.handle({"type": "open_context", "id": context})
    session.handle({"type": "rename_context", "id": context, "name": "Greetings"})
    assert session.last("context") == {"type": "context", "context": store.context(context)}
    assert store.context(context)["name"] == "Greetings"


def test_no_switching_while_a_request_is_running(store: ProjectStore) -> None:
    context = store.create_context(store.general())
    _chat(store, context, ("hello", "Hi."))
    session = Session(store)
    session.state.begin_request()
    session.state.awaiting_llm = True
    session.handle({"type": "open_context", "id": context})
    assert session.last("notice")["text"] == "Wait for the reply first."
    assert session.contexts.transcript.context_id is None


def test_a_missing_context_opens_a_new_chat(store: ProjectStore) -> None:
    session = Session(store)
    session.handle({"type": "open_context", "id": 999})
    assert session.last("context")["context"]["id"] is None


# What the dashboard may send ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "received"),
    [
        ({"type": "open_context", "id": 3}, {"type": "open_context", "id": 3}),
        ({"type": "new_chat"}, {"type": "new_chat"}),
        ({"type": "new_chat", "project_id": 2}, {"type": "new_chat", "project_id": 2}),
        ({"type": "rename_context", "id": 3, "name": "  Loft  "}, {"type": "rename_context", "id": 3, "name": "Loft"}),
        ({"type": "archive_context", "id": 3}, {"type": "archive_context", "id": 3, "archived": True}),
        ({"type": "archive_project", "id": 3, "archived": False}, {"type": "archive_project", "id": 3, "archived": False}),
        ({"type": "new_project", "name": "Garage"}, {"type": "new_project", "name": "Garage"}),
    ],
)
def test_project_messages_are_checked_and_passed_on(sent: dict[str, Any], received: dict[str, Any]) -> None:
    frame = asyncio.run(ChatJsonSerializer().deserialize(json.dumps(sent)))
    assert isinstance(frame, InputTransportMessageFrame) and frame.message == received


@pytest.mark.parametrize(
    "sent",
    [
        {"type": "open_context", "id": "3"},
        {"type": "open_context", "id": True},
        {"type": "open_context"},
        {"type": "rename_context", "id": 3, "name": "   "},
        {"type": "new_project", "name": 5},
    ],
)
def test_malformed_project_messages_are_dropped(sent: dict[str, Any]) -> None:
    assert asyncio.run(ChatJsonSerializer().deserialize(json.dumps(sent))) is None
