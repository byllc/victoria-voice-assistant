from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pipecat.frames.frames import (
    EndFrame,
    InputTransportMessageFrame,
    LLMContextFrame,
    LLMMessagesAppendFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from victoria_voice_assistant.chat.serializer import ChatJsonSerializer
from victoria_voice_assistant.chat.timing import RequestTimer, TimingEndProcessor
from victoria_voice_assistant.chat.tools import MAX_TOOL_ROUNDS, SessionState, ToolRuntime
from victoria_voice_assistant.chat.turns import TurnPreprocessor, is_stop, match_yes_no
from victoria_voice_assistant.cli import main
from victoria_voice_assistant.config.loader import ConfigError, load_config
from victoria_voice_assistant.config.schema import AgentsConfig, TimersConfig, ToolsConfig, WeatherConfig
from victoria_voice_assistant.storage.db import migrate
from victoria_voice_assistant.tools.duration import parse_duration, speak_duration
from victoria_voice_assistant.tools.outcome import PendingQuestion
from victoria_voice_assistant.tools.registry import Services, ToolContext, build_registry, format_template
from victoria_voice_assistant.tools.testing import (
    CaseResult,
    PhraseCase,
    ToolCall,
    format_report,
    format_status,
    run_tool_tests,
)
from victoria_voice_assistant.tools.timers import TimerEvent, TimerService
from victoria_voice_assistant.tools.weather import WeatherError, WeatherService, shape_forecast

AGENTS = AgentsConfig.model_validate(
    {"default": "helper", "agents": [{"name": "helper", "kind": "model", "model": "fast"}]}
)


def run(coro):
    return asyncio.run(coro)


def _services(**kwargs: Any) -> Services:
    return Services(
        timers=kwargs.get("timers") or TimerService(TimersConfig()),
        weather=kwargs.get("weather") or WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})),
        http=kwargs.get("http"),
    )


def _registry(tools: list[dict[str, Any]], services: Services | None = None, **extra: Any):
    config = ToolsConfig.model_validate({"tools": tools, **extra})
    return build_registry(config, AGENTS, services or _services())


# Durations ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("10 minutes", 600),
        ("1 hour 30 minutes", 5400),
        ("an hour and a half", 5400),
        ("half an hour", 1800),
        ("twenty five minutes", 1500),
        ("90 seconds", 90),
        ("1:30", 90),
        ("10", 600),
        ("-2 minutes", -120),
        ("soon", None),
        (None, None),
    ],
)
def test_parse_duration(text: str | None, seconds: float | None) -> None:
    assert parse_duration(text) == seconds


def test_speak_duration() -> None:
    assert speak_duration(4 * 60 + 30) == "4 minutes 30 seconds"
    assert speak_duration(3600 + 5 * 60) == "1 hour 5 minutes"
    assert speak_duration(1) == "1 second"


# Timers ------------------------------------------------------------------------


def test_timers_named_start_duplicate_cancel_add_and_status(tmp_path: Path) -> None:
    async def scenario() -> None:
        timers = TimerService(TimersConfig(), migrate(tmp_path / "db.sqlite3"))
        assert (await timers.handle("web", {"action": "start", "name": "pasta", "duration": "10 minutes"})).speak == (
            "Pasta timer set for 10 minutes."
        )
        second = await timers.handle("web", {"action": "start", "name": "pasta timer", "duration": "5 minutes"})
        assert second.speak == "Second pasta timer set for 5 minutes."
        await timers.handle("web", {"action": "start", "name": "laundry", "duration": "30 minutes"})

        which = await timers.handle("web", {"action": "cancel"})
        assert which.speak == "Which timer: pasta 2, pasta or laundry?"
        assert which.pending is not None and which.pending.kind == "clarify"
        assert which.pending.options == ["pasta 2", "pasta", "laundry"]

        assert (await timers.handle("web", {"action": "cancel", "name": "laundry"})).speak == "Laundry timer cancelled."
        added = await timers.handle("web", {"action": "add_time", "name": "pasta 2", "duration": "5 minutes"})
        assert added.speak.startswith("Added 5 minutes to the pasta 2 timer.")
        removed = await timers.handle("web", {"action": "remove_time", "name": "pasta", "duration": "2 minutes"})
        assert removed.speak.startswith("Took 2 minutes off the pasta timer.")

        one = await timers.handle("web", {"action": "status", "name": "the pasta timer"})
        assert one.speak == "The pasta timer has 8 minutes left."
        everything = await timers.handle("web", {"action": "status"})
        assert everything.speak.startswith("You have two timers: pasta,")
        assert (await timers.handle("other", {"action": "status"})).speak == "You don't have any timers."
        await timers.close()

    run(scenario())


