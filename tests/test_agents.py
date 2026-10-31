from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame, OutputTransportMessageFrame

from victoria_voice_assistant.agents import runner as runner_module
from victoria_voice_assistant.agents.model_agent import AgentError, AgentReply, run_model_agent
from victoria_voice_assistant.agents.runner import AgentRunner, match_agent_address
from victoria_voice_assistant.agents.tasks import TaskEvent, TaskService
from victoria_voice_assistant.chat.llm import build_llm
from victoria_voice_assistant.chat.tools import SessionState, ToolRuntime
from victoria_voice_assistant.chat.turns import TurnPreprocessor
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import AgentsConfig, AppConfig, TimersConfig, WeatherConfig
from victoria_voice_assistant.storage.db import migrate
from victoria_voice_assistant.tools.outcome import ToolOutcome
from victoria_voice_assistant.tools.registry import Services, ToolContext, ToolEntry, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

AGENTS = AgentsConfig.model_validate(
    {
        "default": "helper",
        "agents": [
            {"name": "helper", "aliases": ["helpr"], "kind": "model", "model": "fast", "mode": "wait"},
            {"name": "slowpoke", "kind": "model", "model": "fast", "mode": "auto", "wait_s": 0.05},
            {"name": "butler", "kind": "model", "model": "fast", "mode": "background"},
            {"name": "hermes", "kind": "harness", "model": "fast"},
        ],
    }
)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ask Helper to plan dinners for the week.", ("helper", "plan dinners for the week")),
        ("hey Victoria, ask helpr what to make with leeks?", ("helper", "what to make with leeks")),
        ("Please tell butler: the meeting moved to Tuesday", ("butler", "the meeting moved to Tuesday")),
        ("have Hermes look into flights", ("hermes", "look into flights")),
        ("ask about the weather", None),
        ("Helper is great", None),
        ("ask helper", None),
        ("ask helperful things", None),
    ],
)
def test_agent_address_is_matched_without_a_model(text: str, expected: tuple[str, str] | None) -> None:
    match = match_agent_address(text, AGENTS.agents)
    assert (None if match is None else (match[0].name, match[1])) == expected


def _config(example_config_dir: Path) -> AppConfig:
    config = load_config(example_config_dir)
    return config.model_copy(update={"agents": AGENTS})


def _runner(config: AppConfig, reply=None, database: Path | None = None) -> tuple[AgentRunner, list[TaskEvent]]:
    services = Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()))
    tasks = TaskService(database)
    events: list[TaskEvent] = []

    async def listen(event: TaskEvent) -> None:
        events.append(event)

    tasks.add_listener(listen)
    agent_runner = AgentRunner(config, services, tasks)
    services.agents = agent_runner

    async def fake_model_agent(agent, task) -> AgentReply:
        return await reply(agent, task) if reply else AgentReply("Leek and potato soup.", 40, 8)

    agent_runner._model_agent = fake_model_agent  # type: ignore[method-assign]
    return agent_runner, events


async def _settle(agent_runner: AgentRunner) -> None:
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not agent_runner.tasks.active():
            break
    await asyncio.sleep(0.01)


def _said(events: list[TaskEvent]) -> list[str]:
    return [event.text or "" for event in events if event.kind == "say"]


def test_wait_mode_acknowledges_then_says_the_result(example_config_dir: Path, tmp_path: Path) -> None:
    database = migrate(tmp_path / "a.sqlite3")

    async def scenario():
        agent_runner, events = _runner(_config(example_config_dir), database=database)
        outcome = await agent_runner.ask("Helpr", "what to make with leeks", "web")
        await _settle(agent_runner)
        return outcome, events, agent_runner

    outcome, events, agent_runner = run(scenario())
    assert outcome.speak == "Asking Helper." and outcome.result["agent"] == "helper"
    assert _said(events) == ["Leek and potato soup."]
    task = agent_runner.tasks.get(outcome.result["task_id"])
    assert task is not None and task.state == "finished" and task.input_tokens == 40
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT agent, state, result, request FROM tasks").fetchone()
    assert row == ("helper", "finished", "Leek and potato soup.", "what to make with leeks")


def test_background_mode_reports_back(example_config_dir: Path) -> None:
    async def scenario():
        agent_runner, events = _runner(_config(example_config_dir))
        outcome = await agent_runner.ask("butler", "plan dinners", "web")
        await _settle(agent_runner)
        return outcome, events

    outcome, events = run(scenario())
    assert outcome.speak == "Asking Butler. I'll let you know when it's done."
    assert _said(events) == ["Butler has finished. Leek and potato soup."]


def test_auto_mode_says_it_is_still_working_after_wait_s(example_config_dir: Path) -> None:
    async def slow(agent, task) -> AgentReply:
        await asyncio.sleep(0.15)
        return AgentReply("Done planning.")

    async def scenario():
        agent_runner, events = _runner(_config(example_config_dir), reply=slow)
        await agent_runner.ask("slowpoke", "plan a trip", "web")
        await asyncio.sleep(0.3)
        return events

    assert _said(run(scenario())) == [
        "Slowpoke is still working on it. I'll let you know when it's done.",
        "Slowpoke has finished. Done planning.",
    ]


