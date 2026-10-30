from __future__ import annotations

import logging

from fastapi import WebSocket
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from victoria_voice_assistant.chat.llm import build_router
from victoria_voice_assistant.chat.serializer import ChatJsonSerializer, ChatProtocolProcessor
from victoria_voice_assistant.chat.timing import RequestTimer, TimingEndProcessor, TimingStartProcessor
from victoria_voice_assistant.config.schema import ModelsConfig

logger = logging.getLogger(__name__)


async def run_chat_session(websocket: WebSocket, models: ModelsConfig) -> None:
    """Run one text chat connection through the LLM router and fallback."""
    router = build_router(models)
    timer = RequestTimer()
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
    pair = LLMContextAggregatorPair(LLMContext())
    pipeline = Pipeline(
        [
            transport.input(),
            pair.user(),
            TimingStartProcessor(timer),
            router,
            TimingEndProcessor(timer),
            ChatProtocolProcessor(),
            transport.output(),
            pair.assistant(),
        ]
    )
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=False))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client) -> None:
        await task.cancel(reason="client disconnected")

    await PipelineRunner(handle_sigint=False).run(task)
