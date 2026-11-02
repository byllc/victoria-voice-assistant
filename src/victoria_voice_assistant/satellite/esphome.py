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
        return None


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
    ) -> None:
        self.name = name
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
                VoiceTurnTracker(self.state, preprocessor),
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
        self.run.timers.append(asyncio.get_running_loop().call_later(NO_SPEECH_S, self._no_speech))
        return 0  # stream the audio over this connection

    async def handle_audio(self, data: bytes, _extra: bytes | None = None) -> None:
        if self.run is None or not self.run.listening:
            return
        frame = InputAudioRawFrame(audio=raise_level(data, self.settings.gain), sample_rate=SAMPLE_RATE_IN, num_channels=1)
        await self.task.queue_frame(frame)

    async def handle_stop(self, abort: bool) -> None:
        if self.run is not None and abort:
            logger.info("Satellite turn aborted", extra={"event": "satellite_turn_aborted", "device": self.name})
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
        self.send(E.VOICE_ASSISTANT_STT_VAD_END)
        self.run.timers.append(asyncio.get_running_loop().call_later(REPLY_WAIT_S, self._no_reply))

    def heard(self, text: str) -> None:
        from aioesphomeapi.model import VoiceAssistantEventType as E

        if self.run is None:
            return
        self.send(E.VOICE_ASSISTANT_STT_END, {"text": text})
        self.send(E.VOICE_ASSISTANT_INTENT_START)  # the LED ring shows it thinking

    def _no_speech(self) -> None:
        from aioesphomeapi.model import VoiceAssistantEventType as E

        if self.run is not None and not self.run.heard_speech:
            logger.info("Satellite heard no speech", extra={"event": "satellite_no_speech", "device": self.name})
            self.send(E.VOICE_ASSISTANT_STT_VAD_END)
            self._end_run()

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
                self._end_run()
                logger.info("Satellite reply", extra={"event": "satellite_reply", "device": self.name, "duration_s": round(seconds, 1), "text": text})
                return
            logger.info("Satellite announcement", extra={"event": "satellite_announce", "device": self.name, "duration_s": round(seconds, 1), "text": text})
            await self.client.send_voice_assistant_announcement_await_response(
                url, max(30.0, seconds + 15.0), text=text, start_conversation=follow_up
            )
        except Exception as exc:  # noqa: BLE001 - a reply that can't be sent is logged, never raised into the pipeline
            logger.warning("Satellite reply failed", extra={"event": "satellite_reply_failed", "device": self.name, "reason": str(exc) or type(exc).__name__})
        finally:
            self.delivering = False

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
                for timer in timers.ringing(self.name):
                    self.client.send_voice_assistant_timer_event(
                        T.VOICE_ASSISTANT_TIMER_FINISHED, timer.id, timer.name, int(timer.duration_s), 0, False
                    )
                    self._known_timers.discard(timer.id)
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

    def __init__(self, name: str, settings: SatelliteConfig, config: AppConfig, services: Services, speech: SpeechModels, audio: AudioServer) -> None:
        self.name = name
        self.settings = settings
        self.config = config
        self.services = services
        self.speech = speech
        self.audio = audio
        self.connected = False

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
                session = SatelliteSession(
                    self.name, self.settings, client, self.config, self.services, self.speech, self.audio,
                    await asyncio.to_thread(local_address, self.settings.host),
                )
                await session.run_until_disconnected(disconnected)
                logger.warning("Satellite disconnected", extra={"event": "satellite_disconnected", "device": self.name})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - offline, refused, wrong key: retry
                logger.warning("Satellite unreachable", extra={"event": "satellite_unreachable", "device": self.name, "reason": str(exc) or type(exc).__name__})
            finally:
                self.connected = False
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)


class SatelliteManager:
    """Every satellite in `tools.yaml`, and the audio server they fetch replies from."""

    def __init__(self, config: AppConfig, services: Services, speech: SpeechModels) -> None:
        self.config = config
        self.services = services
        self.speech = speech
        satellites = config.tools.satellites
        port = next(iter(satellites.values())).audio_port if satellites else 8767
        self.audio = AudioServer(port)
        self.links = [SatelliteLink(name, s, config, services, speech, self.audio) for name, s in satellites.items()]
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if not self.links:
            return
        await self.audio.start()
        self._tasks = [asyncio.create_task(link.run(), name=f"satellite-{link.name}") for link in self.links]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.audio.close()
