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
from contextlib import asynccontextmanager
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
from victoria_voice_assistant.chat.worker import run_worker
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
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
from victoria_voice_assistant.satellite.arbiter import Arbiter, describe_levels, speech_level_db
from victoria_voice_assistant.satellite.status import SatelliteBoard
from victoria_voice_assistant.tools.outcome import VOICE_PENDING_S
from victoria_voice_assistant.tools.registry import Services
from victoria_voice_assistant.tools.timers import TimerEvent
from victoria_voice_assistant.voice.models import SpeechModels, session_prompt
from victoria_voice_assistant.chat.turns import HistoryTap
from victoria_voice_assistant.voice.session import VoiceTurnPreprocessor, VoiceTurnTracker, user_params

logger = logging.getLogger(__name__)

SAMPLE_RATE_IN = 16000  # what ESPHome streams
SAMPLE_RATE_OUT = 48000  # what the Satellite1 plays (FLAC, mono)
NO_SPEECH_S = 8.0  # after the wake word, a turn with no speech this long ends quietly
REPLY_WAIT_S = 30.0  # after the user stops, a reply is expected within this long
REPLY_SETTLE_S = 0.6  # the reply is sent once no speech audio has come for this long and the request is done
AUDIO_KEEP_S = 300.0  # how long a reply stays fetchable
RECONNECT_MAX_S = 60.0
ANNOUNCE_WAIT_S = 60.0  # an announcement waits up to this long for a conversation turn to finish
# A turn ends as soon as its reply's URL is sent, while the device is still playing it; an announcement sent
# then is dropped (2026-11-05: the asking satellite said "Announced everywhere." and never "dinner's ready").
# So it also waits for the reply to have played, plus this.
REPLY_GAP_S = 0.5


# The reply audio server --------------------------------------------------------------------------


class AudioServer:
    """Serves encoded replies to satellites over plain HTTP on the LAN, by unguessable one-off names."""

    def __init__(self, port: int, clock=time.monotonic) -> None:
        self.port = port
        self._clock = clock
        self._files: dict[str, tuple[bytes, float]] = {}
        self._streams: dict[str, tuple[ReplyStream, float]] = {}
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

    def _forget_old(self, now: float) -> None:
        for kept in (self._files, self._streams):
            for name, (_, added) in list(kept.items()):
                if now - added > AUDIO_KEEP_S:
                    del kept[name]

    def add(self, data: bytes) -> str:
        now = self._clock()
        self._forget_old(now)
        name = f"{secrets.token_urlsafe(16)}.flac"
        self._files[name] = (data, now)
        return name

    def open_stream(self) -> tuple[str, ReplyStream]:
        """A reply that is served while it is still being spoken: its name and the stream to write to."""
        now = self._clock()
        self._forget_old(now)
        name = f"{secrets.token_urlsafe(16)}.flac"
        stream = ReplyStream()
        self._streams[name] = (stream, now)
        return name, stream

    def get(self, name: str) -> bytes | None:
        entry = self._files.get(name)
        return entry[0] if entry else None

    async def _get(self, request: Any) -> Any:
        from aiohttp import web

        name = request.match_info["name"]
        streamed = self._streams.get(name)
        if streamed is not None:
            response = web.StreamResponse(headers={"Content-Type": "audio/x-flac"})
            await response.prepare(request)
            try:
                async for part in streamed[0].parts():
                    await response.write(part)
                await response.write_eof()
            except (ConnectionError, asyncio.CancelledError):  # the device stopped playing (a new wake word)
                pass
            return response
        data = self.get(name)
        if data is None:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="audio/x-flac")


