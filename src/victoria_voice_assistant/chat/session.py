from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket
from pipecat.frames.frames import (
    LLMMessagesAppendFrame,
    LLMRunFrame,
    LLMSetToolsFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
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
from victoria_voice_assistant.agents.tasks import TaskEvent
from victoria_voice_assistant.dashboard import dashboard
from victoria_voice_assistant.notifications.service import Announcement, Notification
from victoria_voice_assistant.tools.timers import TimerEvent

logger = logging.getLogger(__name__)

# Task changes arriving within this long reach the dashboard as one update.
DASHBOARD_DEBOUNCE_S = 0.3


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


def notification_view(item: Notification) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "kind": item.kind,
        "headline": item.headline,
        "full": item.full,
        "priority": item.priority,
        "state": item.state,
        "task_id": item.task_id,
        "created_at": item.created_at,
    }


class ChatOutlet:
    """The notification service's view of a chat connection: a screen, with no speaker."""

    speaks = False

    def __init__(self, state: SessionState, session: ChatSession) -> None:
        self.state = state
        self.session = session
        self.device = state.device

    def busy(self) -> bool:
        return not self.state.idle()

    def speaking(self) -> bool:
        return False

    def requests(self) -> int:
        return self.state.requests

    async def announce(self, announcement: Announcement) -> None:
        task = self.session.task
        if task is None:
            return
        if announcement.question is not None and announcement.question.kind == "answer":
            self.state.set_pending(announcement.question)  # the next message typed is the agent's answer
        # In the conversation too, so "what was that about?" has something to refer to.
        await task.queue_frames(
            [
                LLMMessagesAppendFrame(messages=[{"role": "assistant", "content": announcement.text}], run_llm=False),
                OutputTransportMessageUrgentFrame(
                    message={
                        "type": "announce",
                        "text": announcement.text,
                        "sound": announcement.sound,
                        "spoken": announcement.spoken,
                        "items": [notification_view(item) for item in announcement.full],
                    }
                ),
            ]
        )

    async def show(self, unread: list[Notification]) -> None:
        await self.session.send({"type": "notifications", "items": [notification_view(item) for item in unread]})


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
    preprocessor = TurnPreprocessor(
        state, runtime, services.timers, agents=services.agents, notifications=services.notifications
    )
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
    # No idle timeout: Pipecat counts only voice activity frames, which text chat never produces,
    # so its default would close every chat 5 minutes after it opened. The websocket ends the session.
    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=False),
        enable_rtvi=False,
        idle_timeout_secs=None,
    )
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

    dashboard_due: list[asyncio.TimerHandle] = []

    def push_dashboard() -> None:
        """Send the dashboard soon; changes arriving together are sent once."""
        if dashboard_due:
            return
        loop = asyncio.get_running_loop()

        async def send() -> None:
            dashboard_due.clear()
            await session.send(dashboard(services))

        def fire() -> None:
            pending = loop.create_task(send())
            session._tasks.add(pending)
            pending.add_done_callback(session._tasks.discard)

        dashboard_due.append(loop.call_later(DASHBOARD_DEBOUNCE_S, fire))

    async def on_health(_results) -> None:
        push_dashboard()

    async def on_task_event(event: TaskEvent) -> None:
        if event.kind == "changed":
            push_dashboard()  # every task, from any device
            return
        # An agent's result, delay notice or error, for this device: shown as an assistant reply
        # and added to the conversation so follow-up questions can refer to it.
        if event.kind != "say" or event.task.device != state.device or not event.text:
            return
        text = event.full or event.text  # chat shows the whole reply; voice speaks the short one
        await task.queue_frames(
            [
                LLMMessagesAppendFrame(messages=[{"role": "assistant", "content": text}], run_llm=False),
                OutputTransportMessageFrame(message={"type": "token", "text": text}),
                OutputTransportMessageFrame(message={"type": "done"}),
            ]
        )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client) -> None:
        await session.send({"type": "timers", "timers": services.timers.snapshot(state.device)})
        await session.send(dashboard(services))
        ringing = [timer.name for timer in services.timers.ringing(state.device)]
        if ringing:
            await session.send({"type": "timer_ring", "names": ringing, "text": None})

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client) -> None:
        await task.cancel(reason="client disconnected")

    outlet = ChatOutlet(state, session)
    services.timers.add_listener(on_timer_event)
    if services.agents is not None:
        services.agents.tasks.add_listener(on_task_event)
    if services.notifications is not None:
        services.notifications.attach(outlet)
    if services.health is not None:
        services.health.add_listener(on_health)
    if hub is not None:
        hub.sessions.add(session)
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        for handle in dashboard_due:
            handle.cancel()
        if services.health is not None:
            services.health.remove_listener(on_health)
        if services.notifications is not None:
            services.notifications.detach(outlet)
        services.timers.remove_listener(on_timer_event)
        if services.agents is not None:
            services.agents.tasks.remove_listener(on_task_event)
        if hub is not None:
            hub.sessions.discard(session)
