"""ESPHome voice satellites (Stage 12): the FutureProofHomes Satellite1, and others on ESPHome firmware.

ESPHome devices don't dial out, so the assistant connects to each satellite in `tools.yaml`
(`satellites:`) with its API key and keeps the connection, reconnecting when it drops. The device
detects the wake word itself and streams 16 kHz audio; each turn runs through the same Pipecat
pipeline as browser voice (Silero VAD and Smart Turn, Whisper, the router and tools, Kokoro).
Findings this follows (docs/stages.md, Stage 12):

- The device can't tell when the user has finished: the speech detector ends the turn and the device
  is told to stop listening (`STT_VAD_END`).
- Its clean microphone channel is quiet, so audio is raised by `gain` before speech detection.
- It plays replies from an HTTP URL, FLAC only (48 kHz mono). Replies are encoded here and served by a
  small LAN audio server, as the turn's reply or, with no turn open, as an announcement.
- A reply that leaves a question open asks the device to reopen its mic ("continue conversation").
- Timers are mirrored to the device as its own timer events, so it rings, shows them on its LEDs,
  and stops on its on-device "stop" word or button; its LED ring follows the voice events.
"""

from __future__ import annotations

import asyncio
import io
import logging
import secrets
import socket
import time
from dataclasses import dataclass, field
from math import gcd
from typing import Any

import numpy as np
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMRunFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from victoria_voice_assistant.agents.tasks import TaskEvent
from victoria_voice_assistant.chat.llm import build_router
from victoria_voice_assistant.chat.prompt import system_prompt
from victoria_voice_assistant.chat.session import ChatSession
from victoria_voice_assistant.chat.timing import RequestTimer, TimingEndProcessor, TimingStartProcessor
from victoria_voice_assistant.chat.tools import SessionState
from victoria_voice_assistant.config.schema import AppConfig, SatelliteConfig
from victoria_voice_assistant.notifications.service import Announcement, Notification
from victoria_voice_assistant.satellite.arbiter import Arbiter, speech_over_noise_db
from victoria_voice_assistant.satellite.status import SatelliteBoard
from victoria_voice_assistant.tools.outcome import VOICE_PENDING_S
from victoria_voice_assistant.tools.registry import Services
from victoria_voice_assistant.tools.timers import TimerEvent
from victoria_voice_assistant.voice.models import SpeechModels, whisper_prompt
from victoria_voice_assistant.voice.session import VoiceTurnPreprocessor, VoiceTurnTracker, user_params

logger = logging.getLogger(__name__)

SAMPLE_RATE_IN = 16000  # what ESPHome streams
SAMPLE_RATE_OUT = 48000  # what the Satellite1 plays (FLAC, mono)
NO_SPEECH_S = 8.0  # after the wake word, a turn with no speech this long ends quietly
REPLY_WAIT_S = 30.0  # after the user stops, a reply is expected within this long
REPLY_SETTLE_S = 0.6  # the reply is sent once no speech audio has come for this long and the request is done
AUDIO_KEEP_S = 300.0  # how long a reply stays fetchable
RECONNECT_MAX_S = 60.0


# The reply audio server --------------------------------------------------------------------------


class AudioServer:
    """Serves encoded replies to satellites over plain HTTP on the LAN, by unguessable one-off names."""

    def __init__(self, port: int, clock=time.monotonic) -> None:
        self.port = port
        self._clock = clock
        self._files: dict[str, tuple[bytes, float]] = {}
        self._runner: Any = None

    async def start(self) -> None:
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/satellite-audio/{name}", self._get)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port).start()  # the satellites must reach it
        logger.info("Satellite audio server started", extra={"event": "satellite_audio_started", "status": self.port})

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def add(self, data: bytes) -> str:
        now = self._clock()
        for name, (_, added) in list(self._files.items()):
            if now - added > AUDIO_KEEP_S:
                del self._files[name]
        name = f"{secrets.token_urlsafe(16)}.flac"
        self._files[name] = (data, now)
        return name

    def get(self, name: str) -> bytes | None:
        entry = self._files.get(name)
        return entry[0] if entry else None

    async def _get(self, request: Any) -> Any:
        from aiohttp import web

        data = self.get(request.match_info["name"])
        if data is None:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="audio/x-flac")