def at_output_rate(audio: bytes, rate: int) -> Any:
    """16-bit mono audio as float samples at the rate the satellite plays."""
    from scipy.signal import resample_poly

    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    if rate != SAMPLE_RATE_OUT and samples.size:
        divisor = gcd(rate, SAMPLE_RATE_OUT)
        samples = resample_poly(samples, SAMPLE_RATE_OUT // divisor, rate // divisor)
    return samples


class ReplyStream:
    """A reply as 48 kHz mono FLAC that grows while it is spoken, so the device can start playing the first
    sentence while the rest is synthesized. Written and read on the event loop.

    The encoder writes the header first, with the length unknown, and goes back to fill it in when the
    stream is closed. A device already reading never sees that: players accept a FLAC of unknown length.
    """

    def __init__(self) -> None:
        import soundfile

        self.data = bytearray()
        self.closed = False
        self.cut = False  # closed early: the wake word was said again while it played
        self.samples = 0
        self.started = time.monotonic()
        self._at = 0
        self._more = asyncio.Event()
        self._file = soundfile.SoundFile(self, "w", samplerate=SAMPLE_RATE_OUT, channels=1, format="FLAC", subtype="PCM_16")

    @property
    def seconds(self) -> float:
        return self.samples / SAMPLE_RATE_OUT

    # The file the encoder writes to

    def write(self, data: Any) -> int:
        data = bytes(data)
        end = self._at + len(data)
        self.data[self._at : end] = data
        self._at = end
        return len(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        self._at = offset if whence == 0 else (self._at + offset if whence == 1 else len(self.data) + offset)
        return self._at

    def tell(self) -> int:
        return self._at

    def flush(self) -> None:
        return None

    # Speech in, FLAC out

    def add(self, audio: bytes, rate: int) -> None:
        if self.closed:
            return
        samples = np.clip(at_output_rate(audio, rate), -32768, 32767).astype(np.int16)
        self._file.write(samples)
        self._file.flush()
        self.samples += samples.size
        self._more.set()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._file.close()
            self._more.set()

    async def parts(self):
        """The stream from its start: what there is now, then each new piece until it is closed."""
        sent = 0
        while True:
            if sent < len(self.data):
                part = bytes(self.data[sent:])
                sent += len(part)
                yield part
            elif self.closed:
                return
            else:
                self._more.clear()
                await self._more.wait()


def to_flac(chunks: list[tuple[bytes, int]], gain: float = 1.0) -> tuple[bytes, float]:
    """Speech audio (16-bit mono chunks at their sample rates) as 48 kHz mono FLAC. Returns (data, seconds)."""
    import soundfile

    parts = [at_output_rate(audio, rate) for audio, rate in chunks]
    samples = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    samples = np.clip(samples * gain, -32768, 32767).astype(np.int16)
    buffer = io.BytesIO()
    soundfile.write(buffer, samples, SAMPLE_RATE_OUT, format="FLAC", subtype="PCM_16")
    return buffer.getvalue(), samples.size / SAMPLE_RATE_OUT


LIMIT_KNEE = 0.5  # of full scale: below it the gain is plain, above it the level is eased towards full scale


def raise_level(audio: bytes, gain: float) -> bytes:
    """`audio` times `gain`, with loud parts eased in under full scale rather than cut off flat: the gain
    that lets speech from across a room through speech detection would clip a voice close by."""
    if gain == 1.0:
        return audio
    level = np.frombuffer(audio, dtype=np.int16).astype(np.float32) * (gain / 32768.0)
    size = np.abs(level)
    over = size > LIMIT_KNEE
    level[over] = np.sign(level[over]) * (LIMIT_KNEE + (1 - LIMIT_KNEE) * np.tanh((size[over] - LIMIT_KNEE) / (1 - LIMIT_KNEE)))
    return np.round(level * 32767).astype(np.int16).tobytes()


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
        self._stream: ReplyStream | None = None  # the reply being streamed to the device, if it is
        self._text: list[str] = []
        self._settle: asyncio.TimerHandle | None = None
        self._waiting_since: float | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def collecting(self) -> bool:
        return bool(self._chunks) or self._stream is not None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            if self._stream is None and not self._chunks:
                self._stream = self.session.start_reply_stream()  # None: collect it and send it whole
            if self._stream is not None:
                self._stream.add(frame.audio, frame.sample_rate)
            else:
                self._chunks.append((frame.audio, frame.sample_rate))
            self._arm()
            return  # the audio goes to the device as a file, not on down the pipeline
        if isinstance(frame, TTSTextFrame) and frame.text.strip():
            self._text.append(frame.text.strip())
        await self.push_frame(frame, direction)

    def cut(self) -> None:
        """The wake word again while a streamed reply plays: end the stream; the rest of it is dropped."""
        if self._stream is not None and not self._stream.closed:
            self._stream.cut = True
            self._stream.close()

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
        if self._stream is not None:
            stream, self._stream = self._stream, None
            self.session.finish_reply_stream(stream, text)
            return
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
        self._playback = asyncio.Lock()  # one announcement at a time; the next waits its turn
        self._reply_until = 0.0  # time.monotonic() when the reply the device is playing has finished
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
                speech.stt(session_prompt(config, runtime.registry, services)),
                pair.user(),
                preprocessor,
                TimingStartProcessor(timer),
                router,
                VoiceTurnTracker(self.state, preprocessor, retry=lambda: self.task.queue_frame(LLMRunFrame())),
                TimingEndProcessor(timer),
                HistoryTap(runtime),
                speech.tts(),
                self.output,
                pair.assistant(),
            ]
        )
        self.task = PipelineWorker(
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

    @asynccontextmanager
    async def _announcing(self):
        """Announcements and notifications play one at a time, after any conversation turn here, with
        the room's music paused meanwhile and resumed after (Bill, 2026-11-03)."""
        async with self._playback:
            waited = 0.0
            # someone is talking to it, or it is still playing their reply
            while (self.run is not None or time.monotonic() < self._reply_until) and waited < ANNOUNCE_WAIT_S:
                await asyncio.sleep(0.2)
                waited += 0.2
            music = self.services.music
            held = await music.hold(self.name) if music is not None else None
            try:
                yield
            finally:
                if held is not None and music is not None:
                    await music.release(held)

    async def play_announcement(self, url: str, text: str, seconds: float) -> None:
        """Play audio already encoded and served (an announcement to several rooms)."""
        async with self._announcing():
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
        self.output.cut()
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
        """For the arbiter: priority (a question open here, or a timer ringing here), and how loud this
        turn's speech is at this satellite, plus its configured offset."""
        priority = self.state.pending is not None or bool(self.services.timers.ringing(self.name))
        return priority, speech_level_db(self._scored_audio()) + self.settings.arbitration_offset_db

    def arbitration_levels(self) -> str:
        """What the score was made of, for the arbiter's decision log."""
        return describe_levels(self._scored_audio(), self.settings.arbitration_offset_db)

    def _scored_audio(self) -> bytes:
        """This turn's first window of audio, before `gain`: every satellite is scored on the same amount,
        however long it waited for the others."""
        if self.run is None:
            return b""
        window_s = self.arbiter.window_s if self.arbiter is not None else 0.5
        return bytes(self.run.raw[: int(window_s * SAMPLE_RATE_IN) * 2])

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
        self._record("speech")
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
            self._record("no-speech")
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

    def _record(self, ending: str) -> None:
        """Fault-finding (`record_dir`): this turn's microphone audio so far, after `gain`, as a WAV file."""
        if self.run is None or not self.settings.record_dir:
            return
        import wave
        from datetime import datetime
        from pathlib import Path

        try:
            folder = Path(self.settings.record_dir).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{self.name}-{datetime.now():%Y%m%d-%H%M%S}-{ending}.wav"
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(SAMPLE_RATE_IN)
                out.writeframes(bytes(self.run.audio))
        except OSError:
            logger.exception("Could not record the satellite turn", extra={"event": "satellite_record_failed", "device": self.name})

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

    def start_reply_stream(self) -> ReplyStream | None:
        """With `stream_replies` on and a turn open: give the device the reply's address at its first
        audio, so it plays the first sentence while the rest is synthesized. The turn ends here, as it
        does when a whole reply is sent; the reply counts as being delivered until the stream closes.

        Whether a question is open (so the device listens again with no wake word) is read now. A question
        that a tool raises after the model's first words have been spoken is missed; the wake word is
        needed for its answer.
        """
        from aioesphomeapi.model import VoiceAssistantEventType as E

        if self.run is None or not self.settings.stream_replies:
            return None
        try:
            name, stream = self.audio.open_stream()
        except Exception as exc:  # noqa: BLE001 - fall back to sending the reply whole
            logger.warning("Satellite reply stream not opened", extra={"event": "satellite_reply_failed", "device": self.name, "reason": str(exc) or type(exc).__name__})
            return None
        url = f"http://{self.audio_host}:{self.audio.port}/satellite-audio/{name}"
        follow_up = self.state.pending is not None
        self.delivering = True
        self.send(E.VOICE_ASSISTANT_INTENT_END, {"continue_conversation": "1" if follow_up else "0"})
        self.send(E.VOICE_ASSISTANT_TTS_START, {"text": ""})
        self.send(E.VOICE_ASSISTANT_TTS_END, {"url": url})
        self._status(state="replying")
        self._end_run()
        logger.info("Satellite reply stream started", extra={"event": "satellite_reply_stream", "device": self.name})
        return stream

    def finish_reply_stream(self, stream: ReplyStream, text: str) -> None:
        """The reply has all been spoken: close the stream and allow for what is left to play."""
        stream.close()
        if stream.cut:  # a new turn has the device now
            self.delivering = False
            return
        left = max(0.0, stream.seconds - (time.monotonic() - stream.started))
        self._reply_until = time.monotonic() + left + REPLY_GAP_S
        self.delivering = False
        self._status(state="replying", reply=text)
        self._idle_after(left)
        logger.info("Satellite reply", extra={"event": "satellite_reply", "device": self.name, "duration_s": round(stream.seconds, 1), "text": text, "kind": "streamed"})

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
                self._reply_until = time.monotonic() + seconds + REPLY_GAP_S
                self._end_run()
                self._idle_after(seconds)
                logger.info("Satellite reply", extra={"event": "satellite_reply", "device": self.name, "duration_s": round(seconds, 1), "text": text})
                return
            async with self._announcing():
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
        runner = asyncio.create_task(run_worker(self.task))
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
        self._playing: set[asyncio.Task] = set()  # announcements queued or playing on the satellites
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
        sent to each as the same audio. Returns the satellites it was sent to, without waiting for them to
        play it: each satellite queues it after its own turn, and when the asking satellite is one of them,
        that turn is the one waiting for this tool (2026-11-05: waiting here held its turn 27 s)."""
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
        for session in sessions:
            url = f"http://{session.audio_host}:{self.audio.port}/satellite-audio/{name}"
            task = asyncio.create_task(session.play_announcement(url, text, seconds), name=f"announce-{session.name}")
            self._playing.add(task)
            task.add_done_callback(self._playing.discard)
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
