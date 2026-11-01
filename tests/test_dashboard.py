from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from victoria_voice_assistant.agents.model_agent import AgentReply
from victoria_voice_assistant.agents.runner import AgentRunner
from victoria_voice_assistant.agents.spend import SpendEntry, SpendLog
from victoria_voice_assistant.agents.tasks import TaskService
from victoria_voice_assistant.chat.tools import SessionState
from victoria_voice_assistant.chat.turns import TurnPreprocessor
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import AppConfig, NotificationsConfig, TimersConfig, WeatherConfig
from victoria_voice_assistant.dashboard import dashboard
from victoria_voice_assistant.decider.engine import Decision
from victoria_voice_assistant.health import HealthMonitor
from victoria_voice_assistant.notifications.service import NotificationService
from victoria_voice_assistant.storage.db import migrate
from victoria_voice_assistant.tools.registry import Services
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService


class FakeDecider:
    async def decide(self, state, question, options) -> Decision:
        return Decision(choice="claude-sonnet", confidence=0.82)


@pytest.fixture
def config(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    return load_config(example_config_dir)


def _runner(config: AppConfig, database: Path | None = None, reply: str = "Done.", fail: bool = False):
    notifications = NotificationService(NotificationsConfig())
    services = Services(
        timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()), notifications=notifications
    )
    tasks = TaskService(database)
    runner = AgentRunner(config, services, tasks, SpendLog(database))
    runner.chooser.decider = FakeDecider()  # type: ignore[assignment]
    services.agents = runner
    calls: list[str] = []

    async def fake_call_model(agent, task, model, tools, history=None) -> AgentReply:
        calls.append(model)
        if fail:
            raise RuntimeError("overloaded")
        return AgentReply(reply, 1200, 300)

    runner._call_model = fake_call_model  # type: ignore[method-assign]
    return runner, services, calls


async def _finish(runner: AgentRunner) -> None:
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not runner.tasks.active():
            return


def test_progress_is_recorded_and_kept(config: AppConfig, tmp_path: Path) -> None:
    database = migrate(tmp_path / "a.sqlite3")

    async def scenario():
        runner, _, _ = _runner(config, database)
        await runner.ask("claude", "plan dinners", "kitchen")
        await _finish(runner)
        reloaded = TaskService(database)
        reloaded.start()
        return reloaded.recent()

    [task] = asyncio.run(scenario())
    assert [text for _, text in task.progress] == ["Sonnet: picked by the decider (82% sure)", "Finished"]
    assert task.state == "finished" and task.device == "kitchen"  # finished tasks are history after a restart


def test_dashboard_snapshot(config: AppConfig) -> None:
    async def scenario():
        runner, services, _ = _runner(config)
        runner.spend.record(SpendEntry(agent="claude", model="claude-sonnet", input_tokens=0, output_tokens=0, cost_usd=4.5, pool="claude"))
        await runner.ask("claude", "plan dinners", "web")
        await _finish(runner)
        hermes = runner.tasks.create("hermes", "web", "write a story", "auto", model="hermes")
        return dashboard(services), hermes

    board, hermes = asyncio.run(scenario())
    assert board["type"] == "dashboard"
    running, finished = board["tasks"]
    assert running["id"] == hermes.id and running["label"] == "Hermes"
    assert running["cancel_note"] == "Stops the reply; Hermes may finish work it already started."
    assert finished["label"] == "Claude (Sonnet)" and finished["result"] == "Done."
    assert finished["cost_usd"] > 0 and finished["input_tokens"] == 1200
    assert finished["resend"] == {"agents": ["helper", "hermes"], "models": ["claude-opus"]}
    assert board["summary"]["running"] == 1 and board["summary"]["waiting"] == 0
    [spend] = board["summary"]["spend"]
    assert spend["pool"] == "claude" and spend["budget_usd"] == 30 and spend["used_usd"] > 4.5


