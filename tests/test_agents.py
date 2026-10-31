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
            {"name": "alexa", "kind": "relay"},
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

    async def fake_call_model(agent, task, model, tools, history=None) -> AgentReply:
        return await reply(agent, task) if reply else AgentReply("Leek and potato soup.", 40, 8)

    agent_runner._call_model = fake_call_model  # type: ignore[method-assign]
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
        return await agent_runner.ask("zed", "x", "web"), await agent_runner.ask("alexa", "x", "web")

    unknown, relay = run(scenario())
    assert unknown.speak == "I don't know an agent called zed."
    assert relay.speak == "Alexa isn't set up yet."


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


LEEKS_MARKDOWN = """Here are some quick ways to use leeks:

**Soups & Stews:**
*   **Potato Leek Soup:** Sauté leeks with garlic, add broth and potatoes, then blend.
*   **Pasta Sauce:** Sauté leeks with garlic and olive oil, then toss with [pasta](https://example.com/pasta).

### Quick Tip
1. Always wash leeks thoroughly, as dirt hides between the layers
---
"""


def test_plain_text_removes_markdown_for_speech() -> None:
    from victoria_voice_assistant.agents.speech import plain_text

    assert plain_text(LEEKS_MARKDOWN) == (
        "Here are some quick ways to use leeks: Soups & Stews: Potato Leek Soup: Sauté leeks with garlic, "
        "add broth and potatoes, then blend. Pasta Sauce: Sauté leeks with garlic and olive oil, then toss with pasta. "
        "Quick Tip. Always wash leeks thoroughly, as dirt hides between the layers."
    )
    assert plain_text("snake_case_name and 2*3*4") == "snake_case_name and 2*3*4."


def test_long_replies_are_shortened_by_the_summarizer_or_cut() -> None:
    from victoria_voice_assistant.agents.speech import spoken_version

    class FakeSummarizer:
        def __init__(self, reply: str | None = None, fail: bool = False) -> None:
            self.reply, self.fail, self.seen = reply, fail, None

        async def run_inference(self, context, system_instruction=None, max_tokens=None):
            if self.fail:
                raise RuntimeError("down")
            self.seen = context.get_messages()[0]["content"]
            return self.reply

    async def scenario():
        good = FakeSummarizer("Make **potato leek soup** or a leek pasta sauce.")
        short = await spoken_version("Just a *short* answer.", good)
        summarized = await spoken_version(LEEKS_MARKDOWN, good)
        cut = await spoken_version(LEEKS_MARKDOWN, FakeSummarizer(fail=True))
        return short, summarized, cut, good.seen

    short, summarized, cut, seen = run(scenario())
    assert short == "Just a short answer."  # short replies skip the summarizer
    assert summarized == "Make potato leek soup or a leek pasta sauce."
    assert "**" not in (seen or "")  # the summarizer gets plain text
    assert cut == (
        "Here are some quick ways to use leeks: Soups & Stews: Potato Leek Soup: Sauté leeks with garlic, "
        "add broth and potatoes, then blend. Pasta Sauce: Sauté leeks with garlic and olive oil, then toss with pasta."
    )


def test_result_event_carries_short_speech_and_full_text(example_config_dir: Path) -> None:
    async def long_reply(agent, task) -> AgentReply:
        return AgentReply(LEEKS_MARKDOWN)

    async def scenario():
        agent_runner, events = _runner(_config(example_config_dir), reply=long_reply)
        agent_runner._summarizer = lambda: None  # type: ignore[method-assign]
        await agent_runner.ask("helper", "leeks", "web")
        await _settle(agent_runner)
        return [e for e in events if e.kind == "say"]

    (event,) = run(scenario())
    assert event.full == LEEKS_MARKDOWN and len(event.text or "") <= 280 and "**" not in (event.text or "")


def _task_service() -> TaskService:
    tasks = TaskService(None)
    tasks.create("helper", "web", "plan dinners for the week", "background")
    tasks.create("hermes", "web", "research flights to Denver", "background")
    done = tasks.create("helper", "web", "what to make with leeks", "wait")
    tasks.update(done, state="finished", result="**Potato leek soup.** It's quick. Wash them well.")
    return tasks


def test_tasks_tool_lists_and_reports_status() -> None:
    from victoria_voice_assistant.agents.tasks_tool import tasks_tool

    async def scenario():
        tasks = _task_service()
        return (
            await tasks_tool(tasks, {"action": "list"}),
            await tasks_tool(tasks, {"action": "list", "agent": "Hermes"}),
            await tasks_tool(TaskService(None), {"action": "list"}),
            await tasks_tool(tasks, {"action": "status", "about": "the leeks"}),
            await tasks_tool(tasks, {"action": "status", "about": "meal plan dinners"}),
            await tasks_tool(tasks, {"action": "status", "about": "taxes"}),
        )

    listed, hermes, empty, leeks, dinners, unknown = run(scenario())
    assert listed.speak == "Helper is working on plan dinners for the week. Hermes is working on research flights to Denver."
    assert hermes.speak == "Hermes is working on research flights to Denver."
    assert empty.speak == "Nothing is working on anything right now."
    assert leeks.speak == "Yes, Helper finished. Potato leek soup. It's quick."
    assert dinners.speak == "Helper is still working on plan dinners for the week."
    assert unknown.speak == "I don't see a task like that."


def test_tasks_tool_cancels_one_or_asks_which() -> None:
    from victoria_voice_assistant.agents.tasks_tool import tasks_tool

    async def scenario():
        tasks = _task_service()
        ambiguous = await tasks_tool(tasks, {"action": "cancel"})
        by_words = await tasks_tool(tasks, {"action": "cancel", "about": "the flights research"})
        by_agent = await tasks_tool(tasks, {"action": "cancel", "agent": "helper"})
        nothing = await tasks_tool(tasks, {"action": "cancel"})
        return ambiguous, by_words, by_agent, nothing, tasks

    ambiguous, by_words, by_agent, nothing, tasks = run(scenario())
    assert ambiguous.speak == "Which one: Hermes on research flights to Denver or Helper on plan dinners for the week?"
    assert ambiguous.pending is not None and ambiguous.pending.kind == "clarify"
    assert by_words.speak == "Cancelled Hermes's task."
    assert by_agent.speak == "Cancelled Helper's task."
    assert nothing.speak == "Nothing is running."
    assert sorted(t.state for t in tasks.recent()) == ["cancelled", "cancelled", "finished"]
