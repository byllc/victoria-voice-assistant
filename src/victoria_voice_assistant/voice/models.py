"""Speech models shared by every voice session, with one lock per model (architecture §4.2).

One Whisper and one Kokoro serve all devices. MLX models shouldn't be called from
several threads at once, so each model has a lock; a second room waits its turn
and the wait is logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from functools import lru_cache

import pipecat.services.kokoro.tts as kokoro_tts
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.kokoro.tts import KOKORO_CACHE_DIR, KokoroTTSService, _ensure_model_files
from pipecat.services.whisper.stt import WhisperSTTServiceMLX
from pipecat.transcriptions.language import Language

from victoria_voice_assistant.config.schema import AgentsConfig, SpeechConfig

logger = logging.getLogger(__name__)

KOKORO_MODEL = KOKORO_CACHE_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = KOKORO_CACHE_DIR / "voices-v1.0.bin"
# Log a lock wait longer than this: it means two rooms wanted the same model at once.
LOCK_WAIT_LOG_MS = 50

_kokoro_factory = kokoro_tts.Kokoro


@lru_cache(maxsize=2)
def _shared_kokoro(model_path: str, voices_path: str):
    return _kokoro_factory(model_path, voices_path)


# KokoroTTSService builds its own ONNX session per instance (~300 MB each). Route that
# construction through a cache so every voice session shares one.
kokoro_tts.Kokoro = _shared_kokoro  # type: ignore[assignment]


class _SharedOnnxRuntime:
    """onnxruntime as seen by Pipecat's Silero VAD and Smart Turn: one InferenceSession per model file.

    Each voice session (every satellite, every browser) builds its own VAD and Smart Turn
    analyzer, and each loaded its own ONNX session, about 19 MB a pair. Neither keeps stream
    state in the session: Silero's recurrent state lives on its wrapper, per analyzer, and Smart
    Turn runs each segment on its own. ONNX Runtime allows concurrent `run` calls on one session,
    so the sessions are shared (architecture §4.2) and everything else passes through.
    """

    def __init__(self, module) -> None:
        self._module = module
        self._sessions: dict[tuple, object] = {}

    def __getattr__(self, name: str):
        return getattr(self._module, name)

    def InferenceSession(self, path, sess_options=None, providers=None, **kwargs):  # noqa: N802 - mirrors onnxruntime
        key = (str(path), tuple(providers or ()))
        session = self._sessions.get(key)
        if session is None:
            session = self._module.InferenceSession(path, sess_options=sess_options, providers=providers, **kwargs)
            self._sessions[key] = session
        return session


def _share_turn_models() -> _SharedOnnxRuntime:
    import onnxruntime
    import pipecat.audio.turn.smart_turn.local_smart_turn_v3 as smart_turn
    import pipecat.audio.vad.silero as silero

    shared = _SharedOnnxRuntime(onnxruntime)
    smart_turn.ort = shared  # type: ignore[assignment]
    silero.onnxruntime = shared  # type: ignore[assignment]
    return shared


shared_onnx = _share_turn_models()


class SpeechModels:
    """Loads the speech models once, before any voice session is accepted."""

    def __init__(self, config: SpeechConfig) -> None:
        self.config = config
        self.whisper_lock = asyncio.Lock()
        self.kokoro_lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.error: str | None = None

    async def load(self) -> None:
        """Load Whisper and Kokoro in parallel, off the event loop."""
        started = time.perf_counter()
        try:
            await asyncio.gather(
                asyncio.to_thread(self._load_whisper),
                asyncio.to_thread(self._load_kokoro),
            )
        except Exception as exc:  # noqa: BLE001 - reported through /health and the offer endpoint
            self.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Speech models failed to load", extra={"event": "speech_load_failed"})
            return
        self.ready.set()
        logger.info(
            "Speech models loaded",
            extra={
                "event": "speech_ready",
                "model": self.config.stt.model,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )

    def _load_whisper(self) -> None:
        import mlx.core as mx
        import numpy as np
        from mlx_whisper.transcribe import ModelHolder, transcribe

        ModelHolder.get_model(self.config.stt.model, mx.float16)
        # One short pass compiles the kernels so the first real request isn't slower.
        transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=self.config.stt.model, language="en")

    def _load_kokoro(self) -> None:
        _ensure_model_files(KOKORO_MODEL, KOKORO_VOICES)
        kokoro = _shared_kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        if self.config.tts.voice not in kokoro.voices:
            raise ValueError(f"Kokoro voice {self.config.tts.voice!r} is not in {KOKORO_VOICES}")
        kokoro.create(".", voice=self.config.tts.voice, lang="en-us")

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """Speech for `text` outside any voice session (an announcement to several rooms, synthesized
        once): 16-bit mono audio and its sample rate, under the same lock the sessions use."""
        import numpy as np

        kokoro = _shared_kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        await _acquire(self.kokoro_lock, "kokoro")
        try:
            samples, rate = await asyncio.to_thread(
                kokoro.create, text, voice=self.config.tts.voice, speed=self.config.tts.speed, lang="en-us"
            )
        finally:
            self.kokoro_lock.release()
        return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes(), rate

    def stt(self, prompt: str | None = None) -> LockedWhisperSTT:
        return LockedWhisperSTT(
            self.whisper_lock,
            prompt=prompt,
            settings=WhisperSTTServiceMLX.Settings(
                model=self.config.stt.model,
                language=Language(self.config.stt.language),
            ),
        )

    def tts(self) -> LockedKokoroTTS:
        return LockedKokoroTTS(
            self.kokoro_lock,
            model_path=str(KOKORO_MODEL),
            voices_path=str(KOKORO_VOICES),
            settings=KokoroTTSService.Settings(voice=self.config.tts.voice, speed=self.config.tts.speed),
        )


async def _acquire(lock: asyncio.Lock, model: str) -> None:
    started = time.perf_counter()
    await lock.acquire()
    waited = (time.perf_counter() - started) * 1000
    if waited > LOCK_WAIT_LOG_MS:
        logger.info("Waited for a speech model", extra={"event": "model_lock_wait", "model": model, "duration_ms": round(waited, 1)})


def whisper_prompt(agents: AgentsConfig) -> str | None:
    """Agent names for Whisper's initial prompt, so "ask Hermes" isn't heard as "ask Herman" (architecture §5).

    Only the names: aliases are the mis-hearings, not what we want written.
    """
    names = [agent.name[:1].upper() + agent.name[1:] for agent in agents.agents]
    return f"Victoria. Ask {', ask '.join(names)}." if names else None


class LockedWhisperSTT(WhisperSTTServiceMLX):
    def __init__(self, lock: asyncio.Lock, prompt: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model_lock = lock
        self.prompt = prompt

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        await _acquire(self._model_lock, "whisper")
        try:
            stt = self._run_stt_with_prompt(audio) if self.prompt else super().run_stt(audio)
            frames = [frame async for frame in stt]
        finally:
            self._model_lock.release()
        for frame in frames:
            yield frame

    async def _run_stt_with_prompt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Pipecat's `WhisperSTTServiceMLX.run_stt` plus `initial_prompt`, which that service doesn't expose.

        mlx_whisper.transcribe takes it; the steps and filters are otherwise Pipecat 1.15's.
        """
        try:
            import mlx_whisper
            import numpy as np
            from pipecat.utils.time import time_now_iso8601

            await self.start_processing_metrics()
            audio_float = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            language = self._settings.language
            chunk = await asyncio.to_thread(
                mlx_whisper.transcribe,
                audio_float,
                path_or_hf_repo=self._settings.model,
                temperature=self._settings.temperature,
                language=language,
                initial_prompt=self.prompt,
            )
            text = ""
            threshold = self._settings.no_speech_prob
            for segment in chunk.get("segments", []):
                if segment.get("compression_ratio", None) == 0.5555555555555556:
                    continue  # Pipecat's hallucination filter
                if threshold is not None and segment.get("no_speech_prob", 0.0) < threshold:
                    text += f"{segment.get('text', '')} "
            await self.stop_processing_metrics()
            if text.strip():
                await self._handle_transcription(text, True, language)
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), language)
        except Exception as exc:  # noqa: BLE001 - as Pipecat: report, don't crash the session
            yield ErrorFrame(error=f"Unknown error occurred: {exc}")


class LockedKokoroTTS(KokoroTTSService):
    def __init__(self, lock: asyncio.Lock, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model_lock = lock

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        # Held per sentence, not per reply, so two rooms speaking at once interleave. The
        # sentence is synthesized under the lock and yielded after, so a session cancelled
        # mid-reply can't leave the lock held. Kokoro renders a short sentence in one
        # batch anyway, so this doesn't delay the first audio.
        await _acquire(self._model_lock, "kokoro")
        try:
            frames = [frame async for frame in super().run_tts(text, context_id)]
        finally:
            self._model_lock.release()
        for frame in frames:
            yield frame
