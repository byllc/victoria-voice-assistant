from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from victoria_voice_assistant.agents.model_agent import AgentError, AgentReply
from victoria_voice_assistant.agents.pools import PoolChooser, split_override
from victoria_voice_assistant.agents.runner import AgentRunner, looks_like_refusal
from victoria_voice_assistant.agents.spend import SpendEntry, SpendLog, cost_usd
from victoria_voice_assistant.agents.tasks import TaskEvent, TaskService
from victoria_voice_assistant.chat.llm import build_llm
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import AppConfig, ModelsConfig, TimersConfig, WeatherConfig
from victoria_voice_assistant.decider.engine import Decision
from victoria_voice_assistant.storage.db import migrate
from victoria_voice_assistant.tools.registry import Services
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

HAIKU, SONNET, OPUS = "claude-haiku", "claude-sonnet", "claude-opus"


def run(coro):
    return asyncio.run(coro)


class FakeDecider:
    def __init__(self, choice: str | None, confidence: float) -> None:
        self.decision = Decision(choice=choice, confidence=confidence)
        self.seen: list[dict[str, str]] = []

    async def decide(self, state: str, question: str, options: dict[str, str]) -> Decision:
        self.seen.append(options)
        return self.decision


@pytest.fixture
def config(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    return load_config(example_config_dir)


def _spent(spend: SpendLog, dollars: float) -> None:
    spend.record(SpendEntry(agent="claude", model=SONNET, input_tokens=0, output_tokens=0, cost_usd=dollars, pool="claude"))


def test_split_override_picks_the_member_and_strips_the_word(config: AppConfig) -> None:
    pool = config.models.pools["claude"]
    assert split_override(pool, "Opus to review this lease clause") == (OPUS, "review this lease clause")
    assert split_override(pool, "haiku, how many cups in a quart") == (HAIKU, "how many cups in a quart")
    assert split_override(pool, "write a haiku about leeks") == (None, "write a haiku about leeks")


@pytest.mark.parametrize(
    ("decided", "confidence", "spent", "override", "expected"),
    [
        (HAIKU, 0.92, 0, None, (HAIKU, "decider")),
        (HAIKU, 0.53, 0, None, (SONNET, "low_confidence")),  # below min_confidence: the pool default
        (None, 0.0, 0, None, (SONNET, "low_confidence")),  # decider down or off-list
        (HAIKU, 0.9, 0, OPUS, (OPUS, "override")),  # a spoken override wins over the decider
        (OPUS, 0.9, 25, None, (SONNET, "budget_downgrade")),  # past 80%: nothing above the default
        (HAIKU, 0.9, 25, None, (HAIKU, "decider")),  # cheaper than the default is still fine
        (HAIKU, 0.9, 25, OPUS, (SONNET, "budget_downgrade")),  # the budget applies to overrides too
        (HAIKU, 0.9, 30, None, (None, "budget_stop")),  # at 100%: no paid call
    ],
)
def test_pool_choice_order(config: AppConfig, decided, confidence, spent, override, expected) -> None:
    spend = SpendLog(None)
    _spent(spend, spent)
    decider = FakeDecider(decided, confidence)
    chooser = PoolChooser(config.models, decider, spend)  # type: ignore[arg-type]
    choice = run(chooser.choose("claude", "a request", override))
    assert (choice.model, choice.reason) == expected
    if override is None:
        assert list(decider.seen[0]) == [HAIKU, SONNET, OPUS]  # each member with its good_for line


def test_escalation_steps_up_once_within_the_budget(config: AppConfig) -> None:
    spend = SpendLog(None)
    chooser = PoolChooser(config.models, None, spend)
    assert chooser.escalate("claude", HAIKU) == SONNET
    assert chooser.escalate("claude", SONNET) == OPUS
    assert chooser.escalate("claude", OPUS) is None
    _spent(spend, 25)
    assert chooser.escalate("claude", SONNET) is None  # past downgrade_at, nothing above the default


def test_cost_and_monthly_totals(config: AppConfig, tmp_path: Path) -> None:
    assert cost_usd(config.models.models[OPUS], 1_000_000, 100_000) == pytest.approx(4.0 + 2.0)
    assert cost_usd(config.models.models["fast"], 1_000_000, 1_000_000) == 0.0  # local models have no price
    database = migrate(tmp_path / "a.sqlite3")
    now = [1_790_000_000.0]
    spend = SpendLog(database, clock=lambda: now[0])
    _spent(spend, 1.25)
    _spent(spend, 0.5)
    assert spend.month_total("claude") == pytest.approx(1.75)
    now[0] += 40 * 86400  # next month starts from zero
    assert spend.month_total("claude") == 0.0


def test_refusal_detection() -> None:
    assert looks_like_refusal("I can't help with booking flights.")
    assert looks_like_refusal("Sorry, I'm unable to do that.")
    assert not looks_like_refusal("I can tell you the opening hours: 10 to 5.")
    assert not looks_like_refusal("Leeks are great in soup; I can't stress that enough.")


def _runner(config: AppConfig, replies: dict[str, object], decider=None, spend: SpendLog | None = None):
    services = Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()))
    tasks = TaskService(None)
    events: list[TaskEvent] = []

    async def listen(event: TaskEvent) -> None:
        events.append(event)

    tasks.add_listener(listen)
    agent_runner = AgentRunner(config, services, tasks, spend or SpendLog(None))
    services.agents = agent_runner
    agent_runner.chooser.decider = decider
    calls: list[str] = []

    async def fake_call_model(agent, task, model, tools, history=None) -> AgentReply:
        calls.append(model)
        reply = replies[model]
        if isinstance(reply, Exception):
            raise reply
        return reply  # type: ignore[return-value]

    agent_runner._call_model = fake_call_model  # type: ignore[method-assign]
    return agent_runner, events, calls


