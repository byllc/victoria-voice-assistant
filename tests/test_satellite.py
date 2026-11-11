from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile
from aioesphomeapi.model import VoiceAssistantEventType as E
from aioesphomeapi.model import VoiceAssistantTimerEventType as T
from pipecat.processors.frame_processor import FrameProcessor

from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import SatelliteConfig, TimersConfig, WeatherConfig
from victoria_voice_assistant.satellite import esphome
from victoria_voice_assistant.satellite.esphome import AudioServer, SatelliteSession, raise_level, to_flac
from victoria_voice_assistant.tools.outcome import PendingQuestion
from victoria_voice_assistant.tools.registry import Services
from victoria_voice_assistant.tools.timers import TimerEvent, TimerService
from victoria_voice_assistant.tools.weather import WeatherService


class FakeClient:
    """What the session sends to the device, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, dict[str, str] | None]] = []
        self.announcements: list[dict[str, Any]] = []
        self.timer_events: list[tuple[Any, ...]] = []
        self.lights: list[tuple[int, dict[str, Any]]] = []

    def send_voice_assistant_event(self, event: Any, data: dict[str, str] | None) -> None:
        self.events.append((event, data))

    async def send_voice_assistant_announcement_await_response(self, url: str, timeout: float, **kwargs: Any) -> None:
        self.announcements.append({"url": url, "timeout": timeout, **kwargs})

    def send_voice_assistant_timer_event(self, *args: Any) -> None:
        self.timer_events.append(args)

    def light_command(self, key: int, **kwargs: Any) -> None:
        self.lights.append((key, kwargs))

    def kinds(self) -> list[Any]:
        return [event for event, _ in self.events]


class FakeSpeech:
    """Pass-through stand-ins for Whisper and Kokoro: these tests don't load speech models."""

    def stt(self, _prompt: str | None = None) -> FrameProcessor:
        return FrameProcessor()

    def tts(self) -> FrameProcessor:
        return FrameProcessor()


def _speech(seconds: float, rate: int) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes()


def _session(example_config_dir: Path, timers: TimerService | None = None, ring_key: int | None = 42) -> tuple[SatelliteSession, FakeClient]:
    config = load_config(example_config_dir)
    services = Services(timers=timers or TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()))
    client = FakeClient()
    session = SatelliteSession(
        "office", SatelliteConfig(host="satellite.local"), client, config, services,
        FakeSpeech(), AudioServer(8767), "10.0.0.5", ring_key=ring_key,  # type: ignore[arg-type]
    )
    return session, client


def test_replies_are_48k_mono_flac() -> None:
    data, seconds = to_flac([(_speech(0.5, 24000), 24000), (_speech(0.5, 16000), 16000)])
    samples, rate = soundfile.read(io.BytesIO(data), dtype="int16")
    assert rate == 48000 and samples.ndim == 1
    assert seconds == pytest.approx(1.0, abs=0.01) and len(samples) == pytest.approx(48000, abs=10)


def test_quiet_microphone_audio_is_raised_without_wrapping() -> None:
    audio = np.array([100, -100, 5000, -5000], dtype=np.int16).tobytes()
    raised = np.frombuffer(raise_level(audio, 8.0), dtype=np.int16).tolist()
    assert raised[:2] == [800, -800]  # quiet audio: plain gain
    assert 30000 < raised[2] < 32767 and raised[3] == -raised[2]  # loud audio: eased in under full scale
    assert raise_level(audio, 1.0) is audio
    # Louder in is still louder out, all the way up: nothing is cut off flat.
    ramp = np.arange(0, 32768, 64, dtype=np.int16).tobytes()
    assert np.all(np.diff(np.frombuffer(raise_level(ramp, 120.0), dtype=np.int16)) >= 0)
    assert np.frombuffer(raise_level(ramp, 120.0), dtype=np.int16).max() <= 32767


def test_audio_server_serves_replies_then_forgets_them() -> None:
    now = [0.0]
    server = AudioServer(8767, clock=lambda: now[0])
    first = server.add(b"one")
    assert first.endswith(".flac") and server.get(first) == b"one"
    assert server.get("guessed.flac") is None
    now[0] = esphome.AUDIO_KEEP_S + 1
    second = server.add(b"two")
    assert server.get(first) is None and server.get(second) == b"two"