def test_failures_timeouts_and_cancels_are_recorded(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(agent, task) -> AgentReply:
        raise AgentError("model unreachable")

    async def endless(agent, task) -> AgentReply:
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    async def scenario():
        failing, failed_events = _runner(_config(example_config_dir), reply=broken)
        failed = await failing.ask("helper", "x", "web")
        await _settle(failing)

        monkeypatch.setattr(runner_module, "AGENT_TIMEOUT_S", 0.05)
        slow, slow_events = _runner(_config(example_config_dir), reply=endless)
        timed = await slow.ask("helper", "x", "web")
        await asyncio.sleep(0.15)

        monkeypatch.setattr(runner_module, "AGENT_TIMEOUT_S", 10)
        stoppable, stop_events = _runner(_config(example_config_dir), reply=endless)
        stopped = await stoppable.ask("helper", "x", "web")
        await asyncio.sleep(0.01)
        assert stoppable.tasks.cancel(stopped.result["task_id"])
        await _settle(stoppable)
        return (
            (failing.tasks.get(failed.result["task_id"]), _said(failed_events)),
            (slow.tasks.get(timed.result["task_id"]), _said(slow_events)),
            (stoppable.tasks.get(stopped.result["task_id"]), _said(stop_events)),
        )

    (failed, failed_said), (timed, timed_said), (stopped, stopped_said) = run(scenario())
    assert failed.state == "failed" and failed.error == "model unreachable"
    assert failed_said == ["Helper couldn't finish that."]
    assert timed.state == "timed_out" and timed_said == ["Helper took too long, so I stopped it."]
    assert stopped.state == "cancelled" and stopped_said == []


def test_unknown_and_unsupported_agents_are_spoken(example_config_dir: Path) -> None:
    async def scenario():
        agent_runner, _ = _runner(_config(example_config_dir))
        return await agent_runner.ask("zed", "x", "web"), await agent_runner.ask("hermes", "x", "web")

    unknown, harness = run(scenario())
    assert unknown.speak == "I don't know an agent called zed."
    assert harness.speak == "Hermes isn't set up yet."


def test_tasks_left_running_by_a_restart_are_marked_failed(tmp_path: Path) -> None:
    database = migrate(tmp_path / "a.sqlite3")
    TaskService(database).create("helper", "web", "x", "wait")
    TaskService(database).start()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT state, error FROM tasks").fetchone() == ("failed", "the assistant restarted")


def test_ask_helper_goes_straight_to_the_agent(example_config_dir: Path) -> None:
    from pipecat.tests.utils import run_test

    async def scenario():
        config = _config(example_config_dir)
        agent_runner, events = _runner(config)
        registry = build_registry(config.tools, config.agents, agent_runner.services)
        state = SessionState()

        async def ignore(_message: dict[str, Any]) -> None:
            return None

        runtime = ToolRuntime(registry, state, notify=ignore)
        preprocessor = TurnPreprocessor(state, runtime, agent_runner.services.timers, agents=agent_runner)
        message = LLMMessagesAppendFrame(messages=[{"role": "user", "content": "Ask Helper what to make with leeks"}], run_llm=True)
        down, _ = await run_test(preprocessor, frames_to_send=[message])
        await _settle(agent_runner)
        return down, events

    down, events = run(scenario())
    messages = [f.message for f in down if isinstance(f, OutputTransportMessageFrame)]
    assert messages == [{"type": "token", "text": "Asking Helper."}, {"type": "done"}]
    assert all(not f.run_llm for f in down if isinstance(f, LLMMessagesAppendFrame))  # no model call
    assert events[-1].task.request == "what to make with leeks"


@pytest.mark.skipif(
    os.environ.get("ASSISTANT_LIVE_TESTS") != "1",
    reason="needs LM Studio running with the configured fast model; set ASSISTANT_LIVE_TESTS=1",
)
def test_live_model_agent_runs_its_own_tool_loop(example_config_dir: Path, live_api_key: None) -> None:
    calls: list[dict[str, Any]] = []

    async def pantry(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
        calls.append(arguments)
        return ToolOutcome(result={"item": arguments.get("item"), "in_pantry": 3})

    entry = ToolEntry(
        name="pantry",
        description="How many of an item are in the pantry.",
        kind="builtin",
        status="ready",
        handler=pantry,
        properties={"item": {"type": "string"}},
        required=["item"],
    )
    models = load_config(example_config_dir).models
    reply = run(
        run_model_agent(
            build_llm("fast", models),
            "Answer in one short sentence. Use tools when needed.",
            "How many cans of tomatoes are in the pantry?",
            [entry],
            ToolContext(device="test"),
        )
    )
    assert calls and "3" in reply.text and reply.input_tokens > 0