def test_timer_start_without_duration_asks() -> None:
    async def scenario() -> None:
        outcome = await TimerService(TimersConfig()).handle("web", {"action": "start"})
        assert outcome.speak == "How long should the timer be?"
        assert outcome.pending is not None and outcome.pending.kind == "clarify"

    run(scenario())


def test_timer_limit_per_device() -> None:
    async def scenario() -> None:
        timers = TimerService(TimersConfig(max_per_device=1))
        await timers.start_seconds("web", "a", 60)
        assert (await timers.start_seconds("web", "b", 60)).error
        await timers.close()

    run(scenario())


def test_timers_survive_restart(tmp_path: Path) -> None:
    database = migrate(tmp_path / "db.sqlite3")

    async def first() -> None:
        timers = TimerService(TimersConfig(), database)
        await timers.start_seconds("web", "pasta", 600)
        await timers.start_seconds("web", "laundry", 1800)
        await timers.close()

    async def second() -> list[str]:
        timers = TimerService(TimersConfig(), database)
        await timers.start()
        names = [timer.name for timer in timers.timers("web")]
        await timers.close()
        return names

    run(first())
    assert run(second()) == ["pasta", "laundry"]


def test_timer_rings_announces_together_and_stops(tmp_path: Path) -> None:
    async def scenario() -> list[TimerEvent]:
        events: list[TimerEvent] = []
        timers = TimerService(TimersConfig(), migrate(tmp_path / "db.sqlite3"))

        async def listen(event: TimerEvent) -> None:
            events.append(event)

        timers.add_listener(listen)
        await timers.start_seconds("web", "tea", 0.05)
        await timers.start_seconds("web", "eggs", 0.06)
        await asyncio.sleep(0.8)
        assert [timer.name for timer in timers.ringing("web")] == ["tea", "eggs"]
        assert await timers.stop_ringing("web") == ["tea", "eggs"]
        await asyncio.sleep(0.05)
        assert timers.timers("web") == []
        await timers.close()
        return events

    events = run(scenario())
    rings = [event for event in events if event.kind == "ring"]
    assert len(rings) == 1
    assert rings[0].text == "Your tea and eggs timers are done."
    assert any(event.kind == "ring_stopped" for event in events)


def test_timer_finished_while_down_rings_or_is_dropped(tmp_path: Path) -> None:
    database = migrate(tmp_path / "db.sqlite3")
    now = time.time()
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO timers (id, device, name, duration_s, ends_at, state, created_at) VALUES (?, 'web', ?, 60, ?, 'running', ?)",
            [("a", "recent", now - 10, now - 70), ("b", "ancient", now - 3600, now - 3660)],
        )

    async def scenario() -> list[str]:
        timers = TimerService(TimersConfig(ring_max_s=300), database)
        await timers.start()
        ringing = [timer.name for timer in timers.ringing("web")]
        await timers.close()
        return ringing

    assert run(scenario()) == ["recent"]
    with sqlite3.connect(database) as connection:
        assert [row[0] for row in connection.execute("SELECT name FROM timers")] == ["recent"]


# Registry ------------------------------------------------------------------------


