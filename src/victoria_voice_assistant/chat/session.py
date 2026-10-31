from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket
from pipecat.frames.frames import LLMRunFrame, LLMSetToolsFrame, OutputTransportMessageUrgentFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from victoria_voice_assistant.chat.llm import build_router
from victoria_voice_assistant.chat.prompt import system_prompt
from victoria_voice_assistant.chat.serializer import ChatJsonSerializer, ChatProtocolProcessor
from victoria_voice_assistant.chat.timing import RequestTimer, TimingEndProcessor, TimingStartProcessor
from victoria_voice_assistant.chat.tools import SessionState, ToolRuntime
from victoria_voice_assistant.chat.turns import TurnPreprocessor, TurnTracker
from victoria_voice_assistant.config.schema import AppConfig
from victoria_voice_assistant.tools.registry import Services, build_registry
from victoria_voice_assistant.tools.timers import TimerEvent

logger = logging.getLogger(__name__)


class ChatHub:
    """The open chat sessions, so config reloads reach all of them."""

    def __init__(self) -> None:
        self.sessions: set[ChatSession] = set()

    def reload(self, config: AppConfig, services: Services) -> None:
        for session in list(self.sessions):
            session.reload(config, services)


class ChatSession:
    def __init__(self, state: SessionState, config: AppConfig, services: Services) -> None:
        self.state = state
        self.runtime = ToolRuntime(build_registry(config.tools, config.agents, services), state, notify=self.send)
        self.task: PipelineTask | None = None
        self._tasks: set[asyncio.Task] = set()

    async def send(self, message: dict[str, Any]) -> None:
        if self.task is not None:
            await self.task.queue_frame(OutputTransportMessageUrgentFrame(message=message))

    def reload(self, config: AppConfig, services: Services) -> None:
        self.runtime.registry = build_registry(config.tools, config.agents, services)
        if self.task is None:
            return
        task = asyncio.get_running_loop().create_task(
            self.task.queue_frame(LLMSetToolsFrame(tools=self.runtime.tools_schema()))
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("Chat tools reloaded", extra={"event": "tools_reloaded", "count": len(self.runtime.registry.active)})


async def run_chat_session(
    websocket: WebSocket,
    config: AppConfig,
    services: Services,
    hub: ChatHub | None = None,
) -> None:
    """Run one text chat connection through the LLM router and fallback, with tools."""
    models = config.models
    router = build_router(models)
    state = SessionState()
    session = ChatSession(state, config, services)
    runtime = session.runtime

    def next_llm_call() -> int:
        state.llm_calls += 1
        state.llm_busy += 1
        state.awaiting_llm = False
        return state.llm_calls

    timer = RequestTimer(on_llm_call=next_llm_call)
    timer.model = models.roles.router

    @router.strategy.event_handler("on_service_switched")
    async def on_service_switched(_strategy, service) -> None:
        timer.model = service.name
        logger.warning(
            "LLM failover",
            extra={"event": "llm_failover", "active_role": service.name},
        )
        await task.queue_frame(LLMRunFrame())

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(serializer=ChatJsonSerializer()),
    )
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt(runtime.registry)}],
        tools=runtime.tools_schema(),
    )
    pair = LLMContextAggregatorPair(context)
    preprocessor = TurnPreprocessor(state, runtime, services.timers)
    runtime.on_settled = preprocessor.release_next
    pipeline = Pipeline(
        [
            transport.input(),
            preprocessor,
            pair.user(),
            TimingStartProcessor(timer),
            router,
            TurnTracker(state, preprocessor),
            TimingEndProcessor(timer),
            ChatProtocolProcessor(),
            transport.output(),
            pair.assistant(),
        ]
    )
    # RTVI messages would reach the chat client through the urgent transport-message path.
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=False), enable_rtvi=False)
    session.task = task

    async def on_timer_event(event: TimerEvent) -> None:
        if event.device != state.device:
            return
        if event.kind == "changed":
            await session.send({"type": "timers", "timers": services.timers.snapshot(state.device)})
        elif event.kind == "ring":
            await session.send({"type": "timer_ring", "names": event.names or [], "text": event.text})
        elif event.kind == "ring_stopped":
            await session.send({"type": "timer_ring_stop", "names": event.names or []})

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client) -> None:
        await session.send({"type": "timers", "timers": services.timers.snapshot(state.device)})
        ringing = [timer.name for timer in services.timers.ringing(state.device)]
        if ringing:
            await session.send({"type": "timer_ring", "names": ringing, "text": None})

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client) -> None:
        await task.cancel(reason="client disconnected")

    services.timers.add_listener(on_timer_event)
    if hub is not None:
        hub.sessions.add(session)
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        services.timers.remove_listener(on_timer_event)
        if hub is not None:
            hub.sessions.discard(session)