def test_a_turn_runs_from_wake_word_to_reply(example_config_dir: Path) -> None:
    async def scenario() -> tuple[SatelliteSession, FakeClient, list[Any]]:
        session, client = _session(example_config_dir)
        queued: list[Any] = []

        async def queue_frame(frame: Any) -> None:
            queued.append(frame)

        session.task.queue_frame = queue_frame  # type: ignore[method-assign]
        assert await session.handle_start("c1", 0, None, "Victoria") == 0  # audio over the API
        await session.handle_audio(b"\x10\x00" * 160)
        session.user_started()
        session.user_started()  # only the first start of speech is reported
        session.user_stopped()
        await session.handle_audio(b"\x10\x00" * 160)  # the turn is the model's now: not queued
        session.heard("what time is it")
        session.state.set_pending(PendingQuestion(kind="clarify", question="Which room?"))
        await session.deliver([(_speech(0.2, 24000), 24000)], "Which room?")
        return session, client, queued

    session, client, queued = asyncio.run(scenario())
    assert len(queued) == 1
    assert client.kinds() == [
        E.VOICE_ASSISTANT_RUN_START,
        E.VOICE_ASSISTANT_STT_START,
        E.VOICE_ASSISTANT_STT_VAD_START,
        E.VOICE_ASSISTANT_STT_VAD_END,
        E.VOICE_ASSISTANT_STT_END,
        E.VOICE_ASSISTANT_INTENT_START,
        E.VOICE_ASSISTANT_INTENT_END,
        E.VOICE_ASSISTANT_TTS_START,
        E.VOICE_ASSISTANT_TTS_END,
        E.VOICE_ASSISTANT_RUN_END,
    ]
    data = dict(client.events)
    assert data[E.VOICE_ASSISTANT_STT_END] == {"text": "what time is it"}
    assert data[E.VOICE_ASSISTANT_INTENT_END] == {"continue_conversation": "1"}  # a question is open: listen again
    url = data[E.VOICE_ASSISTANT_TTS_END]["url"]
    assert url.startswith("http://10.0.0.5:8767/satellite-audio/")
    assert session.audio.get(url.rsplit("/", 1)[1]) is not None
    assert session.run is None and not session.delivering and not client.announcements


def test_a_reply_with_no_turn_open_is_an_announcement(example_config_dir: Path) -> None:
    async def scenario() -> FakeClient:
        session, client = _session(example_config_dir)
        await session.deliver([(_speech(0.2, 24000), 24000)], "Your pasta timer is done.")
        await session.deliver([], "nothing to play")  # no audio, nothing sent
        return client

    client = asyncio.run(scenario())
    assert client.events == []
    [announcement] = client.announcements
    assert announcement["text"] == "Your pasta timer is done."
    assert announcement["start_conversation"] is False and announcement["timeout"] >= 30


def test_a_wake_with_no_speech_ends_quietly(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esphome, "NO_SPEECH_S", 0.05)

    async def scenario() -> tuple[SatelliteSession, FakeClient]:
        session, client = _session(example_config_dir)
        await session.handle_start("c1", 0, None, "Hey Viki")
        await asyncio.sleep(0.15)
        return session, client

    session, client = asyncio.run(scenario())
    assert client.kinds()[-2:] == [E.VOICE_ASSISTANT_STT_START, E.VOICE_ASSISTANT_RUN_END]  # no STT_VAD_END first
    assert session.run is None


def test_a_turn_is_recorded_only_when_a_folder_is_set(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import wave

    monkeypatch.setattr(esphome, "NO_SPEECH_S", 0.05)

    async def scenario(folder: str | None) -> None:
        session, _client = _session(example_config_dir)
        session.settings = session.settings.model_copy(update={"record_dir": folder, "gain": 2.0})
        await session.handle_start("c1", 0, None, "Victoria")
        await session.handle_audio(np.full(1600, 100, dtype=np.int16).tobytes())
        await asyncio.sleep(0.15)

    asyncio.run(scenario(None))
    assert not list(tmp_path.rglob("*.wav"))
    asyncio.run(scenario(str(tmp_path / "turns")))
    (recording,) = (tmp_path / "turns").iterdir()
    assert recording.name.startswith("office-") and recording.name.endswith("-no-speech.wav")
    with wave.open(str(recording)) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getnframes()) == (16000, 1, 1600)
        assert np.frombuffer(audio.readframes(1600), dtype=np.int16)[0] == 200  # after `gain`


def test_an_aborted_turn_sends_no_end(example_config_dir: Path) -> None:
    async def scenario() -> tuple[SatelliteSession, FakeClient]:
        session, client = _session(example_config_dir)
        await session.handle_start("c1", 0, None, "Victoria")
        await session.handle_stop(abort=True)
        return session, client

    session, client = asyncio.run(scenario())
    assert session.run is None and E.VOICE_ASSISTANT_RUN_END not in client.kinds()