def test_registry_builds_schemas_and_builtins() -> None:
    registry = _registry(
        [
            {
                "name": "timer",
                "description": "timers",
                "kind": "builtin",
                "service": "timers",
                "params": {"action": {"type": "string", "required": True}, "name": {"type": "string"}},
                "reply": {"template": True},
            }
        ]
    )
    names = [entry.name for entry in registry.active]
    assert names == ["timer", "clarify", "ask_agent", "offer_agent"]
    timer = registry.get("timer")
    assert timer is not None and timer.status == "ready" and timer.reply == "template"
    schema = timer.openai_tool()["function"]
    assert schema["parameters"]["required"] == ["action"]
    assert "required" not in schema["parameters"]["properties"]["action"]
    assert registry.get("ask_agent").properties["agent"]["enum"] == ["helper"]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("tool", "reason"),
    [
        ({"kind": "python", "service": "nowhere.module:missing"}, "handler `nowhere.module:missing` not found"),
        ({"kind": "builtin", "service": "search"}, "built-in service `search` isn't available yet"),
        ({"kind": "mcp", "mcp_server": None}, "MCP tools (kind `mcp`) aren't supported yet"),
        ({"kind": "http"}, "http tool has no `http:` section with a url"),
        ({"kind": "python", "params": {"x": {"type": "banana"}}}, "invalid params: param 'x' has unknown type 'banana'"),
    ],
)
def test_unresolvable_tools_become_automatic_stubs(tool: dict[str, Any], reason: str) -> None:
    registry = _registry([{"name": "thing", "description": "a thing", **tool}])
    entry = registry.get("thing")
    assert entry is not None
    assert entry.status == "auto_stub"
    assert entry.reason == reason

    outcome = run(entry.handler({}, ToolContext()))
    assert outcome.speak == "Thing isn't set up yet."
    assert outcome.chat is not None and reason in outcome.chat


def test_invalid_tool_definition_is_a_stub_not_a_rejected_file(example_config_dir: Path) -> None:
    path = example_config_dir / "tools.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["tools"].append({"name": "broken", "description": "oops", "kind": "teleport", "surprise": 1})
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = load_config(example_config_dir)
    broken = next(tool for tool in config.tools.tools if tool.name == "broken")
    assert broken.kind == "stub" and broken.broken is not None and "kind" in broken.broken
    registry = build_registry(config.tools, config.agents, _services())
    assert registry.get("broken").status == "auto_stub"  # type: ignore[union-attr]

    data["tools"].append({"description": "no name at all", "kind": "stub"})
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="name"):
        load_config(example_config_dir)


def test_explicit_stub_speaks_notice_sends_setup_and_can_offer_an_agent() -> None:
    registry = _registry(
        [
            {
                "name": "home_control",
                "description": "home",
                "kind": "stub",
                "notice": "Home control isn't set up yet.",
                "setup": "Add Home Assistant.",
                "then": {"offer_agent": True},
            }
        ]
    )
    outcome = run(registry.get("home_control").handler({"action": "turn_off"}, ToolContext()))  # type: ignore[union-attr]
    assert outcome.speak == "Home control isn't set up yet. Want me to ask Helper?"
    assert outcome.chat == "Add Home Assistant."
    assert outcome.pending is not None and outcome.pending.on_yes is not None
    assert outcome.pending.on_yes[0] == "ask_agent"


def test_disabled_tool_is_hidden_from_the_model() -> None:
    registry = _registry([{"name": "clock", "description": "time", "kind": "stub", "enabled": False}])
    assert registry.get("clock").status == "disabled"  # type: ignore[union-attr]
    assert "clock" not in [entry.name for entry in registry.active]


def test_python_tool_runs_handler() -> None:
    registry = _registry(
        [{"name": "clock", "description": "time", "kind": "python", "service": "victoria_voice_assistant.tools.local:current_time"}]
    )
    outcome = run(registry.get("clock").handler({}, ToolContext()))  # type: ignore[union-attr]
    assert outcome.speak is not None and outcome.speak.startswith("It's ")


