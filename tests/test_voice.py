from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import LLMContextFrame, TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy, TurnAnalyzerUserTurnStopStrategy

from victoria_voice_assistant.app import create_app
from victoria_voice_assistant.chat.tools import SessionState, ToolRuntime
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import AgentsConfig, SpeechConfig, TimersConfig, ToolsConfig, WeatherConfig
from victoria_voice_assistant.tools.outcome import PendingQuestion
from victoria_voice_assistant.tools.registry import Services, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService
from victoria_voice_assistant.voice.bench import TurnResult, report
from victoria_voice_assistant.voice.monitor import monitor_loop_lag
from victoria_voice_assistant.voice.session import VoiceTurnPreprocessor, last_user_text, log_latency, user_params
from victoria_voice_assistant.web.app import _http_url, create_web_app

LOOPBACK = ("127.0.0.1", 50000)
AGENTS = AgentsConfig.model_validate({"default": "helper", "agents": [{"name": "helper", "kind": "model", "model": "fast"}]})


async def _ignore(_message: dict[str, Any]) -> None:
    return None


def test_speech_config_defaults_and_example(example_config_dir: Path) -> None:
    speech = load_config(example_config_dir).models.speech
    assert speech.stt.model == "mlx-community/whisper-large-v3-turbo"
    assert speech.turn.smart_turn and speech.turn.fallback_s == 1.8 and speech.turn.short_clip_s == 0.5
    assert SpeechConfig().vad.stop_s == 0.2


def test_turn_detection_follows_config() -> None:
    smart = user_params(SpeechConfig())
    assert isinstance(smart.user_turn_strategies.stop[0], TurnAnalyzerUserTurnStopStrategy)  # type: ignore[union-attr,index]
    plain = user_params(SpeechConfig.model_validate({"turn": {"smart_turn": False, "fallback_s": 0.9}}))
    assert isinstance(plain.user_turn_strategies.stop[0], SpeechTimeoutUserTurnStopStrategy)  # type: ignore[union-attr,index]


def test_last_user_text_reads_strings_and_parts() -> None:
    context = LLMContext(messages=[{"role": "system", "content": "x"}, {"role": "user", "content": "Stop."}])
    assert last_user_text(context) == "Stop."
    context.add_message({"role": "user", "content": [{"type": "text", "text": "yes"}]})
    assert last_user_text(context) == "yes"


def _voice_preprocessor(state: SessionState, timers: TimerService, context: LLMContext) -> VoiceTurnPreprocessor:
    services = Services(timers=timers, weather=WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})))
    registry = build_registry(ToolsConfig(), AGENTS, services)
    return VoiceTurnPreprocessor(state, ToolRuntime(registry, state, notify=_ignore), timers, context=context)


def test_voice_yes_is_spoken_without_a_model() -> None:
    from pipecat.tests.utils import run_test

    async def scenario():
        context = LLMContext(messages=[{"role": "user", "content": "Yes."}])
        state = SessionState()
        state.set_pending(PendingQuestion(kind="yes_no", question="Ask Helper?", on_yes=("ask_agent", {"agent": "helper", "task": "x"})))
        processor = _voice_preprocessor(state, TimerService(TimersConfig()), context)
        down, _ = await run_test(processor, frames_to_send=[LLMContextFrame(context=context)])
        return down, context

    down, context = asyncio.run(scenario())
    spoken = [frame for frame in down if isinstance(frame, TTSSpeakFrame)]
    assert [frame.text for frame in spoken] == ["Agents aren't set up yet."]
    assert not any(isinstance(frame, LLMContextFrame) for frame in down)  # the model is not called
    assert context.get_messages()[-1] == {"role": "assistant", "content": "Agents aren't set up yet."}


def test_voice_turn_without_a_pending_question_goes_to_the_model() -> None:
    from pipecat.tests.utils import run_test

    async def scenario():
        context = LLMContext(messages=[{"role": "user", "content": "What time is it?"}])
        processor = _voice_preprocessor(SessionState(), TimerService(TimersConfig()), context)
        down, _ = await run_test(processor, frames_to_send=[LLMContextFrame(context=context)])
        return down

    assert any(isinstance(frame, LLMContextFrame) for frame in asyncio.run(scenario()))


def test_latency_breakdown_is_logged_by_stage(caplog: pytest.LogCaptureFixture) -> None:
    from pipecat.observers.user_bot_latency_observer import (
        LatencyBreakdown,
        LatencyContribution,
        LatencyOwnerKind,
        MeasuredFrom,
    )

    breakdown = LatencyBreakdown(
        contributions=[
            LatencyContribution(key="transcription", label="STT", owner="whisper", owner_kind=LatencyOwnerKind.SERVICE, start_time=0, duration_secs=0.25),
            LatencyContribution(key="llm_inference", label="LLM", owner="fast", owner_kind=LatencyOwnerKind.SERVICE, start_time=0.25, duration_secs=0.5),
        ],
        measured_from=MeasuredFrom.USER_SILENCE,
        total_secs=0.75,
    )
    with caplog.at_level(logging.INFO):
        log_latency(breakdown, "pc-1")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "voice_latency")
    assert record.parts == {"transcription": 250.0, "llm_inference": 500.0}  # type: ignore[attr-defined]
    assert record.total_ms == 750.0  # type: ignore[attr-defined]


