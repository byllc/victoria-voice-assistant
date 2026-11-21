"""Every place the assistant reaches into Pipecat's internals, in one module.

Pipecat 1.15's public interface didn't cover these, so each one uses a private name or replaces a
module attribute. An upgrade can rename any of them without an error at import, which would break
voice silently. `INTERNALS` lists them, `missing_internals()` checks they still exist, and
`tests/test_pipecat_internals.py` fails with the list when one is gone. Add nothing here without
adding it to `INTERNALS`, and use Pipecat's private names nowhere else.

1. One Kokoro for every session: `pipecat.services.kokoro.tts.Kokoro` is replaced with a cached
   factory, because `KokoroTTSService` builds its own ONNX session per instance (about 300 MB each)
   and takes no ready-made one. `_ensure_model_files` downloads the model files before the first use.
2. One ONNX session per model file for Silero VAD and Smart Turn: the `onnxruntime` name inside
   `pipecat.audio.vad.silero` and `ort` inside `...smart_turn.local_smart_turn_v3` are replaced with
   a caching wrapper, because every analyzer loads its own session and neither takes a shared one.
3. Whisper's `initial_prompt`: `WhisperSTTServiceMLX` doesn't expose it, so `run_stt` is copied with
   the one extra argument. The copy uses `_settings`, `_handle_transcription` and `_user_id`.
4. Ending a short clip's turn without Smart Turn: `_process_speech_segment` is overridden on the
   analyzer, and the stop strategy overrides `_handle_vad_user_started_speaking`,
   `_handle_input_audio` and `_handle_vad_user_stopped_speaking` and reads `_vad_user_speaking`,
   because the strategy has no public hook for "this stretch of speech was short".
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncGenerator, Callable
from functools import lru_cache

import pipecat.services.kokoro.tts as kokoro_tts
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TurnMetricsData
from pipecat.services.kokoro.tts import KOKORO_CACHE_DIR, _ensure_model_files
from pipecat.services.whisper.stt import WhisperSTTServiceMLX
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy

logger = logging.getLogger(__name__)

# (module, name inside it or "Class.attribute", what breaks without it). Instance attributes set in
# a constructor can't be checked on the class; those are checked on an instance in the test.
INTERNALS: tuple[tuple[str, str, str], ...] = (
    ("pipecat.services.kokoro.tts", "Kokoro", "the shared Kokoro: each voice session would load its own model"),
    ("pipecat.services.kokoro.tts", "_ensure_model_files", "Kokoro's model files are not downloaded at startup"),
    ("pipecat.services.kokoro.tts", "KOKORO_CACHE_DIR", "where Kokoro's model files are"),
    ("pipecat.audio.vad.silero", "onnxruntime", "the shared Silero VAD session: one per voice session instead"),
    ("pipecat.audio.turn.smart_turn.local_smart_turn_v3", "ort", "the shared Smart Turn session: one per voice session instead"),
    ("pipecat.services.whisper.stt", "WhisperSTTServiceMLX.run_stt", "speech-to-text with the agent names as Whisper's prompt"),
    ("pipecat.services.whisper.stt", "WhisperSTTServiceMLX._handle_transcription", "the copied run_stt"),
    ("pipecat.services.whisper.stt", "WhisperSTTServiceMLX.start_processing_metrics", "the copied run_stt"),
    ("pipecat.services.whisper.stt", "WhisperSTTServiceMLX.stop_processing_metrics", "the copied run_stt"),
    ("pipecat.audio.turn.smart_turn.local_smart_turn_v3", "LocalSmartTurnAnalyzerV3._process_speech_segment", "short clips ending their turn without Smart Turn"),
    ("pipecat.turns.user_stop", "TurnAnalyzerUserTurnStopStrategy._handle_vad_user_started_speaking", "short-clip timing"),
    ("pipecat.turns.user_stop", "TurnAnalyzerUserTurnStopStrategy._handle_input_audio", "short-clip timing"),
    ("pipecat.turns.user_stop", "TurnAnalyzerUserTurnStopStrategy._handle_vad_user_stopped_speaking", "short-clip timing"),
)
# Attributes Pipecat sets on instances, by class name here: checked on a built object in the test.
INSTANCE_INTERNALS: dict[str, tuple[str, ...]] = {
    "PromptedWhisperSTT": ("_settings", "_user_id"),
    "ShortClipStopStrategy": ("_vad_user_speaking",),
}


def missing_internals() -> list[str]:
    """The entries of `INTERNALS` this Pipecat no longer has, each as "module.name: what breaks"."""
    missing = []
    for module_name, path, why in INTERNALS:
        try:
            target = importlib.import_module(module_name)
            for part in path.split("."):
                target = getattr(target, part)
        except (ImportError, AttributeError):
            missing.append(f"{module_name}.{path}: {why}")
    return missing


KOKORO_MODEL = KOKORO_CACHE_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = KOKORO_CACHE_DIR / "voices-v1.0.bin"


def ensure_kokoro_files() -> None:
    _ensure_model_files(KOKORO_MODEL, KOKORO_VOICES)


_kokoro_factory = kokoro_tts.Kokoro


@lru_cache(maxsize=2)
def shared_kokoro(model_path: str, voices_path: str):
    return _kokoro_factory(model_path, voices_path)


# KokoroTTSService builds its own ONNX session per instance (~300 MB each). Route that
# construction through a cache so every voice session shares one.
kokoro_tts.Kokoro = shared_kokoro  # type: ignore[assignment]


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


class PromptedWhisperSTT(WhisperSTTServiceMLX):
    """Whisper with an initial prompt. `drop(text, prompt)` may reject a transcript before it's sent on."""

    def __init__(self, *, prompt: str | None = None, drop: Callable[[str, str | None], bool] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.prompt = prompt
        self.drop = drop

    async def run_stt_with_prompt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
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
            if self.drop is not None and self.drop(text, self.prompt):
                logger.info("Dropped a transcript that was only Whisper's prompt", extra={"event": "stt_prompt_echo_dropped"})
                text = ""
            if text.strip():
                await self._handle_transcription(text, True, language)
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), language)
        except Exception as exc:  # noqa: BLE001 - as Pipecat: report, don't crash the session
            yield ErrorFrame(error=f"Unknown error occurred: {exc}")


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
