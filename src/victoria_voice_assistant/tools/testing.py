"""`assistant tools test`: send each example phrase to the router model once and check what it does."""

from __future__ import annotations

import dataclasses
import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI
from pipecat.services.openai.llm import OpenAILLMService

from victoria_voice_assistant.chat.prompt import system_prompt
from victoria_voice_assistant.config.schema import AppConfig, ToolTestGiven
from victoria_voice_assistant.notifications.service import NotificationService
from victoria_voice_assistant.tools.registry import Services, ToolContext, ToolRegistry, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService
from victoria_voice_assistant.search.service import PageReader, SearchService

# Built-in services whose handlers are safe to run in the sandbox, so service-level outcomes
# (a timer asking "which timer?") count. Others (weather, http) are not called.
SANDBOXED_SERVICES = ("timers",)


@dataclass
class PhraseCase:
    say: str
    expect: str
    source: str
    given: ToolTestGiven | None = None


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]

    def __str__(self) -> str:
        args = ", ".join(f"{key}={value!r}" for key, value in self.arguments.items())
        return f"{self.name}({args})"


@dataclass
class CaseResult:
    case: PhraseCase
    passed: bool
    outcomes: list[str] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    text: str | None = None
    ms: float = 0.0
    error: str | None = None


Completion = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Awaitable[tuple[list[ToolCall], str | None]]]


def collect_cases(config: AppConfig, only: str | None = None) -> list[PhraseCase]:
    cases = []
    for tool in config.tools.tools:
        if not tool.enabled or (only and tool.name != only):
            continue
        for phrase in tool.examples:
            cases.append(PhraseCase(say=phrase, expect=f"tool:{tool.name}", source=f"{tool.name}.examples"))
    for index, test in enumerate(config.tools.tests):
        if only and test.expect not in (f"tool:{only}", only):
            continue
        cases.append(PhraseCase(say=test.say, expect=test.expect, source=f"tests[{index}]", given=test.given))
    return cases


def openai_completion(config: AppConfig) -> Completion:
    """One non-streaming request to the router model, with the same params chat uses."""
    model = config.models.models[config.models.roles.router]
    server = config.models.servers[model.server]
    client = AsyncOpenAI(base_url=server.base_url, api_key=server.api_key or "not-required", timeout=model.timeout_s)
    known = {item.name for item in dataclasses.fields(OpenAILLMService.Settings)}
    params = {key: value for key, value in model.params.items() if key in known}
    extra = {key: value for key, value in model.params.items() if key not in known}

    async def complete(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[list[ToolCall], str | None]:
        response = await client.chat.completions.create(
            model=model.model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            extra_body=extra or None,
            **params,
        )
        message = response.choices[0].message
        calls = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except ValueError:
                arguments = {"_unparsed": call.function.arguments}
            calls.append(ToolCall(call.function.name, arguments))
        return calls, message.content

    return complete


async def _sandbox(config: AppConfig, given: ToolTestGiven | None) -> tuple[ToolRegistry, Services]:
    services = Services(
        timers=TimerService(config.tools.timers, database_path=None),
        weather=WeatherService(config.tools.weather),
        search=SearchService(config.tools.search),
        reader=PageReader(config.tools.search.read if config.tools.search else None),
        notifications=NotificationService(config.tools.notifications),
    )
    for name in given.timers if given else []:
        await services.timers.start_seconds("test", name, 600)
    return build_registry(config.tools, config.agents, services), services


async def run_case(case: PhraseCase, config: AppConfig, complete: Completion) -> CaseResult:
    registry, services = await _sandbox(config, case.given)
    messages = [
        {"role": "system", "content": system_prompt(registry)},
        {"role": "user", "content": case.say},
    ]
    tools = [entry.openai_tool() for entry in registry.active]
    started = time.perf_counter()
    try:
        calls, text = await complete(messages, tools)
    except Exception as exc:  # noqa: BLE001 - reported per case
        return CaseResult(case, False, error=f"{type(exc).__name__}: {exc}", ms=(time.perf_counter() - started) * 1000)
    finally:
        await services.timers.close()
    ms = (time.perf_counter() - started) * 1000
    outcomes: list[str] = []
    for call in calls:
        outcomes.extend(await _call_outcomes(call, registry, config))
    if not calls:
        outcomes.append("answer")
    return CaseResult(case, case.expect in outcomes, outcomes=outcomes, calls=calls, text=text, ms=ms)


async def _call_outcomes(call: ToolCall, registry: ToolRegistry, config: AppConfig) -> list[str]:
    if call.name == "clarify":
        return ["clarify"]
    if call.name == "offer_agent":
        return ["offer_agent"]
    if call.name == "ask_agent":
        return [f"agent:{call.arguments.get('agent', '')}"]
    outcomes = [f"tool:{call.name}"]
    entry = registry.get(call.name)
    tool = next((tool for tool in config.tools.tools if tool.name == call.name), None)
    if entry is not None and entry.status == "ready" and tool is not None and tool.service in SANDBOXED_SERVICES:
        outcome = await entry.handler(dict(call.arguments), ToolContext(device="test"))
        if outcome.pending is not None and outcome.pending.kind == "clarify":
            outcomes.append("clarify")
    return outcomes


async def run_tool_tests(
    config: AppConfig,
    complete: Completion | None = None,
    only: str | None = None,
) -> list[CaseResult]:
    completion = complete or openai_completion(config)
    return [await run_case(case, config, completion) for case in collect_cases(config, only)]


def format_report(results: list[CaseResult]) -> str:
    lines = []
    width = max((len(result.case.say) for result in results), default=10) + 2
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        phrase = json.dumps(result.case.say)
        if result.error:
            got = f"error: {result.error}"
        elif result.calls:
            got = "; ".join(str(call) for call in result.calls)
            if "clarify" in result.outcomes and not any(call.name == "clarify" for call in result.calls):
                got += " [tool asked which one]"
        else:
            got = f"answer: {json.dumps((result.text or '').strip()[:80])}"
        line = f"{mark}  {result.case.expect:<18} {phrase:<{width}} -> {got}  ({result.ms:.0f} ms)"
        if not result.passed:
            line += f"\n      expected {result.case.expect}, got {', '.join(result.outcomes) or 'nothing'} [{result.case.source}]"
        lines.append(line)
    passed = sum(result.passed for result in results)
    total = len(results)
    timings = [result.ms for result in results if not result.error]
    summary = f"{passed}/{total} passed ({(passed / total * 100) if total else 0:.0f}%)"
    if timings:
        summary += f", median {statistics.median(timings):.0f} ms per request"
    lines.append("")
    lines.append(summary)
    return "\n".join(lines)


def format_status(registry: ToolRegistry) -> str:
    rows = [("TOOL", "KIND", "STATUS", "REPLY", "NOTE")]
    for entry in registry.entries:
        note = entry.reason or ""
        if entry.status == "stub" and not note:
            note = "planned (kind: stub)"
        rows.append((entry.name, entry.kind, entry.status, "direct" if entry.speaks_directly else entry.reply, note))
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(row[:4])) + "  " + row[4] for row in rows]
    stubs = registry.stubs()
    lines.append("")
    lines.append(f"{len(registry.active)} tools active, {len(stubs)} stubs ({sum(e.status == 'auto_stub' for e in stubs)} automatic)")
    return "\n".join(line.rstrip() for line in lines)
