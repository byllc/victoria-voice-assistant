"""Browser voice over SmallWebRTC: Silero VAD + Smart Turn -> Whisper -> router + tools -> Kokoro."""

from __future__ import annotations

import logging
import asyncio
import time
from typing import Any

from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMRunFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TurnMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
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
from victoria_voice_assistant.agents.tasks import TaskEvent
from victoria_voice_assistant.notifications.service import Announcement, Notification
from victoria_voice_assistant.tools.timers import TimerEvent
from victoria_voice_assistant.voice.models import SpeechModels, whisper_prompt

logger = logging.getLogger(__name__)

# A session whose browser never completes the WebRTC connection is ended after this long.
CONNECT_TIMEOUT_S = 30.0
# The room counts as mid-conversation for this long after anyone last spoke.
CONVERSATION_GRACE_S = 3.0
# After the user stops speaking, a reply is expected within this long.
REPLY_WAIT_S = 15.0


class RoomActivity(BaseObserver):
    """Whether someone is mid-conversation in this voice session, for notification delivery (architecture §10.3).

    Pipecat's TurnTrackingObserver counts the first turn as starting with the pipeline and
    ending only after the bot first speaks, so a fresh session would look busy until then.
    This tracks the same speaking frames, plus a grace period after anyone stops.
    """

    def __init__(self, clock=time.monotonic, on_change=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._clock = clock
        self._on_change = on_change  # async callback(kind): "user_started", "user_stopped", "bot_stopped"
        self.user_speaking = False
        self.bot_speaking = False
        self.awaiting_reply_since: float | None = None
        self.last_activity = float("-inf")

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        change = None
        if isinstance(frame, UserStartedSpeakingFrame):
            change = None if self.user_speaking else "user_started"
            self.user_speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            change = "user_stopped" if self.user_speaking else None
            self.user_speaking = False
            self.awaiting_reply_since = self._clock()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            self.awaiting_reply_since = None
        elif isinstance(frame, BotStoppedSpeakingFrame):
            change = "bot_stopped" if self.bot_speaking else None
            self.bot_speaking = False
        else:
            return
        self.last_activity = self._clock()
        if change and self._on_change is not None:
            await self._on_change(change)

    def busy(self) -> bool:
        now = self._clock()
        if self.user_speaking or self.bot_speaking or now - self.last_activity < CONVERSATION_GRACE_S:
            return True
        return self.awaiting_reply_since is not None and now - self.awaiting_reply_since < REPLY_WAIT_S


class MusicDucker:
    """Architecture §8: music for this device drops to `duck_level` while someone talks to the assistant,
    and comes back once the room is quiet again (after the reply, or when no reply comes)."""

    def __init__(self, services: Services, device: str, activity: RoomActivity) -> None:
        self.services = services
        self.device = device
        self.activity = activity
        self._restore: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task] = set()

    async def on_change(self, kind: str) -> None:
        music = self.services.music
        if music is None:
            return
        if kind == "user_started":
            self._cancel()
            await music.duck(self.device)
        else:
            self._schedule()

    def _schedule(self) -> None:
        self._cancel()
        self._restore = asyncio.get_running_loop().call_later(CONVERSATION_GRACE_S, self._check)

    def _check(self) -> None:
        self._restore = None
        if self.activity.busy():
            self._schedule()  # still talking, or a reply is on its way
            return
        assert self.services.music is not None
        task = asyncio.get_running_loop().create_task(self.services.music.unduck(self.device))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _cancel(self) -> None:
        if self._restore is not None:
            self._restore.cancel()
            self._restore = None

    async def close(self) -> None:
        self._cancel()
        if self.services.music is not None:
            await self.services.music.unduck(self.device)


class VoiceOutlet:
    """The notification service's view of a voice session: a speaker. Screens get the badge from chat."""

    speaks = True
    client_id = None

    def __init__(self, state: SessionState, activity: RoomActivity, context: LLMContext, task: PipelineTask) -> None:
        self.state = state
        self.activity = activity
        self.context = context
        self.task = task
        self.device = state.device

    def busy(self) -> bool:
        return self.activity.busy()

    def speaking(self) -> bool:
        return self.activity.bot_speaking

    def requests(self) -> int:
        return self.state.requests

    async def announce(self, announcement: Announcement) -> None:
        if announcement.question is not None:
            self.state.set_pending(announcement.question)  # "yes" reads them out
        self.context.add_message({"role": "assistant", "content": announcement.text})
        await self.task.queue_frame(TTSSpeakFrame(announcement.text, append_to_context=False))

    async def show(self, unread: list[Notification]) -> None:
        return None


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