def test_timers_are_mirrored_to_the_device(example_config_dir: Path) -> None:
    now = [1000.0]

    async def scenario() -> FakeClient:
        timers = TimerService(TimersConfig(), clock=lambda: now[0])
        session, client = _session(example_config_dir, timers)
        await timers.start_seconds("office", "pasta", 60)
        await timers.start_seconds("kitchen", "tea", 60)  # another device's: not sent
        await session.on_timer_event(TimerEvent("changed", "office"))
        now[0] += 10
        await session.on_timer_event(TimerEvent("changed", "office"))
        await session.on_timer_event(TimerEvent("changed", "kitchen"))
        await timers.cancel("office", "pasta")
        await session.on_timer_event(TimerEvent("changed", "office"))
        await timers.close()
        return client

    client = asyncio.run(scenario())
    started, updated, cancelled = client.timer_events
    assert started[0] == T.VOICE_ASSISTANT_TIMER_STARTED and started[2:] == ("pasta", 60, 60, True)
    assert updated[0] == T.VOICE_ASSISTANT_TIMER_UPDATED and updated[2:] == ("pasta", 60, 50, True)
    assert cancelled[0] == T.VOICE_ASSISTANT_TIMER_CANCELLED and cancelled[1] == started[1]


def test_unread_notifications_light_the_ring_until_read(example_config_dir: Path) -> None:
    async def scenario() -> FakeClient:
        session, client = _session(example_config_dir)
        await session.outlet.show([])  # on connect, nothing unread: the ring is turned off
        await session.outlet.show([object(), object()])  # type: ignore[list-item]
        await session.outlet.show([object()])  # type: ignore[list-item]  # still on: nothing sent
        await session.outlet.show([])
        return client

    client = asyncio.run(scenario())
    assert client.lights == [
        (42, {"state": False}),
        (42, {"state": True, "rgb": (1.0, 160 / 255, 0.0), "brightness": 0.6}),
        (42, {"state": False}),
    ]


def test_no_ring_or_no_colour_leaves_the_light_alone(example_config_dir: Path) -> None:
    async def scenario() -> list[FakeClient]:
        no_ring, first = _session(example_config_dir, ring_key=None)
        await no_ring.outlet.show([object()])  # type: ignore[list-item]
        off, second = _session(example_config_dir)
        off.settings = off.settings.model_copy(update={"pending_color": None})
        await off.outlet.show([object()])  # type: ignore[list-item]
        return [first, second]

    assert [client.lights for client in asyncio.run(scenario())] == [[], []]


def test_a_finished_timer_rings_on_the_device_which_then_owns_it(example_config_dir: Path) -> None:
    async def scenario() -> tuple[FakeClient, list[Any], list[Any]]:
        timers = TimerService(TimersConfig())
        session, client = _session(example_config_dir, timers)
        await timers.start_seconds("office", "tea", 0.05)
        await asyncio.sleep(0.2)  # it rings
        ringing = timers.ringing("office")
        await session.on_timer_event(TimerEvent("ring", "office"))
        left = timers.timers("office")
        await timers.close()
        return client, ringing, left

    client, ringing, left = asyncio.run(scenario())
    assert len(ringing) == 1
    assert [event[0] for event in client.timer_events] == [T.VOICE_ASSISTANT_TIMER_FINISHED]
    assert left == []  # the device's "stop" word or button stops it; nothing tells us when


def test_satellites_follow_config_reloads(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[str] = []

    async def fake_run(self: esphome.SatelliteLink) -> None:
        runs.append(self.name)
        await asyncio.Event().wait()  # "connected" until cancelled

    async def no_server(self: AudioServer) -> None:
        return None

    monkeypatch.setattr(esphome.SatelliteLink, "run", fake_run)
    monkeypatch.setattr(AudioServer, "start", no_server)
    monkeypatch.setattr(AudioServer, "close", no_server)
    base = load_config(example_config_dir)

    def with_satellites(**satellites: dict) -> Any:
        tools = base.tools.model_copy(update={"satellites": {n: SatelliteConfig(**s) for n, s in satellites.items()}})
        return base.model_copy(update={"tools": tools})

    async def scenario() -> list[Any]:
        manager = esphome.SatelliteManager(with_satellites(), Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig())), FakeSpeech())  # type: ignore[arg-type]
        await manager.start()
        seen = [sorted(manager.links)]  # none configured: nothing starts
        await manager.update(with_satellites(office={"host": "o.local"}, kitchen={"host": "k.local"}))
        await asyncio.sleep(0)
        office = manager.links["office"]
        seen.append(sorted(manager.links))
        await manager.update(with_satellites(office={"host": "o.local", "gain": 12}, kitchen={"host": "k2.local"}))
        await asyncio.sleep(0)
        seen.append((manager.links["office"] is office, manager.links["office"].settings.gain))  # same connection, new gain
        await manager.update(with_satellites(kitchen={"host": "k2.local"}))
        seen.append(sorted(manager.links))
        await manager.close()
        seen.append(sorted(manager.links))
        return seen

    seen = asyncio.run(scenario())
    assert seen == [[], ["kitchen", "office"], (True, 12.0), ["kitchen"], []]
    assert runs == ["office", "kitchen", "kitchen"]  # the kitchen reconnected for its new host


