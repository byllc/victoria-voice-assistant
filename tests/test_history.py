from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import LLMContextFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMMessagesAppendFrame, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test

from victoria_voice_assistant.chat.session import ChatSession
from victoria_voice_assistant.chat.tools import SessionState, ToolRuntime
from victoria_voice_assistant.chat.turns import HistoryTap, TurnPreprocessor
from victoria_voice_assistant.config.loader import ConfigError, load_config
from victoria_voice_assistant.config.schema import AgentsConfig, TimersConfig, ToolsConfig, WeatherConfig
from victoria_voice_assistant.history import HistoryRecorder, HistoryStore, window
from victoria_voice_assistant.storage.db import migrate
from victoria_voice_assistant.tools.outcome import PendingQuestion
from victoria_voice_assistant.tools.registry import Services, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService
from victoria_voice_assistant.voice.session import VoiceTurnPreprocessor

AGENTS = AgentsConfig.model_validate({"default": "helper", "agents": [{"name": "helper", "kind": "model", "model": "fast"}]})
HISTORY_TOOL = {
    "name": "history",
    "description": "Look up past requests.",
    "kind": "builtin",
    "service": "history",
    "params": {"words": {"type": "string"}, "room": {"type": "string"}, "when": {"type": "string"}},
}


async def _ignore(_message: dict[str, Any]) -> None:
    return None


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(migrate(tmp_path / "assistant.sqlite3"))


def _services(store: HistoryStore) -> Services:
    return Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})), history=store)


def _runtime(store: HistoryStore, device: str = "web", tools: list[dict[str, Any]] | None = None) -> tuple[SessionState, ToolRuntime]:
    state = SessionState(device=device)
    registry = build_registry(ToolsConfig.model_validate({"tools": tools or []}), AGENTS, _services(store))
    runtime = ToolRuntime(registry, state, notify=_ignore)
    runtime.history = HistoryRecorder(store, device=lambda: state.device, room=lambda d: {"office": "office"}.get(d))
    return state, runtime


# The store ------------------------------------------------------------------------


def test_a_request_is_saved_with_its_tools_and_reply(store: HistoryStore) -> None:
    entry_id = store.start("office", "office", "turn on the bedroom light")
    store.add_tool(entry_id, "home_control", {"action": "turn_on", "target": "bedroom light"})
    store.add_reply(entry_id, "Done.")
    store.add_reply(entry_id, "Anything else?")
    [entry] = store.recent()
    assert (entry.device, entry.room, entry.request) == ("office", "office", "turn on the bedroom light")
    assert entry.tools == [{"name": "home_control", "arguments": {"action": "turn_on", "target": "bedroom light"}}]
    assert entry.reply == "Done. Anything else?"


def test_search_by_words_room_and_time_newest_first(store: HistoryStore) -> None:
    now = datetime(2026, 11, 11, 15, 0).astimezone()
    morning = now.replace(hour=9).timestamp()
    store.start("office", "office", "turn on the bedroom light", at=morning)
    store.start("kitchen", "kitchen", "set a pasta timer", at=morning + 60)
    store.start("office", "office", "turn off the bedroom light", at=now.timestamp() - 60)
    store.start("office", "office", "what's the weather", at=(now - timedelta(days=1)).replace(hour=10).timestamp())

    assert [e.request for e in store.search("bedroom light", now=now)] == ["turn off the bedroom light", "turn on the bedroom light"]
    assert [e.request for e in store.search(room="kitchen", now=now)] == ["set a pasta timer"]
    assert [e.request for e in store.search(when="this_morning", now=now)] == ["set a pasta timer", "turn on the bedroom light"]
    assert [e.request for e in store.search(when="yesterday", now=now)] == ["what's the weather"]
    assert len(store.search(now=now, limit=2)) == 2


def test_the_tool_looks_back_only_search_days(store: HistoryStore) -> None:
    now = datetime.now().astimezone()
    store.start("office", "office", "old request", at=(now - timedelta(days=8)).timestamp())
    store.start("office", "office", "new request", at=(now - timedelta(days=6)).timestamp())
    assert [e.request for e in store.search(now=now)] == ["new request"]
    assert [e.request for e in store.search(when="this_week", now=now)] == ["new request"]


def test_the_tool_leaves_out_its_own_request(store: HistoryStore) -> None:
    store.start("web", None, "set a pasta timer")
    current = store.start("web", None, "what did I ask?")
    outcome = asyncio.run(store.handle({}, exclude=current))
    assert [item["said"] for item in outcome.result["found"]] == ["set a pasta timer"]
    assert set(outcome.result["found"][0]) == {"when", "room", "said", "reply"}
    empty = asyncio.run(store.handle({"words": "lasagne"}))
    assert empty.result["found"] == [] and "7 days" in empty.result["note"]


def test_window_names_local_spans() -> None:
    now = datetime(2026, 11, 11, 15, 30).astimezone()
    since, until = window("last_night", now)
    assert datetime.fromtimestamp(since).astimezone().hour == 17 and datetime.fromtimestamp(until).astimezone().hour == 6  # type: ignore[arg-type]
    assert window("whenever", now) == (None, None)


