"""Silero VAD and Smart Turn share one ONNX session per model across voice sessions (Stage 13)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import onnxruntime
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer

import victoria_voice_assistant.voice.models  # noqa: F401 - installs the shared sessions


SPEECH = Path(__file__).parent / "data" / "turn_off_the_lights.wav"  # macOS `say`, 16 kHz mono


def _speech() -> bytes:
    with wave.open(str(SPEECH)) as clip:
        return clip.readframes(clip.getnframes())


def _vad() -> SileroVADAnalyzer:
    vad = SileroVADAnalyzer()
    vad.set_sample_rate(16000)  # as the pipeline does when audio starts
    return vad


def _confidences(vad: SileroVADAnalyzer, audio: bytes) -> list[float]:
    step = vad.num_frames_required() * 2
    return [float(np.asarray(vad.voice_confidence(audio[i : i + step])).item()) for i in range(0, len(audio) - step + 1, step)]


def test_analyzers_share_one_session_per_model():
    first, second = _vad(), _vad()
    assert first._model.session is second._model.session
    assert first._model is not second._model  # the recurrent state stays per analyzer
    assert LocalSmartTurnAnalyzerV3()._session is LocalSmartTurnAnalyzerV3()._session


def test_interleaved_vads_score_as_if_alone():
    """Two satellites' audio, frame by frame in turn, scores exactly as each alone."""
    audio_a = _speech()
    audio_b = np.zeros(8000, dtype=np.int16).tobytes() + audio_a  # the same words, half a second later
    alone_a = _confidences(_vad(), audio_a)
    alone_b = _confidences(_vad(), audio_b)
    assert max(alone_a) > 0.9 and min(alone_a) < 0.1  # real speech and pauses, so the state matters

    vad_a, vad_b = _vad(), _vad()
    step = vad_a.num_frames_required() * 2
    together_a, together_b = [], []
    for i in range(0, min(len(audio_a), len(audio_b)) - step + 1, step):
        together_a.append(float(np.asarray(vad_a.voice_confidence(audio_a[i : i + step])).item()))
        together_b.append(float(np.asarray(vad_b.voice_confidence(audio_b[i : i + step])).item()))
    assert together_a == alone_a[: len(together_a)]
    assert together_b == alone_b[: len(together_b)]


def test_smart_turn_matches_its_own_session():
    """The shared Smart Turn session predicts what a private one does."""
    audio = np.frombuffer(_speech(), dtype=np.int16).astype(np.float32) / 32768
    shared = LocalSmartTurnAnalyzerV3()
    private = LocalSmartTurnAnalyzerV3()
    private._session = onnxruntime.InferenceSession(
        shared._session._model_path, providers=["CPUExecutionProvider"]
    )
    assert private._session is not shared._session
    assert shared._predict_endpoint(audio)["probability"] == private._predict_endpoint(audio)["probability"]