def test_retry_and_resend(config: AppConfig) -> None:
    async def scenario():
        runner, _, calls = _runner(config)
        first = await runner.ask("claude", "plan dinners", "web")
        await _finish(runner)
        task_id = first.result["task_id"]
        retried = await runner.retry(task_id)
        await _finish(runner)
        stronger = await runner.retry(task_id, model="claude-opus")
        await _finish(runner)
        elsewhere = await runner.retry(task_id, agent="helper")
        await _finish(runner)
        bad = await runner.retry(task_id, model="hermes")
        return calls, [o.speak for o in (retried, stronger, elsewhere, bad)], runner

    calls, said, runner = asyncio.run(scenario())
    assert calls == ["claude-sonnet", "claude-sonnet", "claude-opus", "fast"]
    assert said == ["Asking Claude.", "Asking Claude.", "Asking Helper.", "That model isn't available."]
    assert {t.request for t in runner.tasks.recent()} == {"plan dinners"}


def test_dashboard_buttons_reach_the_runner(config: AppConfig) -> None:
    async def scenario():
        runner, services, calls = _runner(config)
        preprocessor = TurnPreprocessor(
            SessionState(), None, services.timers, agents=runner, notifications=services.notifications  # type: ignore[arg-type]
        )
        notes: list[str] = []

        async def note(text: str) -> None:
            notes.append(text)

        preprocessor._deliver_note = note  # type: ignore[method-assign]
        slow = runner.tasks.create("hermes", "web", "write a story", "auto", model="hermes")
        await preprocessor._task_action({"action": "cancel", "task_id": slow.id})
        first = await runner.ask("helper", "what goes with leeks", "web")
        await _finish(runner)
        services.notifications.post("helper", "result", "Helper has finished.", "Potatoes.", None, "web", task_id=first.result["task_id"])
        await preprocessor._task_action({"action": "open", "task_id": first.result["task_id"]})
        await preprocessor._task_action({"action": "resend", "task_id": first.result["task_id"], "agent": "claude"})
        await _finish(runner)
        return notes, slow, services.notifications.unread(), calls

    notes, slow, unread, calls = asyncio.run(scenario())
    assert notes == ["Cancelled Hermes's task.", "Asking Claude."] and slow.state == "cancelled"
    assert unread == []  # opening the result read its notification
    assert calls == ["fast", "claude-sonnet"]


def test_the_serializer_passes_dashboard_actions() -> None:
    from victoria_voice_assistant.chat.serializer import ChatJsonSerializer

    serializer = ChatJsonSerializer()

    async def scenario():
        good = await serializer.deserialize('{"type": "task_action", "action": "resend", "task_id": "t1", "model": "claude-opus", "x": 1}')
        bad = await serializer.deserialize('{"type": "task_action", "action": "delete", "task_id": "t1"}')
        return good, bad

    good, bad = asyncio.run(scenario())
    assert good.message == {"type": "task_action", "action": "resend", "task_id": "t1", "model": "claude-opus"}
    assert bad is None


def test_health_targets_and_checks(config: AppConfig) -> None:
    monitor = HealthMonitor(config, clock=lambda: 100.0)
    targets = {name: (url, note) for name, url, _headers, note in monitor.targets()}
    assert targets["lmstudio"][0].endswith("/v1/models")
    assert targets["hermes"][0] == "http://127.0.0.1:8642/v1/models"
    assert targets["anthropic"] == (None, "key set")  # cloud servers aren't called
    assert targets["searxng"][0].endswith("/healthz")

    def respond(request: httpx.Request) -> httpx.Response:
        if "8642" in str(request.url):
            raise httpx.ConnectError("refused")
        if "1234" in str(request.url):
            return httpx.Response(401)
        return httpx.Response(200)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return {
                name: await monitor._check_one(client, name, url, headers, note)
                for name, url, headers, note in monitor.targets()
            }

    results = asyncio.run(scenario())
    assert (results["hermes"].ok, results["hermes"].detail) == (False, "ConnectError")
    assert (results["lmstudio"].ok, results["lmstudio"].detail) == (False, "rejected the key")
    assert results["searxng"].ok is True and results["anthropic"].ok is None