def test_offer_needs_speech_ready_and_a_real_sdp(example_config_dir: Path, tmp_path: Path) -> None:
    app = create_app(example_config_dir, tmp_path / "assistant.sqlite3")
    with TestClient(app, client=LOOPBACK) as client:
        assert client.get("/health").json()["speech"] == "off"
        assert client.post("/api/offer", json={"sdp": "v=0\r\nm=audio", "type": "offer"}).status_code == 503

        class Ready:
            ready = asyncio.Event()
            error = None

        Ready.ready.set()
        app.state.speech = Ready()
        response = client.post("/api/offer", json={"sdp": "bad", "type": "offer"})
        assert response.status_code == 400


def test_web_proxies_signaling_only_when_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"sdp": "answer", "type": "answer", "pc_id": "pc-1"})

    monkeypatch.setattr(
        "victoria_voice_assistant.web.app.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    client = TestClient(create_web_app(password="household", service_url="ws://127.0.0.1:8765/ws", static_dir=None))
    assert client.post("/api/offer", json={"sdp": "v=0", "type": "offer"}).status_code == 401
    client.post("/api/login", json={"password": "household"})
    assert client.post("/api/offer", json={"sdp": "v=0", "type": "offer"}).json()["pc_id"] == "pc-1"
    assert client.patch("/api/offer", json={"pc_id": "pc-1", "candidates": []}).status_code == 200
    assert [(r.method, str(r.url)) for r in seen] == [
        ("POST", "http://127.0.0.1:8765/api/offer"),
        ("PATCH", "http://127.0.0.1:8765/api/offer"),
    ]


def test_service_http_url_from_websocket_url() -> None:
    assert _http_url("ws://127.0.0.1:8765/ws") == "http://127.0.0.1:8765"
    assert _http_url("wss://host:1/ws") == "https://host:1"


def test_loop_stall_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    import time

    async def scenario() -> None:
        monitor = asyncio.create_task(monitor_loop_lag(tick_s=0.01, stall_ms=20))
        await asyncio.sleep(0.03)
        time.sleep(0.08)  # block the loop
        await asyncio.sleep(0.03)
        monitor.cancel()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert any(getattr(r, "event", "") == "loop_stall" for r in caplog.records)


def test_bench_report_compares_with_target() -> None:
    results = [
        TurnResult("What time is it?", 1200, server={"parts": {"transcription": 300.0}}, transcript="What time is it?"),
        TurnResult("Stop.", 2000, server={"parts": {"transcription": 200.0}}, transcript="Stop."),
        TurnResult("Skip.", None),
    ]
    text = report(results)
    assert "replies 2/3" in text and "1 within the 1500 ms target" in text
    assert "short commands: Stop. 2000 ms" in text


def test_resident_memory_is_read() -> None:
    from victoria_voice_assistant.voice.monitor import resident_mb

    value = asyncio.run(resident_mb())
    assert value is not None and value > 10


def test_short_clip_ends_turn_without_smart_turn() -> None:
    import numpy as np
    from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
    from pipecat.frames.frames import InputAudioRawFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    from victoria_voice_assistant.voice.session import ShortClipSmartTurn, ShortClipStopStrategy

    def run_turn(*segments_s: float, short_clip_s: float = 0.7) -> tuple[bool, int]:
        """Speak each segment, pausing after it; Smart Turn is asked at every pause."""
        analyzer = ShortClipSmartTurn()
        calls: list[int] = []
        analyzer._predict_endpoint = lambda audio: calls.append(1) or {"prediction": 0, "probability": 0.1}  # type: ignore[method-assign]
        strategy = ShortClipStopStrategy(turn_analyzer=analyzer, short_clip_s=short_clip_s)
        now, state = 1000.0, EndOfTurnState.INCOMPLETE
        for spoken_s in segments_s:
            # VAD reports speaking from start_secs into the speech until stop_secs of silence;
            # the audio arrives as 20 ms frames, timestamps deliberately meaningless.
            strategy.note_speech_started(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=now))
            for _ in range(round((spoken_s - 0.2 + 0.2) / 0.02)):
                strategy.note_audio(InputAudioRawFrame(audio=bytes(640), sample_rate=16000, num_channels=1), True)
            strategy.note_speech_stopped(VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=now))
            state, _ = analyzer._process_speech_segment([(0.0, np.ones(320, dtype=np.int16))])
            now += 1.0
        return state == EndOfTurnState.COMPLETE, len(calls)

    assert run_turn(0.4) == (True, 0)  # "stop": no model
    assert run_turn(1.2) == (False, 1)  # a sentence: Smart Turn decides
    assert run_turn(0.4, short_clip_s=0) == (False, 1)  # turned off
    assert run_turn(1.2, 0.4) == (False, 2)  # "set a timer for ... ten": a later short stretch still asks Smart Turn