def to_flac(chunks: list[tuple[bytes, int]], gain: float = 1.0) -> tuple[bytes, float]:
    """Speech audio (16-bit mono chunks at their sample rates) as 48 kHz mono FLAC. Returns (data, seconds)."""
    import soundfile
    from scipy.signal import resample_poly

    parts = []
    for audio, rate in chunks:
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
        if rate != SAMPLE_RATE_OUT and samples.size:
            divisor = gcd(rate, SAMPLE_RATE_OUT)
            samples = resample_poly(samples, SAMPLE_RATE_OUT // divisor, rate // divisor)
        parts.append(samples)
    samples = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    samples = np.clip(samples * gain, -32768, 32767).astype(np.int16)
    buffer = io.BytesIO()
    soundfile.write(buffer, samples, SAMPLE_RATE_OUT, format="FLAC", subtype="PCM_16")
    return buffer.getvalue(), samples.size / SAMPLE_RATE_OUT


def raise_level(audio: bytes, gain: float) -> bytes:
    if gain == 1.0:
        return audio
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) * gain
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def local_address(host: str) -> str:
    """This machine's address on the route to `host` (what the satellite can reach)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((socket.gethostbyname(host), 9))  # no packet is sent for UDP connect
        return probe.getsockname()[0]
    finally:
        probe.close()


# One turn's state and the device's events --------------------------------------------------------


@dataclass
class Run:
    """One wake-word (or follow-up) turn on the device, from its start to RUN_END."""

    started: float
    wake_word: str | None
    heard_speech: bool = False
    listening: bool = True
    audio_s: float = 0.0  # audio received this turn, and its raw peak level (before `gain`), for the log
    peak: int = 0
    audio: bytearray = field(default_factory=bytearray)  # after `gain`: its loudness is logged too
    arbitrating: bool = False  # waiting to hear whether this satellite answers (several woke)
    held: list[bytes] = field(default_factory=list)  # audio after `gain`, held until then
    raw: bytearray = field(default_factory=bytearray)  # the same, before `gain`, for its score
    timers: list[asyncio.TimerHandle] = field(default_factory=list)


class SatelliteTurnObserver(BaseObserver):
    """Turns the pipeline's speaking and transcription frames into the device's voice events."""

    def __init__(self, session: SatelliteSession, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self._seen: set[int] = set()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if not isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame, TranscriptionFrame)):
            return
        if frame.id in self._seen:  # each frame is seen at every hop
            return
        self._seen.add(frame.id)
        if len(self._seen) > 1000:
            self._seen = {frame.id}
        if isinstance(frame, UserStartedSpeakingFrame):
            self.session.user_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.session.user_stopped()
        else:
            self.session.heard(frame.text)


class SatelliteOutput(FrameProcessor):
    """Collects the spoken reply (instead of a transport's audio output) and hands it to the session."""

    def __init__(self, session: SatelliteSession, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self._chunks: list[tuple[bytes, int]] = []
        self._text: list[str] = []
        self._settle: asyncio.TimerHandle | None = None
        self._waiting_since: float | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def collecting(self) -> bool:
        return bool(self._chunks)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self._chunks.append((frame.audio, frame.sample_rate))
            self._arm()
            return  # the audio goes to the device as a file, not on down the pipeline
        if isinstance(frame, TTSTextFrame) and frame.text.strip():
            self._text.append(frame.text.strip())
        await self.push_frame(frame, direction)

    def _arm(self) -> None:
        if self._settle is not None:
            self._settle.cancel()
        self._settle = asyncio.get_running_loop().call_later(REPLY_SETTLE_S, self._settled)

    def _settled(self) -> None:
        self._settle = None
        now = time.monotonic()
        self._waiting_since = self._waiting_since or now
        # More may be coming (a tool's result read out after the model's first words): wait while the
        # request is still in progress, but never forever.
        if not self.session.state.idle() and now - self._waiting_since < REPLY_WAIT_S:
            self._arm()
            return
        self._waiting_since = None
        chunks, text = self._chunks, " ".join(self._text)
        self._chunks, self._text = [], []
        task = asyncio.get_running_loop().create_task(self.session.deliver(chunks, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class SatelliteOutlet:
    """The notification service's view of a satellite: a speaker in a room."""

    speaks = True
    client_id = None

    def __init__(self, session: SatelliteSession) -> None:
        self.session = session
        self.device = session.state.device

    def busy(self) -> bool:
        return self.session.busy()

    def speaking(self) -> bool:
        return self.session.output.collecting or self.session.delivering

    def requests(self) -> int:
        return self.session.state.requests

    async def announce(self, announcement: Announcement) -> None:
        if announcement.question is not None:
            self.session.state.set_pending(announcement.question)  # "yes" reads them out
        await self.session.say(announcement.text)

    async def show(self, unread: list[Notification]) -> None:
        self.session.set_pending_light(bool(unread))


# One connected satellite -------------------------------------------------------------------------


class SatelliteSession:
    """A connected satellite's voice pipeline and its turns."""

    def __init__(
        self,
        name: str,
        settings: SatelliteConfig,
        client: Any,
        config: AppConfig,
        services: Services,
        speech: SpeechModels,
        audio: AudioServer,
        audio_host: str,
        ring_key: int | None = None,
        arbiter: Arbiter | None = None,
    ) -> None:
        self.name = name
        self.arbiter = arbiter  # decides which satellite answers when several wake together
        self.ring_key = ring_key  # the device's user-facing "LED Ring" light, for the pending indicator
        self._ring_on: bool | None = None
        self.settings = settings
        self.client = client
        self.services = services
        self.audio = audio
        self.audio_host = audio_host
        self.run: Run | None = None
        self.delivering = False
        self.state = SessionState(device=name, pending_window_s=VOICE_PENDING_S)
        session = ChatSession(self.state, config, services)
        self.chat = session
        runtime = session.runtime
        router = build_router(config.models)

        def next_llm_call() -> int:
            self.state.llm_calls += 1
            self.state.llm_busy += 1
            self.state.awaiting_llm = False
            return self.state.llm_calls

        timer = RequestTimer(on_llm_call=next_llm_call)
        timer.model = config.models.roles.router

        @router.strategy.event_handler("on_service_switched")
        async def on_service_switched(_strategy, service) -> None:
            timer.model = service.name
            logger.warning("LLM failover", extra={"event": "llm_failover", "device": name})
            await self.task.queue_frame(LLMRunFrame())

        self.context = LLMContext(
            messages=[{"role": "system", "content": system_prompt(runtime.registry)}],
            tools=runtime.tools_schema(),
        )
        pair = LLMContextAggregatorPair(self.context, user_params=user_params(config.models.speech, self.state))
        preprocessor = VoiceTurnPreprocessor(
            self.state, runtime, services.timers, agents=services.agents, notifications=services.notifications, context=self.context
        )
        runtime.on_settled = preprocessor.release_next
        self.output = SatelliteOutput(self)
        pipeline = Pipeline(
            [
                speech.stt(whisper_prompt(config.agents)),
                pair.user(),
                preprocessor,
                TimingStartProcessor(timer),
                router,
                VoiceTurnTracker(self.state, preprocessor, retry=lambda: self.task.queue_frame(LLMRunFrame())),
                TimingEndProcessor(timer),
                speech.tts(),
                self.output,
                pair.assistant(),
            ]
        )
        self.task = PipelineTask(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=SAMPLE_RATE_IN),
            observers=[SatelliteTurnObserver(self)],
            idle_timeout_secs=None,  # a satellite sits quiet for hours between turns
            enable_rtvi=False,
        )
        session.task = self.task

        async def ignore(_message: dict[str, Any]) -> None:
            return None  # chat-only messages (notices, timers lists) have nowhere to go

        session.send = ignore  # type: ignore[method-assign]
        runtime.notify = ignore
        self.outlet = SatelliteOutlet(self)
        self._known_timers: set[str] = set()

    # Events to the device

    def send(self, event: Any, data: dict[str, str] | None = None) -> None:
        try:
            self.client.send_voice_assistant_event(event, data)
        except Exception as exc:  # noqa: BLE001 - the connection dropped; the link reconnects
            logger.warning("Satellite event not sent", extra={"event": "satellite_send_failed", "device": self.name, "reason": str(exc)})

    def _status(self, **changes: Any) -> None:
        if self.services.satellites is not None:
            self.services.satellites.update(self.name, **changes)

    async def play_announcement(self, url: str, text: str, seconds: float) -> None:
        """Play audio already encoded and served (an announcement to several rooms)."""
        self._status(state="announcing", reply=text)
        try:
            await self.client.send_voice_assistant_announcement_await_response(url, max(30.0, seconds + 15.0), text=text)
        except Exception as exc:  # noqa: BLE001 - one satellite failing mustn't stop the others
            logger.warning("Satellite announcement failed", extra={"event": "satellite_reply_failed", "device": self.name, "reason": str(exc) or type(exc).__name__})
        finally:
            if self.run is None and self._board_state() == "announcing":
                self._status(state="idle")

    def _board_state(self) -> str | None:
        board = self.services.satellites
        return next((s["state"] for s in board.snapshot() if s["name"] == self.name), None) if board else None

    def _idle_after(self, seconds: float) -> None:
        """The device plays the reply after the turn ends; it's idle once that has had time to play."""
        def idle() -> None:
            if self.run is None and self._board_state() == "replying":
                self._status(state="idle")

        asyncio.get_running_loop().call_later(seconds + 1.0, idle)

    def reconfigure(self, settings: SatelliteConfig, config: AppConfig) -> None:
        """A config reload while connected: new settings (gain, room, light colour) and the reloaded tools."""
        if settings.pending_color != self.settings.pending_color:
            self._ring_on = None  # the next update re-lights it in the new colour
        self.settings = settings
        self.chat.reload(config, self.services)

    def set_pending_light(self, on: bool) -> None:
        """Light the LED ring while notifications are unread here. The firmware shows it whenever the
        satellite is idle; its voice animations take over during a turn."""
        color = self.settings.pending_color
        if self.ring_key is None or color is None or on == self._ring_on:
            return
        self._ring_on = on
        try:
            if on:
                rgb = tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
                self.client.light_command(self.ring_key, state=True, rgb=rgb, brightness=0.6)
            else:
                self.client.light_command(self.ring_key, state=False)
        except Exception as exc:  # noqa: BLE001 - the connection dropped; the link reconnects
            self._ring_on = None
            logger.warning("Satellite light not set", extra={"event": "satellite_send_failed", "device": self.name, "reason": str(exc)})
            return
        logger.info("Satellite pending light", extra={"event": "satellite_pending_light", "device": self.name, "status": "on" if on else "off"})

    def busy(self) -> bool:
        return self.run is not None or not self.state.idle() or self.output.collecting or self.delivering

    # The device's voice requests

    async def handle_start(self, conversation_id: str, flags: int, audio_settings: Any, wake_word_phrase: str | None) -> int | None:
        from aioesphomeapi.model import VoiceAssistantEventType as E

        self._end_run()
        self.run = Run(started=time.monotonic(), wake_word=wake_word_phrase)
        logger.info("Satellite turn", extra={"event": "satellite_turn", "device": self.name, "text": wake_word_phrase})
        self.send(E.VOICE_ASSISTANT_RUN_START)
        self.send(E.VOICE_ASSISTANT_STT_START)
        self._status(state="listening")
        self.run.timers.append(asyncio.get_running_loop().call_later(NO_SPEECH_S, self._no_speech))
        if self.arbiter is not None and self.arbiter.satellites > 1:
            self.run.arbitrating = True
            task = asyncio.get_running_loop().create_task(self._arbitrate(self.run))
            self.output._tasks.add(task)
            task.add_done_callback(self.output._tasks.discard)
        return 0  # stream the audio over this connection

    async def handle_audio(self, data: bytes, _extra: bytes | None = None) -> None:
        if self.run is None or not self.run.listening:
            return
        samples = np.frombuffer(data, dtype=np.int16)
        self.run.audio_s += samples.size / SAMPLE_RATE_IN
        if samples.size:
            self.run.peak = max(self.run.peak, int(np.abs(samples.astype(np.int32)).max()))
        raised = raise_level(data, self.settings.gain)
        self.run.audio += raised
        if self.run.arbitrating:
            self.run.held.append(raised)
            self.run.raw += data
            return
        await self.task.queue_frame(InputAudioRawFrame(audio=raised, sample_rate=SAMPLE_RATE_IN, num_channels=1))

    def arbitration_score(self) -> tuple[bool, float]:
        """For the arbiter: priority (a question open here, or a timer ringing here), and how far this
        turn's speech stands above the satellite's own background noise, plus its configured offset."""
        priority = self.state.pending is not None or bool(self.services.timers.ringing(self.name))
        raw = bytes(self.run.raw) if self.run is not None else b""
        return priority, speech_over_noise_db(raw) + self.settings.arbitration_offset_db

    async def _arbitrate(self, run: Run) -> None:
        won = await self.arbiter.join(self)  # type: ignore[union-attr]
        if self.run is not run:
            return
        if not won:
            logger.info("Satellite stood down: another answers", extra={"event": "satellite_stood_down", "device": self.name})
            self._end_run()  # RUN_END while streaming: the device goes back to idle
            return
        run.arbitrating = False
        for chunk in run.held:  # the speech heard during the window, then the rest live
            await self.task.queue_frame(InputAudioRawFrame(audio=chunk, sample_rate=SAMPLE_RATE_IN, num_channels=1))
        run.held.clear()

    async def handle_stop(self, abort: bool) -> None:
        if self.run is not None and abort:
            logger.info("Satellite turn aborted", extra={"event": "satellite_turn_aborted", "device": self.name, **self._audio_stats()})
            self._end_run(send_end=False)

    async def handle_announcement_finished(self, finished: Any) -> None:
        return None

    # Turn progress, from the pipeline

    def user_started(self) -> None:
        from aioesphomeapi.model import VoiceAssistantEventType as E

        if self.run is not None and self.run.listening and not self.run.heard_speech:
            self.run.heard_speech = True
            self.send(E.VOICE_ASSISTANT_STT_VAD_START)

    def user_stopped(self) -> None:
        from aioesphomeapi.model import VoiceAssistantEventType as E

        if self.run is None or not self.run.listening:
            return
        self.run.listening = False  # the device stops streaming; the turn is the model's now
        self._status(state="thinking")
        logger.info("Satellite speech ended", extra={"event": "satellite_speech_end", "device": self.name, **self._audio_stats()})
        self.send(E.VOICE_ASSISTANT_STT_VAD_END)
        self.run.timers.append(asyncio.get_running_loop().call_later(REPLY_WAIT_S, self._no_reply))

    def heard(self, text: str) -> None:
        from aioesphomeapi.model import VoiceAssistantEventType as E

        if self.run is None:
            return
        self.send(E.VOICE_ASSISTANT_STT_END, {"text": text})
        self._status(request=text)
        self.send(E.VOICE_ASSISTANT_INTENT_START)  # the LED ring shows it thinking

    def _no_speech(self) -> None:
        if self.run is not None and not self.run.heard_speech:
            logger.info("Satellite heard no speech", extra={"event": "satellite_no_speech", "device": self.name, **self._audio_stats()})
            # RUN_END alone, while the device is still streaming, always stops its mic and returns it to
            # idle. Sent right after STT_VAD_END it could land while the mic was stopping, which the
            # firmware ignores, leaving it waiting for a reply forever (2026-11-02, after a follow-up).
            self._end_run()

    def _audio_stats(self) -> dict[str, float]:
        """For the log: how much audio came, its raw peak, and its loudness after `gain` on Pipecat's 0-1
        scale, the one speech detection's `min_volume` gate (0.6) uses."""
        if self.run is None:
            return {}
        stats: dict[str, float] = {"duration_s": round(self.run.audio_s, 1), "peak": self.run.peak}
        if self.run.audio_s >= 0.5:
            from pipecat.audio.utils import calculate_audio_volume

            try:
                stats["volume"] = round(calculate_audio_volume(bytes(self.run.audio), SAMPLE_RATE_IN), 2)
            except ValueError:
                pass
        return stats

    def _no_reply(self) -> None:
        if self.run is not None:
            logger.warning("Satellite turn got no reply", extra={"event": "satellite_no_reply", "device": self.name})
            self._end_run()

    def _end_run(self, send_end: bool = True) -> None:
        from aioesphomeapi.model import VoiceAssistantEventType as E

        if self.run is None:
            return
        for handle in self.run.timers:
            handle.cancel()
        self.run = None
        if not self.delivering:
            self._status(state="idle")
        if send_end:
            self.send(E.VOICE_ASSISTANT_RUN_END)

    # Replies

    async def say(self, text: str) -> None:
        """Speak unprompted (a timer, a finished task, a notification): through the pipeline's voice."""
        self.context.add_message({"role": "assistant", "content": text})
        await self.task.queue_frame(TTSSpeakFrame(text, append_to_context=False))

    async def deliver(self, chunks: list[tuple[bytes, int]], text: str) -> None:
        from aioesphomeapi.model import VoiceAssistantEventType as E

        if not chunks:
            return
        self.delivering = True
        try:
            data, seconds = await asyncio.to_thread(to_flac, chunks)
            url = f"http://{self.audio_host}:{self.audio.port}/satellite-audio/{self.audio.add(data)}"
            follow_up = self.state.pending is not None  # a question is open: listen again with no wake word
            if self.run is not None:
                self.send(E.VOICE_ASSISTANT_INTENT_END, {"continue_conversation": "1" if follow_up else "0"})
                self.send(E.VOICE_ASSISTANT_TTS_START, {"text": text})
                self.send(E.VOICE_ASSISTANT_TTS_END, {"url": url})
                self._status(state="replying", reply=text)
                self._end_run()
                self._idle_after(seconds)
                logger.info("Satellite reply", extra={"event": "satellite_reply", "device": self.name, "duration_s": round(seconds, 1), "text": text})
                return
            logger.info("Satellite announcement", extra={"event": "satellite_announce", "device": self.name, "duration_s": round(seconds, 1), "text": text})
            self._status(state="announcing", reply=text)
            await self.client.send_voice_assistant_announcement_await_response(
                url, max(30.0, seconds + 15.0), text=text, start_conversation=follow_up
            )
        except Exception as exc:  # noqa: BLE001 - a reply that can't be sent is logged, never raised into the pipeline
            logger.warning("Satellite reply failed", extra={"event": "satellite_reply_failed", "device": self.name, "reason": str(exc) or type(exc).__name__})
        finally:
            self.delivering = False
            if self.run is None and self._board_state() == "announcing":
                self._status(state="idle")

    # Timers, mirrored to the device so it rings and its "stop" word works

    async def on_timer_event(self, event: TimerEvent) -> None:
        from aioesphomeapi.model import VoiceAssistantTimerEventType as T

        if event.device != self.name:
            return
        timers = self.services.timers
        now = timers.clock()
        try:
            if event.kind == "changed":
                running = {t.id: t for t in timers.timers(self.name) if t.state == "running"}
                ringing = {t.id for t in timers.ringing(self.name)}
                for timer_id, timer in running.items():
                    kind = T.VOICE_ASSISTANT_TIMER_UPDATED if timer_id in self._known_timers else T.VOICE_ASSISTANT_TIMER_STARTED
                    self.client.send_voice_assistant_timer_event(
                        kind, timer_id, timer.name, int(timer.duration_s), int(timer.remaining(now)), True
                    )
                for timer_id in self._known_timers - set(running) - ringing:
                    self.client.send_voice_assistant_timer_event(T.VOICE_ASSISTANT_TIMER_CANCELLED, timer_id, None, 0, 0, False)
                self._known_timers = set(running) | (self._known_timers & ringing)
            elif event.kind == "ring":
                finished = timers.ringing(self.name)
                for timer in finished:
                    self.client.send_voice_assistant_timer_event(
                        T.VOICE_ASSISTANT_TIMER_FINISHED, timer.id, timer.name, int(timer.duration_s), 0, False
                    )
                    self._known_timers.discard(timer.id)
                if finished:
                    # The satellite rings, and its "stop" word or button stop it; nothing tells us when, so
                    # the timer is done here once the device has it.
                    await timers.stop_ringing(self.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Satellite timer not sent", extra={"event": "satellite_send_failed", "device": self.name, "reason": str(exc)})

    async def on_task_event(self, event: TaskEvent) -> None:
        if event.kind == "say" and event.task.device == self.name and event.text:
            self.context.add_message({"role": "assistant", "content": event.full or event.text})
            await self.task.queue_frame(TTSSpeakFrame(event.text, append_to_context=False))

    # Lifetime

    async def run_until_disconnected(self, disconnected: asyncio.Event) -> None:
        services = self.services
        self.client.subscribe_voice_assistant(
            handle_start=self.handle_start,
            handle_stop=self.handle_stop,
            handle_audio=self.handle_audio,
            handle_announcement_finished=self.handle_announcement_finished,
        )
        services.timers.add_listener(self.on_timer_event)
        if services.agents is not None:
            services.agents.tasks.add_listener(self.on_task_event)
        if services.notifications is not None:
            services.notifications.attach(self.outlet)
        await self.on_timer_event(TimerEvent("changed", self.name))  # timers set before it connected
        runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        waiter = asyncio.create_task(disconnected.wait())
        try:
            await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            if not runner.done():
                await self.task.cancel(reason="satellite disconnected")
                try:
                    await runner
                except asyncio.CancelledError:
                    pass
            self._end_run(send_end=False)
            if services.notifications is not None:
                services.notifications.detach(self.outlet)
            services.timers.remove_listener(self.on_timer_event)
            if services.agents is not None:
                services.agents.tasks.remove_listener(self.on_task_event)


class SatelliteLink:
    """Keeps one satellite connected: connect, serve until it drops, wait, reconnect."""

    def __init__(
        self, name: str, settings: SatelliteConfig, config: AppConfig, services: Services, speech: SpeechModels, audio: AudioServer,
        arbiter: Arbiter | None = None,
    ) -> None:
        self.arbiter = arbiter
        self.name = name
        self.settings = settings
        self.config = config
        self.services = services
        self.speech = speech
        self.audio = audio
        self.connected = False
        self.session: SatelliteSession | None = None

    def reconfigure(self, settings: SatelliteConfig, config: AppConfig) -> None:
        """Settings that don't need a new connection; the next connection uses them too."""
        self.settings, self.config = settings, config
        if self.session is not None:
            self.session.reconfigure(settings, config)

    async def run(self) -> None:
        from aioesphomeapi import APIClient

        await self.speech.ready.wait()
        delay = 2.0
        while True:
            client = APIClient(self.settings.host, self.settings.port, password=None, noise_psk=self.settings.key or None, client_info="victoria-voice-assistant")
            disconnected = asyncio.Event()

            # Bound now: a late stop from this client must not end the next connection.
            async def on_stop(_expected: bool, disconnected: asyncio.Event = disconnected) -> None:
                disconnected.set()

            try:
                await asyncio.wait_for(client.connect(on_stop=on_stop, login=True), 15)
                self.connected = True
                delay = 2.0
                logger.info("Satellite connected", extra={"event": "satellite_connected", "device": self.name})
                entities, _services = await client.list_entities_services()
                ring = next((e.key for e in entities if type(e).__name__ == "LightInfo" and e.object_id == "led_ring"), None)
                session = SatelliteSession(
                    self.name, self.settings, client, self.config, self.services, self.speech, self.audio,
                    await asyncio.to_thread(local_address, self.settings.host), ring_key=ring, arbiter=self.arbiter,
                )
                self.session = session
                self.services.satellites.update(self.name, online=True, state="idle")
                await session.run_until_disconnected(disconnected)
                logger.warning("Satellite disconnected", extra={"event": "satellite_disconnected", "device": self.name})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - offline, refused, wrong key: retry
                logger.warning("Satellite unreachable", extra={"event": "satellite_unreachable", "device": self.name, "reason": str(exc) or type(exc).__name__})
            finally:
                self.connected = False
                self.session = None
                if self.services.satellites is not None:
                    self.services.satellites.update(self.name, online=False, state="idle")
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)


def _connection(settings: SatelliteConfig) -> tuple[str, int, str | None]:
    """What a satellite's connection depends on: a change here means reconnecting."""
    return settings.host, settings.port, settings.key


class SatelliteManager:
    """Every satellite in `tools.yaml`, and the audio server they fetch replies from.

    Follows config reloads (Stage 13): a satellite added to `satellites:` connects, one removed
    disconnects, one whose host, port or key changed reconnects, and other changes reach the connected
    session. The audio server starts with the first satellite; changing its port needs a restart.
    """

    def __init__(self, config: AppConfig, services: Services, speech: SpeechModels) -> None:
        self.config = config
        self.services = services
        self.speech = speech
        self.audio = AudioServer(config.tools.satellite_audio_port)
        if services.satellites is None:
            services.satellites = SatelliteBoard()
        self.board = services.satellites
        self.arbiter = Arbiter(config.tools.satellite_arbitration_s)
        self.links: dict[str, SatelliteLink] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._audio_started = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.update(self.config)

    async def update(self, config: AppConfig) -> None:
        async with self._lock:
            self.config = config
            wanted = config.tools.satellites
            self.board.configured({name: settings.room for name, settings in wanted.items()})
            self.arbiter.satellites = len(wanted)
            self.arbiter.window_s = config.tools.satellite_arbitration_s
            for name in list(self.links):
                if name not in wanted or _connection(wanted[name]) != _connection(self.links[name].settings):
                    await self._stop(name)
            for name, settings in wanted.items():
                link = self.links.get(name)
                if link is not None:
                    link.reconfigure(settings, config)
                    continue
                if not self._audio_started:
                    await self.audio.start()
                    self._audio_started = True
                link = SatelliteLink(name, settings, config, self.services, self.speech, self.audio, arbiter=self.arbiter)
                self.links[name] = link
                self._tasks[name] = asyncio.create_task(link.run(), name=f"satellite-{name}")
                logger.info("Satellite added", extra={"event": "satellite_added", "device": name})

    async def announce(self, text: str, rooms: list[str] | None = None) -> list[str]:
        """Say `text` on every connected satellite in `rooms` (None: everywhere), synthesized once and
        sent to each as the same audio. Returns the satellites it played on."""
        sessions = [
            link.session for link in self.links.values()
            if link.session is not None and (rooms is None or link.settings.room in rooms)
        ]
        if not sessions:
            return []
        audio, rate = await self.speech.synthesize(text)
        data, seconds = await asyncio.to_thread(to_flac, [(audio, rate)])
        name = self.audio.add(data)
        logger.info(
            "Satellite announcement to several rooms",
            extra={"event": "satellite_broadcast", "count": len(sessions), "duration_s": round(seconds, 1), "text": text},
        )
        await asyncio.gather(*(
            session.play_announcement(f"http://{session.audio_host}:{self.audio.port}/satellite-audio/{name}", text, seconds)
            for session in sessions
        ))
        return [session.name for session in sessions]

    async def _stop(self, name: str) -> None:
        self.links.pop(name, None)
        task = self._tasks.pop(name, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        logger.info("Satellite removed", extra={"event": "satellite_removed", "device": name})

    async def close(self) -> None:
        async with self._lock:
            for name in list(self._tasks):
                await self._stop(name)
        if self._audio_started:
            await self.audio.close()