def test_http_tool_fills_arguments_and_returns_json() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"level": 42})

    async def scenario() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            registry = _registry(
                [
                    {
                        "name": "tank",
                        "description": "tank level",
                        "kind": "http",
                        "http": {"url": "http://example.test/tanks/{tank}", "query": {"unit": "{unit}"}},
                        "reply": {"template": "The {tank} tank is at {level} percent."},
                    }
                ],
                _services(http=client),
            )
            entry = registry.get("tank")
            outcome = await entry.handler({"tank": "water", "unit": "pct"}, ToolContext())  # type: ignore[union-attr]
            runtime = ToolRuntime(registry, SessionState(), notify=_ignore)
            _, direct = await runtime.run(entry, {"tank": "water", "unit": "pct"})  # type: ignore[arg-type]
            return outcome, direct

    outcome, direct = run(scenario())
    assert str(seen[0].url) == "http://example.test/tanks/water?unit=pct"
    assert outcome.result == {"level": 42}
    assert direct == "The water tank is at 42 percent."


def test_format_template_tolerates_missing_fields() -> None:
    assert format_template("{name} is locked{extra}", {"name": "Front door"}, None) == "Front door is locked"


# Tool runtime: reply modes, guards ---------------------------------------------------


async def _ignore(_message: dict[str, Any]) -> None:
    return None


def test_reply_modes_decide_the_second_model_call() -> None:
    async def scenario() -> dict[str, str | None]:
        registry = _registry(
            [
                {"name": "t_model", "description": "x", "kind": "python", "service": "victoria_voice_assistant.tools.local:current_time"},
                {
                    "name": "t_template",
                    "description": "x",
                    "kind": "python",
                    "service": "victoria_voice_assistant.tools.local:current_time",
                    "reply": {"template": True},
                },
                {
                    "name": "t_silent",
                    "description": "x",
                    "kind": "python",
                    "service": "victoria_voice_assistant.tools.local:current_time",
                    "reply": {"silent": True},
                },
            ]
        )
        runtime = ToolRuntime(registry, SessionState(), notify=_ignore)
        results = {}
        for name in ("t_model", "t_template", "t_silent"):
            _, direct = await runtime.run(registry.get(name), {})  # type: ignore[arg-type]
            results[name] = direct
        return results

    results = run(scenario())
    assert results["t_model"] is None  # the model phrases it
    assert results["t_template"] is not None and results["t_template"].startswith("It's ")
    assert results["t_silent"] is None


def test_loop_guard_offers_an_agent() -> None:
    async def scenario():
        registry = _registry([])
        state = SessionState(llm_calls=MAX_TOOL_ROUNDS + 1)
        runtime = ToolRuntime(registry, state, notify=_ignore)
        return await runtime.run(registry.get("clarify"), {"question": "Which?"}), state  # type: ignore[arg-type]

    (outcome, direct), state = run(scenario())
    assert direct == "I can't do that myself. Want me to ask Helper?"
    assert state.pending is not None and state.pending.kind == "yes_no"


def test_only_one_clarifying_question_per_request() -> None:
    async def scenario():
        registry = _registry([])
        state = SessionState()
        runtime = ToolRuntime(registry, state, notify=_ignore)
        first = await runtime.run(registry.get("clarify"), {"question": "Which room?"})  # type: ignore[arg-type]
        state.begin_request()
        state.answering_clarify = True
        second = await runtime.run(registry.get("clarify"), {"question": "Which lamp?"})  # type: ignore[arg-type]
        return first[1], second[1]

    first, second = run(scenario())
    assert first == "Which room?"
    assert second == "I can't do that myself. Want me to ask Helper?"


# Turns: pending questions and stop ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "answer"),
    [("Yes", "yes"), ("yeah, go ahead", None), ("sure", "yes"), ("No thanks.", "no"), ("never mind", "no"), ("what time is it", None)],
)
def test_match_yes_no(text: str, answer: str | None) -> None:
    assert match_yes_no(text) == answer


