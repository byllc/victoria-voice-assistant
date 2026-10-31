"""Harness agents (Hermes) against a stand-in OpenAI-compatible server, as Hermes's gateway would be."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from victoria_voice_assistant.agents.runner import AgentRunner
from victoria_voice_assistant.agents.spend import SpendLog
from victoria_voice_assistant.agents.tasks import TaskEvent, TaskService
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import AppConfig, ModelsConfig, TimersConfig, WeatherConfig
from victoria_voice_assistant.tools.registry import Services
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService


class StandIn:
    """Records requests; streams a reply, or one chunk and then nothing (to test cancelling)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.auth: list[str | None] = []
        self.slow = False
        self.closed_early = threading.Event()

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def completions(request: Request) -> StreamingResponse:
            body = await request.json()
            self.requests.append(body)
            self.auth.append(request.headers.get("authorization"))

            def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
                data = {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                return f"data: {json.dumps(data)}\n\n"

            async def stream():
                try:
                    yield chunk({"role": "assistant", "content": ""})
                    if self.slow:
                        yield chunk({"content": "Working on"})
                        await asyncio.sleep(30)  # Hermes busy running tools
                    for word in ["Your ", "meal ", "plan ", "is ", "ready."]:
                        yield chunk({"content": word})
                    yield chunk({}, "stop")
                    yield "data: [DONE]\n\n"
                except asyncio.CancelledError:
                    self.closed_early.set()  # the client closed the stream
                    raise

            return StreamingResponse(stream(), media_type="text/event-stream")

        return app


@pytest.fixture
def stand_in():
    server_state = StandIn()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(server_state.app(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield server_state, port
    server.should_exit = True
    thread.join(timeout=5)


def _config(example_config_dir: Path, port: int, key: str | None = "test-hermes-key") -> AppConfig:
    config = load_config(example_config_dir)
    data = config.models.model_dump()
    data["servers"]["hermes"]["base_url"] = f"http://127.0.0.1:{port}/v1"
    data["servers"]["hermes"]["api_key"] = key
    return config.model_copy(update={"models": ModelsConfig.model_validate(data)})


def _runner(config: AppConfig) -> tuple[AgentRunner, list[TaskEvent]]:
    services = Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()))
    tasks = TaskService(None)
    events: list[TaskEvent] = []

    async def listen(event: TaskEvent) -> None:
        events.append(event)

    tasks.add_listener(listen)
    agent_runner = AgentRunner(config, services, tasks, SpendLog(None))
    services.agents = agent_runner
    return agent_runner, events


async def _settle(agent_runner: AgentRunner, seconds: float = 10) -> None:
    for _ in range(int(seconds * 20)):
        await asyncio.sleep(0.05)
        if not agent_runner.tasks.active():
            await asyncio.sleep(0.05)
            return


def test_ask_hermes_streams_from_its_gateway(example_config_dir: Path, stand_in) -> None:
    server, port = stand_in

    async def scenario():
        agent_runner, events = _runner(_config(example_config_dir, port))
        outcome = await agent_runner.ask("herms", "plan dinners for the week", "web")
        await _settle(agent_runner)
        return outcome, events, agent_runner

    outcome, events, agent_runner = asyncio.run(scenario())
    assert outcome.speak == "Asking Hermes."
    assert [e.text for e in events if e.kind == "say"] == ["Your meal plan is ready."]
    task = agent_runner.tasks.get(outcome.result["task_id"])
    assert task.state == "finished" and task.model == "hermes"
    request = server.requests[0]
    assert request["model"] == "hermes-agent" and request["stream"] is True
    assert request["messages"] == [{"role": "user", "content": "plan dinners for the week"}]  # the request only
    assert "tools" not in request  # Hermes runs its own tools
    assert server.auth[0] == "Bearer test-hermes-key"


def test_cancelling_hermes_closes_the_request_stream(example_config_dir: Path, stand_in) -> None:
    server, port = stand_in
    server.slow = True

    async def scenario():
        agent_runner, events = _runner(_config(example_config_dir, port))
        outcome = await agent_runner.ask("hermes", "plan a trip", "web")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if server.requests:
                break
        await asyncio.sleep(0.3)
        assert agent_runner.tasks.cancel(outcome.result["task_id"])
        await _settle(agent_runner)
        closed = await asyncio.to_thread(server.closed_early.wait, 5)
        return agent_runner.tasks.get(outcome.result["task_id"]), events, closed

    task, events, closed = asyncio.run(scenario())
    assert task.state == "cancelled" and closed
    assert [e for e in events if e.kind == "say"] == []


def test_unreachable_or_keyless_hermes(example_config_dir: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]  # nothing listens here once the probe closes

    async def scenario():
        down, down_events = _runner(_config(example_config_dir, dead_port))
        await down.ask("hermes", "x", "web")
        await _settle(down, seconds=20)
        keyless, _ = _runner(_config(example_config_dir, dead_port, key=None))
        return [e.text for e in down_events if e.kind == "say"], await keyless.ask("hermes", "x", "web")

    said, keyless = asyncio.run(scenario())
    assert said == ["I can't reach Hermes right now."]
    assert keyless.speak == "Hermes isn't set up yet." and "docs/hermes.md" in (keyless.chat or "")