class ShortClipSmartTurn(LocalSmartTurnAnalyzerV3):
    """Smart Turn that can be told to end the current turn without running the model."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.end_now = False

    def _process_speech_segment(self, audio_buffer):
        if self.end_now:
            self.end_now = False
            return EndOfTurnState.COMPLETE, TurnMetricsData(
                processor="ShortClipSmartTurn", is_complete=True, probability=1.0, e2e_processing_time_ms=0.0
            )
        return super()._process_speech_segment(audio_buffer)


class ShortClipStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """Ends a turn of very short speech ("stop", "pause") at its first pause.

    The Smart Turn project notes that very short clips work poorly, and many home
    commands are one word. Architecture §4.2: skip Smart Turn for clips under about
    a second. Speech is measured in audio time, by counting the samples received
    while the VAD reports speaking: the VAD frames' own wall-clock stamps can't be
    used, because audio is often processed in bursts. Only a turn's first stretch
    of speech counts, so a pause later in a sentence still goes to Smart Turn.
    """

    def __init__(self, *, turn_analyzer: ShortClipSmartTurn, short_clip_s: float, **kwargs) -> None:
        super().__init__(turn_analyzer=turn_analyzer, **kwargs)
        self._analyzer = turn_analyzer
        self._short_clip_s = short_clip_s
        self._segments = 0
        self._speech_audio_s = 0.0
        self._start_secs = 0.0

    async def handle_user_turn_stopped(self):
        self._segments = 0
        self._speech_audio_s = 0.0
        await super().handle_user_turn_stopped()

    def note_speech_started(self, frame: VADUserStartedSpeakingFrame) -> None:
        self._segments += 1
        self._start_secs = frame.start_secs

    def note_audio(self, frame: InputAudioRawFrame, speaking: bool) -> None:
        if speaking and self._segments == 1:
            self._speech_audio_s += len(frame.audio) / (2 * frame.num_channels * frame.sample_rate)

    def note_speech_stopped(self, frame: VADUserStoppedSpeakingFrame) -> bool:
        """Whether this pause ends a short clip; if so, the next analysis skips the model."""
        if self._short_clip_s <= 0 or self._segments != 1:
            return False
        # Audio is marked speaking from start_secs into the speech until stop_secs of silence.
        spoken_s = self._speech_audio_s + self._start_secs - frame.stop_secs
        if spoken_s >= self._short_clip_s:
            return False
        self._analyzer.end_now = True
        logger.info(
            "Short clip ended its turn without Smart Turn",
            extra={"event": "short_clip_turn", "duration_ms": round(spoken_s * 1000, 1)},
        )
        return True

    async def _handle_vad_user_started_speaking(self, frame: VADUserStartedSpeakingFrame):
        self.note_speech_started(frame)
        await super()._handle_vad_user_started_speaking(frame)

    async def _handle_input_audio(self, frame: InputAudioRawFrame):
        self.note_audio(frame, self._vad_user_speaking)
        await super()._handle_input_audio(frame)

    async def _handle_vad_user_stopped_speaking(self, frame: VADUserStoppedSpeakingFrame):
        self.note_speech_stopped(frame)
        await super()._handle_vad_user_stopped_speaking(frame)


def user_params(speech: SpeechConfig) -> LLMUserAggregatorParams:
    vad = SileroVADAnalyzer(
        params=VADParams(confidence=speech.vad.confidence, start_secs=speech.vad.start_s, stop_secs=speech.vad.stop_s)
    )
    if speech.turn.smart_turn:
        stop = ShortClipStopStrategy(
            turn_analyzer=ShortClipSmartTurn(params=SmartTurnParams(stop_secs=speech.turn.fallback_s)),
            short_clip_s=speech.turn.short_clip_s,
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
    preprocessor = VoiceTurnPreprocessor(
        state, runtime, services.timers, agents=services.agents, notifications=services.notifications, context=context
    )
    pipeline = Pipeline(
        [
            transport.input(),
            speech.stt(whisper_prompt(config.agents)),
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
    ducker_ref: list[MusicDucker] = []

    async def on_activity(kind: str) -> None:
        if ducker_ref:
            await ducker_ref[0].on_change(kind)

    activity = RoomActivity(on_change=on_activity)
    ducker_ref.append(MusicDucker(services, state.device, activity))
    # RTVI stays on here: the browser's Pipecat client uses it for transcripts and state.
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True), observers=[latency, activity])
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

    async def on_task_event(event: TaskEvent) -> None:
        if event.kind == "say" and event.task.device == state.device and event.text:
            # Speak the short version; keep the whole reply in the conversation for follow-ups.
            context.add_message({"role": "assistant", "content": event.full or event.text})
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
    outlet = VoiceOutlet(state, activity, context, task)
    services.timers.add_listener(on_timer_event)
    if services.agents is not None:
        services.agents.tasks.add_listener(on_task_event)
    if services.notifications is not None:
        services.notifications.attach(outlet)
    if hub is not None:
        hub.sessions.add(session)
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        watchdog.cancel()
        await ducker_ref[0].close()
        if services.notifications is not None:
            services.notifications.detach(outlet)
        services.timers.remove_listener(on_timer_event)
        if services.agents is not None:
            services.agents.tasks.remove_listener(on_task_event)
        if hub is not None:
            hub.sessions.discard(session)