def test_stop_phrases() -> None:
    assert is_stop("Stop!") and is_stop("stop the timer") and not is_stop("stop the music in the kitchen")


def _user(text: str) -> LLMMessagesAppendFrame:
    return LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=True)


def _messages(frames: list) -> list[dict[str, Any]]:
    return [f.message for f in frames if isinstance(f, OutputTransportMessageFrame)]


def test_pending_yes_runs_follow_up_without_a_model() -> None:
    from pipecat.tests.utils import run_test

    async def scenario():
        registry = _registry([])
        state = SessionState()
        state.set_pending(PendingQuestion(kind="yes_no", question="Ask Helper?", on_yes=("ask_agent", {"agent": "helper", "task": "x"})))
        runtime = ToolRuntime(registry, state, notify=_ignore)
        down, _ = await run_test(TurnPreprocessor(state, runtime, TimerService(TimersConfig())), frames_to_send=[_user("yes")])
        return down, state

    down, state = run(scenario())
    assert _messages(down) == [{"type": "token", "text": "Agents aren't set up yet."}, {"type": "done"}]
    appended = [f for f in down if isinstance(f, LLMMessagesAppendFrame)]
    assert len(appended) == 1 and appended[0].run_llm is False  # recorded in context, no model call
    assert state.pending is None


def test_expired_or_unmatched_pending_goes_to_the_model() -> None:
    from pipecat.tests.utils import run_test

    async def scenario(text: str, expired: bool):
        registry = _registry([])
        state = SessionState()
        state.set_pending(PendingQuestion(kind="yes_no", question="Ask Helper?"))
        if expired:
            state.pending.expires_at = 0  # type: ignore[union-attr]
        runtime = ToolRuntime(registry, state, notify=_ignore)
        down, _ = await run_test(TurnPreprocessor(state, runtime, TimerService(TimersConfig())), frames_to_send=[_user(text)])
        return [f for f in down if isinstance(f, LLMMessagesAppendFrame)]

    for text, expired in (("yes", True), ("what's the weather", False)):
        passed = run(scenario(text, expired))
        assert len(passed) == 1 and passed[0].run_llm is True


def test_stop_silences_a_ringing_timer_without_a_model() -> None:
    from pipecat.tests.utils import run_test

    async def scenario():
        timers = TimerService(TimersConfig())
        await timers.start_seconds("web", "tea", 0.01)
        await asyncio.sleep(0.05)
        assert timers.ringing("web")
        state = SessionState()
        runtime = ToolRuntime(_registry([]), state, notify=_ignore)
        down, _ = await run_test(TurnPreprocessor(state, runtime, timers), frames_to_send=[_user("stop")])
        ringing = timers.ringing("web")
        await timers.close()
        return down, ringing

    down, ringing = run(scenario())
    assert ringing == []
    assert _messages(down)[0] == {"type": "token", "text": "Okay."}


def test_timer_stop_message_from_the_client() -> None:
    serializer = ChatJsonSerializer()

    async def scenario():
        frame = await serializer.deserialize('{"type": "timer_stop"}')
        urgent = await serializer.serialize(OutputTransportMessageUrgentFrame(message={"type": "timers", "timers": []}))
        return frame, urgent

    frame, urgent = run(scenario())
    assert isinstance(frame, InputTransportMessageFrame) and frame.message == {"type": "timer_stop"}
    assert json.loads(urgent) == {"type": "timers", "timers": []}


def test_second_model_call_is_counted_from_the_upstream_context_frame() -> None:
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.tests.utils import run_test

    calls = []
    timer = RequestTimer(on_llm_call=lambda: len(calls.append(1) or calls))

    async def scenario():
        await run_test(
            TimingEndProcessor(timer),
            frames_to_send=[LLMContextFrame(context=LLMContext())],
            frames_to_send_direction=FrameDirection.UPSTREAM,
            expected_up_frames=[LLMContextFrame],
        )

    run(scenario())
    assert timer.llm_call == 1


# Weather ------------------------------------------------------------------------------