def test_the_board_tracks_satellites_for_the_dashboard() -> None:
    from victoria_voice_assistant.satellite.status import SatelliteBoard

    changes: list[int] = []
    board = SatelliteBoard(clock=lambda: 5.0)
    board.add_listener(lambda: changes.append(1))
    board.configured({"office": "office", "kitchen-1": "kitchen"})
    board.update("office", online=True)
    board.update("office", online=True)  # no change: no update sent
    board.configured({"office": "office"})  # the kitchen satellite was removed
    assert [(s["name"], s["room"], s["online"], s["state"]) for s in board.snapshot()] == [("office", "office", True, "idle")]
    assert len(changes) == 3


def test_a_turn_shows_on_the_board(example_config_dir: Path) -> None:
    from victoria_voice_assistant.dashboard import dashboard
    from victoria_voice_assistant.satellite.status import SatelliteBoard

    async def scenario() -> list[tuple[str, Any, Any]]:
        session, _client = _session(example_config_dir)
        board = SatelliteBoard()
        session.services.satellites = board
        board.configured({"office": "office"})
        seen = []

        def note() -> None:
            (status,) = board.snapshot()
            seen.append((status["state"], status["request"], status["reply"]))

        board.add_listener(note)
        await session.handle_start("c1", 0, None, "Victoria")
        session.user_started()
        session.user_stopped()
        session.heard("what time is it")
        await session.deliver([(_speech(0.2, 24000), 24000)], "It's noon.")
        view = dashboard(session.services)["satellites"]
        assert view[0]["room"] == "office" and view[0]["timers"] == []
        return seen

    seen = asyncio.run(scenario())
    assert seen == [
        ("listening", None, None),
        ("thinking", None, None),
        ("thinking", "what time is it", None),
        ("replying", "what time is it", "It's noon."),
    ]


def test_an_offline_satellite_shows_red_and_posts_one_notice(example_config_dir: Path) -> None:
    from victoria_voice_assistant.health import SATELLITE_NOTICE_CHECKS, HealthMonitor
    from victoria_voice_assistant.satellite.status import SatelliteBoard

    posted: list[dict[str, Any]] = []

    class Notices:
        def post(self, **kwargs: Any) -> None:
            posted.append(kwargs)

    health = HealthMonitor(load_config(example_config_dir))
    board = SatelliteBoard()
    health.satellites, health.notifications = board, Notices()
    board.configured({"office": "office", "kitchen": "kitchen"})
    board.update("office", online=True)
    first = health.satellite_health()
    for _ in range(SATELLITE_NOTICE_CHECKS + 2):  # the kitchen stays offline
        health.satellite_health()
    board.update("kitchen", online=True)
    back = health.satellite_health()
    assert [(h.name, h.ok) for h in first] == [("satellite kitchen", False), ("satellite office", True)]
    assert len(posted) == 1 and posted[0]["headline"] == "The kitchen satellite is offline."
    assert posted[0]["priority"] == "low" and posted[0]["target"] == "web"
    assert all(h.ok for h in back) and health._offline == {}


def test_fleet_update_builds_once_then_uploads_to_each_satellite() -> None:
    from victoria_voice_assistant.satellite import fleet

    satellites = {"office": SatelliteConfig(host="o.local"), "kitchen": SatelliteConfig(host="k.local")}
    commands: list[list[str]] = []

    def run(command: list[str]) -> int:
        commands.append(command)
        return 1 if command[-1] == "k.local" else 0  # the kitchen's upload fails

    assert fleet.update(satellites, run=run) == {"office": True, "kitchen": False}
    assert [c[2:] for c in commands] == [[], ["--upload", "o.local"], ["--upload", "k.local"]]
    assert fleet.update(satellites, run=lambda _c: 1) == {}  # a failed build uploads nothing


def test_fleet_status_reads_each_version_and_reports_the_unreachable() -> None:
    from victoria_voice_assistant.satellite import fleet

    async def read(name: str, settings: SatelliteConfig) -> fleet.DeviceVersion:
        if name == "kitchen":
            return fleet.DeviceVersion(name, settings.host, False, error="Host is down")
        return fleet.DeviceVersion(name, settings.host, True, "v0.2.1", "2026.7.3", "2026-11-02 10:54")

    found = asyncio.run(fleet.versions({"office": SatelliteConfig(host="o.local"), "kitchen": SatelliteConfig(host="k.local")}, read=read))
    assert fleet.report(found).splitlines() == [
        "office (o.local): firmware v0.2.1, ESPHome 2026.7.3, built 2026-11-02 10:54",
        "kitchen (k.local): unreachable (Host is down)",
    ]