# Everything is kept ------------------------------------------------------------------------


def test_everything_is_kept_and_the_command_line_searches_it_all(store: HistoryStore) -> None:
    now = datetime.now().astimezone()
    store.start("kitchen", "kitchen", "an old pasta timer", at=(now - timedelta(days=40)).timestamp())
    store.start("office", "office", "a recent pasta timer", at=(now - timedelta(hours=1)).timestamp())
    store.start("office", "office", "what's the weather", at=now.timestamp())
    assert [e.request for e in store.search_all("pasta")] == ["an old pasta timer", "a recent pasta timer"]  # oldest first
    assert [e.request for e in store.search_all("pasta", room="kitchen")] == ["an old pasta timer"]
    assert [e.request for e in store.search_all(since=now - timedelta(days=2))] == ["a recent pasta timer", "what's the weather"]
    assert [e.request for e in store.search("pasta", now=now)] == ["a recent pasta timer"]  # the tool: search_days only
    stats = store.stats()
    assert stats["count"] == 3 and stats["oldest"] < now.timestamp() and stats["database_bytes"] > 0


# Saving from the sessions ----------------------------------------------------------------


def test_chat_saves_a_direct_reply(store: HistoryStore) -> None:
    async def scenario() -> None:
        state, runtime = _runtime(store)
        state.set_pending(PendingQuestion(kind="yes_no", question="Ask Helper?"))
        frame = LLMMessagesAppendFrame(messages=[{"role": "user", "content": "no"}], run_llm=True)
        await run_test(TurnPreprocessor(state, runtime, TimerService(TimersConfig())), frames_to_send=[frame])

    asyncio.run(scenario())
    [entry] = store.recent()
    assert entry.request == "no" and entry.device == "web" and entry.room is None
    assert entry.reply == "Okay."


def test_voice_saves_the_transcript_with_the_device_room(store: HistoryStore) -> None:
    async def scenario() -> None:
        state, runtime = _runtime(store, device="office")
        context = LLMContext(messages=[{"role": "user", "content": "What's the weather?"}])
        processor = VoiceTurnPreprocessor(state, runtime, TimerService(TimersConfig()), context=context)
        await run_test(processor, frames_to_send=[LLMContextFrame(context=context)])

    asyncio.run(scenario())
    [entry] = store.recent()
    assert (entry.request, entry.device, entry.room) == ("What's the weather?", "office", "office")


def test_the_tap_saves_the_model_reply_to_the_open_request(store: HistoryStore) -> None:
    async def scenario() -> None:
        _state, runtime = _runtime(store)
        runtime.history.begin("tell me a joke")  # type: ignore[union-attr]
        frames = [LLMFullResponseStartFrame(), LLMTextFrame("Why did "), LLMTextFrame("the chicken..."), LLMFullResponseEndFrame()]
        await run_test(HistoryTap(runtime), frames_to_send=frames)

    asyncio.run(scenario())
    assert store.recent()[0].reply == "Why did the chicken..."


def test_tool_calls_are_saved_and_the_lookup_leaves_out_its_own_request(store: HistoryStore) -> None:
    store.start("kitchen", "kitchen", "set a pasta timer")
    _state, runtime = _runtime(store, tools=[HISTORY_TOOL])
    runtime.history.begin("what did I ask the kitchen?")  # type: ignore[union-attr]
    entry = runtime.registry.get("history")
    assert entry is not None
    outcome, _ = asyncio.run(runtime.run(entry, {"room": "kitchen"}))
    assert [item["said"] for item in outcome.result["found"]] == ["set a pasta timer"]


def test_a_satellite_session_saves_under_its_room(store: HistoryStore, example_config_dir: Path) -> None:
    config = load_config(example_config_dir)
    config.tools.satellites = {}
    config.tools = ToolsConfig.model_validate({**config.tools.model_dump(), "satellites": {"kitchen-sat": {"host": "x.local", "room": "kitchen"}}})
    session = ChatSession(SessionState(device="kitchen-sat"), config, _services(store))
    assert session.runtime.history is not None
    session.runtime.history.begin("play some jazz")
    [entry] = store.recent()
    assert (entry.device, entry.room) == ("kitchen-sat", "kitchen")


def test_nothing_is_saved_without_a_store() -> None:
    state = SessionState()
    runtime = ToolRuntime(build_registry(ToolsConfig(), AGENTS, Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()))), state, notify=_ignore)
    assert runtime.history is None


# Config --------------------------------------------------------------------------


def test_the_example_config_has_the_tool_and_seven_days(example_config_dir: Path) -> None:
    config = load_config(example_config_dir)
    assert config.tools.history.search_days == 7
    assert any(tool.name == "history" and tool.service == "history" for tool in config.tools.tools)


def test_an_agent_can_never_have_the_history_tool(example_config_dir: Path) -> None:
    agents = example_config_dir / "agents.yaml"
    agents.write_text(agents.read_text().replace("tools: [weather, web_search, web_read]", "tools: [weather, history]"))
    with pytest.raises(ConfigError, match="command history"):
        load_config(example_config_dir)