FORECAST = {
    "current": {"time": "2026-10-30T14:00", "temperature_2m": 54.2, "apparent_temperature": 51.0, "weather_code": 3, "wind_speed_10m": 8.0},
    "hourly": {
        "time": [f"2026-10-30T{hour:02d}:00" for hour in range(24)],
        "temperature_2m": [50 + hour % 5 for hour in range(24)],
        "precipitation_probability": [70 if hour >= 16 else 5 for hour in range(24)],
        "weather_code": [61 if hour >= 16 else 3 for hour in range(24)],
    },
    "daily": {
        "time": [f"2026-10-{day}" for day in range(30, 32)] + [f"2026-11-0{day}" for day in range(1, 6)],
        "weather_code": [61, 0, 2, 3, 3, 80, 0],
        "temperature_2m_max": [56.4, 60.1, 58, 57, 55, 52, 50],
        "temperature_2m_min": [41.2, 44, 40, 39, 38, 37, 36],
        "precipitation_probability_max": [80, 10, 20, 30, 40, 60, 0],
    },
}


def test_weather_shapes_only_the_needed_fields() -> None:
    place = {"name": "Home", "lat": 1, "lon": 2}
    now = shape_forecast(FORECAST, place, "now", "imperial")
    assert now["now"]["temp"] == 54 and now["now"]["conditions"] == "cloudy"
    assert now["next_hours"] == {"max_chance_of_rain_pct": 70, "rain_likely_from": "4 pm"}
    tomorrow = shape_forecast(FORECAST, place, "tomorrow", "imperial")
    assert tomorrow["day"] == {
        "date": "2026-10-31", "day": "Saturday", "conditions": "clear", "high": 60, "low": 44, "chance_of_rain_pct": 10,
    }
    weekend = shape_forecast(FORECAST, place, "this weekend", "imperial")
    assert [day["day"] for day in weekend["weekend"]] == ["Saturday", "Sunday"]
    assert "now" not in tomorrow


class FakeProvider:
    def __init__(self, fail: bool = False) -> None:
        self.forecasts = 0
        self.fail = fail

    async def geocode(self, name: str) -> dict[str, Any] | None:
        return {"name": "Boston, Massachusetts", "lat": 42.36, "lon": -71.06} if name == "Boston" else None

    async def forecast(self, lat: float, lon: float, units: str) -> dict[str, Any]:
        if self.fail:
            raise WeatherError("down")
        self.forecasts += 1
        return FORECAST


def test_weather_caches_and_geocodes() -> None:
    provider = FakeProvider()
    service = WeatherService(WeatherConfig(home={"lat": 1, "lon": 2}), provider=provider)

    async def scenario():
        first = await service.handle({"when": "now"})
        await service.handle({"when": "tomorrow"})
        boston = await service.handle({"location": "Boston", "when": "tomorrow"})
        nowhere = await service.handle({"location": "Atlantis"})
        return first, boston, nowhere

    first, boston, nowhere = run(scenario())
    assert first.result["place"] == "Home"
    assert boston.result["place"] == "Boston, Massachusetts"
    assert nowhere.speak == "I couldn't find Atlantis."
    assert provider.forecasts == 2  # home once (cached for the second question), Boston once


def test_weather_failure_is_spoken_not_guessed() -> None:
    service = WeatherService(WeatherConfig(home={"lat": 1, "lon": 2}), provider=FakeProvider(fail=True))
    outcome = run(service.handle({}))
    assert outcome.error and outcome.speak == "I couldn't get the weather right now."


# tools test / tools status ------------------------------------------------------------------


