"""Browser voice over SmallWebRTC: Silero VAD + Smart Turn -> Whisper -> router + tools -> Kokoro."""

from __future__ import annotations

import logging
import asyncio
from typing import Any

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import Frame, LLMContextFrame, LLMRunFrame, TTSSpeakFrame
from pipecat.observers.user_bot_latency_observer import LatencyBreakdown, UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy, TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from victoria_voice_assistant.chat.llm import build_router
from victoria_voice_assistant.chat.prompt import system_prompt
from victoria_voice_assistant.chat.session import ChatHub, ChatSession
from victoria_voice_assistant.chat.timing import RequestTimer, TimingEndProcessor, TimingStartProcessor
from victoria_voice_assistant.chat.tools import SessionState
from victoria_voice_assistant.chat.turns import TurnPreprocessor
from victoria_voice_assistant.config.schema import AppConfig, SpeechConfig
from victoria_voice_assistant.tools.outcome import VOICE_PENDING_S
from victoria_voice_assistant.tools.registry import Services
from victoria_voice_assistant.tools.timers import TimerEvent
from victoria_voice_assistant.voice.models import SpeechModels

logger = logging.getLogger(__name__)

# A session whose browser never completes the WebRTC connection is ended after this long.
CONNECT_TIMEOUT_S = 30.0


class VoiceTurnPreprocessor(TurnPreprocessor):
    """Voice version: sits after the user aggregator, so the transcript is already in the context.

    Stop and yes/no turns are answered by speaking directly; every other turn's
    context frame goes on to the model. Overlapping speech is handled by Pipecat's
    own interruptions, so there is no queue here.
    """

    def __init__(self, *args, context: LLMContext, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.context = context

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await FrameProcessor.process_frame(self, frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMContextFrame):
            text = last_user_text(frame.context)
            if text is not None:
                logger.info("Voice transcript", extra={"event": "voice_transcript", "text": text})
                if await self._handled(text):
                    return
        await self.push_frame(frame, direction)

    async def _deliver(self, user_text: str, reply: str) -> None:
        self.context.add_message({"role": "assistant", "content": reply})
        await self.push_frame(TTSSpeakFrame(reply, append_to_context=False))


def last_user_text(context: LLMContext) -> str | None:
    for message in reversed(context.get_messages()):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()
        return None
    return None


def user_params(speech: SpeechConfig) -> LLMUserAggregatorParams:
    vad = SileroVADAnalyzer(
        params=VADParams(confidence=speech.vad.confidence, start_secs=speech.vad.start_s, stop_secs=speech.vad.stop_s)
    )
    if speech.turn.smart_turn:
        stop = TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=speech.turn.fallback_s))
        )
    else:
        stop = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=speech.turn.fallback_s)
    return LLMUserAggregatorParams(vad_analyzer=vad, user_turn_strategies=UserTurnStrategies(stop=[stop]))


def log_latency(breakdown: LatencyBreakdown, session_id: str) -> None:
    """One line per voice turn: where the time from end of speech to first audio went."""
    parts = {part.key: round(part.duration_secs * 1000, 1) for part in breakdown.contributions}
    logger.info(
        "Voice turn latency",
        extra={
            "event": "voice_latency",
            "session": session_id,
            "total_ms": round(breakdown.total_secs * 1000, 1),
            "parts": parts,
            "labels": [f"{part.label} ({part.owner})" for part in breakdown.contributions],
            "ttfb": {item.processor: round(item.duration_secs * 1000, 1) for item in breakdown.ttfb},
            "tools": {call.function_name: round(call.duration_secs * 1000, 1) for call in breakdown.function_calls},
            "measured_from": str(breakdown.measured_from) if breakdown.measured_from else None,
        },
    )


async def run_voice_session(
    connection: SmallWebRTCConnection,
    config: AppConfig,
    services: Services,
    speech: SpeechModels,
    hub: ChatHub | None = None,
) -> None:
    """Run one browser voice connection until it closes."""
    session_id = connection.pc_id
    router = build_router(config.models)
    state = SessionState(pending_window_s=VOICE_PENDING_S)
    session = ChatSession(state, config, services)
    runtime = session.runtime

    def next_llm_call() -> int:
        state.llm_calls += 1
        return state.llm_calls

    timer = RequestTimer(on_llm_call=next_llm_call)
    timer.model = config.models.roles.router

    @router.strategy.event_handler("on_service_switched")
    async def on_service_switched(_strategy, service) -> None:
        timer.model = service.name
        logger.warning("LLM failover", extra={"event": "llm_failover", "active_role": service.name})
        await task.queue_frame(LLMRunFrame())

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt(runtime.registry)}],
        tools=runtime.tools_schema(),
    )
    pair = LLMContextAggregatorPair(context, user_params=user_params(config.models.speech))
    preprocessor = VoiceTurnPreprocessor(state, runtime, services.timers, context=context)
    pipeline = Pipeline(
        [
            transport.input(),
            speech.stt(),
            pair.user(),
            preprocessor,
            TimingStartProcessor(timer),
            router,
            TimingEndProcessor(timer),
            speech.tts(),
            transport.output(),
            pair.assistant(),
        ]
    )
    latency = UserBotLatencyObserver()
    # RTVI stays on here: the browser's Pipecat client uses it for transcripts and state.
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True), observers=[latency])
    session.task = task

    async def send(message: dict[str, Any]) -> None:
        await task.queue_frame(RTVIServerMessageFrame(data=message))

    session.send = send  # type: ignore[method-assign]
    runtime.notify = send

    @latency.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(_observer, breakdown: LatencyBreakdown) -> None:
        log_latency(breakdown, session_id)

    async def on_timer_event(event: TimerEvent) -> None:
        if event.device == state.device and event.kind == "ring" and event.text:
            await task.queue_frame(TTSSpeakFrame(event.text, append_to_context=False))

    connected = asyncio.Event()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client) -> None:
        connected.set()
        logger.info("Voice session connected", extra={"event": "voice_connected", "session": session_id})

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client) -> None:
        logger.info("Voice session ended", extra={"event": "voice_disconnected", "session": session_id})
        await task.cancel(reason="client disconnected")

    async def end_if_never_connected() -> None:
        try:
            await asyncio.wait_for(connected.wait(), CONNECT_TIMEOUT_S)
        except TimeoutError:
            logger.warning("Voice session never connected", extra={"event": "voice_connect_timeout", "session": session_id})
            await task.cancel(reason="never connected")

    watchdog = asyncio.create_task(end_if_never_connected())
    services.timers.add_listener(on_timer_event)
    if hub is not None:
        hub.sessions.add(session)
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        watchdog.cancel()
        services.timers.remove_listener(on_timer_event)
        if hub is not None:
            hub.sessions.discard(session)