async def _settle(agent_runner: AgentRunner) -> None:
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not agent_runner.tasks.active():
            return


def test_ask_claude_uses_the_decider_and_logs_spend(config: AppConfig, tmp_path: Path) -> None:
    database = migrate(tmp_path / "a.sqlite3")

    async def scenario():
        spend = SpendLog(database)
        agent_runner, events, calls = _runner(
            config, {HAIKU: AgentReply("Four cups.", 900, 10)}, decider=FakeDecider(HAIKU, 0.92), spend=spend
        )
        outcome = await agent_runner.ask("claude", "how many cups are in a quart", "web")
        await _settle(agent_runner)
        return outcome, events, calls, agent_runner

    outcome, events, calls, agent_runner = run(scenario())
    assert outcome.speak == "Asking Claude." and calls == [HAIKU]
    task = agent_runner.tasks.get(outcome.result["task_id"])
    assert task.model == HAIKU and task.cost_usd == pytest.approx((900 * 0.10 + 10 * 0.50) / 1e6)
    assert [e.text for e in events if e.kind == "say"] == ["Four cups."]
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT pool, model, reason, decider_choice, confidence, input_tokens FROM spend").fetchone()
    assert row == ("claude", HAIKU, "decider", HAIKU, 0.92, 900)


def test_spoken_override_strips_the_word_and_skips_the_decider(config: AppConfig) -> None:
    async def scenario():
        decider = FakeDecider(HAIKU, 0.99)
        agent_runner, _, calls = _runner(config, {OPUS: AgentReply("Careful answer.")}, decider=decider)
        outcome = await agent_runner.ask("claude", "Opus to review this lease clause", "web")
        await _settle(agent_runner)
        return agent_runner.tasks.get(outcome.result["task_id"]), calls, decider.seen

    task, calls, seen = run(scenario())
    assert calls == [OPUS] and seen == [] and task.request == "review this lease clause"


def test_a_decline_or_failure_escalates_once(config: AppConfig) -> None:
    async def scenario(replies):
        agent_runner, events, calls = _runner(config, replies, decider=FakeDecider(HAIKU, 0.9))
        await agent_runner.ask("claude", "plan a dinner party", "web")
        await _settle(agent_runner)
        return calls, [e.text for e in events if e.kind == "say"]

    declined = run(scenario({HAIKU: AgentReply("I can't plan parties."), SONNET: AgentReply("Here's a plan.")}))
    assert declined == ([HAIKU, SONNET], ["Here's a plan."])
    failed = run(scenario({HAIKU: AgentError("overloaded"), SONNET: AgentError("overloaded"), OPUS: AgentReply("x")}))
    assert failed == ([HAIKU, SONNET], ["Claude couldn't finish that."])  # one step up, not two


def test_budget_stop_offers_the_default_agent(config: AppConfig) -> None:
    async def scenario():
        spend = SpendLog(None)
        _spent(spend, 30)
        agent_runner, _, calls = _runner(config, {}, spend=spend)
        return await agent_runner.ask("claude", "plan the week", "web"), calls

    outcome, calls = run(scenario())
    assert calls == []
    assert outcome.speak == "We've hit this month's Claude budget. Want me to ask Helper?"
    assert outcome.pending is not None and outcome.pending.on_yes == ("ask_agent", {"agent": "helper", "task": "plan the week"})


def test_claude_without_a_key_says_it_is_not_set_up(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = load_config(example_config_dir)  # loads: the key is optional (${ANTHROPIC_API_KEY:-})

    async def scenario():
        agent_runner, _, calls = _runner(config, {})
        return await agent_runner.ask("claude", "anything", "web"), calls

    outcome, calls = run(scenario())
    assert calls == [] and outcome.speak == "Claude isn't set up yet."
    assert "assistant secrets set" in (outcome.chat or "") and "docs/claude-setup.md" in (outcome.chat or "")


def test_anthropic_models_build_pipecats_anthropic_service(config: AppConfig) -> None:
    from pipecat.services.anthropic.llm import AnthropicLLMService

    llm = build_llm(OPUS, config.models)
    assert isinstance(llm, AnthropicLLMService)
    assert llm._settings.model == "claude-opus-5-5" and llm._settings.max_tokens == 1024


def test_optional_environment_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from victoria_voice_assistant.config.loader import ConfigError, substitute_environment

    monkeypatch.delenv("MISSING_THING", raising=False)
    assert substitute_environment("${MISSING_THING:-}", "x.yaml") == ""
    assert substitute_environment("${MISSING_THING:-fallback}", "x.yaml") == "fallback"
    with pytest.raises(ConfigError):
        substitute_environment("${MISSING_THING}", "x.yaml")