def test_tools_test_runs_examples_and_tests_with_sandboxed_timers(example_config_dir: Path) -> None:
    config = load_config(example_config_dir)
    seen: list[str] = []

    async def complete(messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        say = messages[-1]["content"]
        seen.append(say)
        assert messages[0]["role"] == "system" and any(tool["function"]["name"] == "timer" for tool in tools)
        if say == "cancel the timer":
            return [ToolCall("timer", {"action": "cancel"})], None
        if say == "set a timer":
            return [ToolCall("clarify", {"question": "How long?"})], None
        if say == "how many ounces are in a cup":
            return [], "Eight."
        return [], "I don't know."

    results = run(run_tool_tests(config, complete=complete))
    by_phrase = {(r.case.say, r.case.expect): r for r in results}
    # Two timers running: the timer service asks which one, which counts as clarify.
    assert by_phrase[("cancel the timer", "clarify")].passed
    assert by_phrase[("cancel the timer", "tool:timer")].passed
    assert by_phrase[("set a timer", "clarify")].passed
    assert by_phrase[("how many ounces are in a cup", "answer")].passed
    assert not by_phrase[("book me a flight to Denver", "offer_agent")].passed
    report = format_report(results)
    assert "FAIL  offer_agent" in report and "expected offer_agent, got answer [tests[" in report


def test_format_report_summary() -> None:
    case = PhraseCase(say="what time is it", expect="tool:clock", source="clock.examples")
    report = format_report(
        [
            CaseResult(case, True, outcomes=["tool:clock"], calls=[ToolCall("clock", {})], ms=700),
            CaseResult(case, False, error="APIConnectionError: refused", ms=5),
        ]
    )
    assert "PASS  tool:clock" in report
    assert "error: APIConnectionError: refused" in report
    assert report.endswith("1/2 passed (50%), median 700 ms per request")


def test_tools_status_lists_stubs_and_reasons(example_config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = example_config_dir / "tools.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["tools"].append({"name": "groceries", "description": "list", "kind": "python", "service": "nope:add"})
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    assert main(["tools", "status", "--config-dir", str(example_config_dir)]) == 0
    status = capsys.readouterr().out
    assert "home_control" in status and "planned (kind: stub)" in status
    assert "groceries" in status and "auto_stub" in status and "handler `nope:add` not found" in status

    assert main(["config", "check", "--config-dir", str(example_config_dir)]) == 0
    assert "Warning: tool 'groceries' is an automatic stub" in capsys.readouterr().out


def test_status_output_counts() -> None:
    registry = _registry([{"name": "x", "description": "x", "kind": "stub"}])
    assert format_status(registry).endswith("4 tools active, 1 stubs (0 automatic)")


@pytest.mark.skipif(
    os.environ.get("ASSISTANT_LIVE_TESTS") != "1",
    reason="needs LM Studio running with the configured fast model; set ASSISTANT_LIVE_TESTS=1",
)
def test_live_tools_test_on_example_phrases(example_config_dir: Path, live_api_key: None) -> None:
    results = run(run_tool_tests(load_config(example_config_dir)))
    print(format_report(results))
    assert sum(result.passed for result in results) / len(results) >= 0.9


def test_message_sent_mid_request_waits_until_it_settles() -> None:
    from pipecat.tests.utils import run_test

    async def scenario():
        state = SessionState()
        runtime = ToolRuntime(_registry([]), state, notify=_ignore)
        preprocessor = TurnPreprocessor(state, runtime, TimerService(TimersConfig()))
        state.begin_request()  # a request is already waiting on the model
        down, _ = await run_test(preprocessor, frames_to_send=[_user("second")])
        held = [f for f in down if isinstance(f, LLMMessagesAppendFrame)]
        state.awaiting_llm = False  # the first request settles
        assert state.idle()
        return held, list(preprocessor._waiting)

    held, waiting = run(scenario())
    assert held == []
    assert len(waiting) == 1 and waiting[0].messages[0]["content"] == "second"


def test_request_is_busy_until_model_and_tools_finish() -> None:
    state = SessionState()
    state.begin_request()
    assert not state.idle()
    state.awaiting_llm, state.llm_busy = False, 1
    assert not state.idle()
    state.llm_busy, state.tools_busy = 0, 1
    assert not state.idle()
    state.tools_busy = 0
    assert state.idle()
