from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import ToolsConfig
from victoria_voice_assistant.satellite import esphome
from victoria_voice_assistant.tools.announce import announce

TOOLS = ToolsConfig.model_validate({"rooms": {"kitchen": {"aliases": ["cooking"]}, "office": {}, "den": {}}})


class Recorder:
    def __init__(self, played: list[str] | None = None) -> None:
        self.calls: list[tuple[str, list[str] | None]] = []
        self.played = played

    async def announce(self, text: str, rooms: list[str] | None = None) -> list[str]:
        self.calls.append((text, rooms))
        return self.played if self.played is not None else (rooms or ["kitchen-sat", "office-sat"])


def run(coro):
    return asyncio.run(coro)


def test_announce_everywhere_or_in_the_rooms_named() -> None:
    recorder = Recorder()
    everywhere = run(announce({"message": "dinner's ready"}, TOOLS, recorder))
    named = run(announce({"message": "I'm on my way", "rooms": "the Kitchen and office"}, TOOLS, recorder))
    alias = run(announce({"message": "pizza", "rooms": ["cooking"]}, TOOLS, recorder))
    assert everywhere.speak == "Announced everywhere."
    assert named.speak == "Announced in the kitchen and the office."
    assert alias.speak == "Announced in the kitchen."
    assert recorder.calls == [("dinner's ready", None), ("I'm on my way", ["kitchen", "office"]), ("pizza", ["kitchen"])]


def test_unknown_rooms_no_message_and_no_satellite_online() -> None:
    assert run(announce({"message": "hi", "rooms": "garage"}, TOOLS, Recorder())).speak == "I don't know a room called garage."
    assert run(announce({"message": " "}, TOOLS, Recorder())).speak == "What should I announce?"
    assert run(announce({"message": "hi", "rooms": "den"}, TOOLS, Recorder(played=[]))).speak == "There's no satellite online in the den."


def test_the_manager_synthesizes_once_and_sends_the_same_audio_to_each(example_config_dir: Path, monkeypatch) -> None:
    from test_satellite import FakeSpeech

    synthesized: list[str] = []

    class Speech(FakeSpeech):
        async def synthesize(self, text: str) -> tuple[bytes, int]:
            synthesized.append(text)
            return (np.zeros(2400, dtype=np.int16)).tobytes(), 24000

    played: list[tuple[str, str, str]] = []

    def session(name: str) -> Any:
        async def play(url: str, text: str, seconds: float) -> None:
            played.append((name, url.rsplit("/", 1)[1], text))

        return SimpleNamespace(name=name, audio_host="10.0.0.5", play_announcement=play)

    async def scenario() -> list[str]:
        manager = esphome.SatelliteManager(load_config(example_config_dir), SimpleNamespace(satellites=None), Speech())  # type: ignore[arg-type]
        manager.links = {
            name: SimpleNamespace(session=session(name), settings=SimpleNamespace(room=room))
            for name, room in {"kitchen-1": "kitchen", "kitchen-2": "kitchen", "office": "office", "den": "den"}.items()
        }
        manager.links["den"].session = None  # offline
        reached = await manager.announce("dinner's ready", ["kitchen", "office", "den"])
        await asyncio.gather(*manager._playing)
        return reached

    reached = asyncio.run(scenario())
    assert reached == ["kitchen-1", "kitchen-2", "office"] and synthesized == ["dinner's ready"]
    assert len({audio for _, audio, _ in played}) == 1  # one file, the same for every satellite


def test_announcements_on_one_satellite_queue_and_wait_for_a_turn(example_config_dir: Path) -> None:
    from test_satellite import _session

    async def scenario() -> list[tuple[str, str, float]]:
        session, client = _session(example_config_dir)
        loop = asyncio.get_running_loop()
        log: list[tuple[str, str, float]] = []

        async def announcement(url: str, timeout: float, **kwargs: Any) -> None:
            log.append(("start", kwargs["text"], loop.time()))
            await asyncio.sleep(0.1)
            log.append(("end", kwargs["text"], loop.time()))

        client.send_voice_assistant_announcement_await_response = announcement  # type: ignore[method-assign]
        await session.handle_start("c1", 0, None, "Victoria")  # someone is talking to it
        first = asyncio.create_task(session.play_announcement("u1", "dinner's ready", 0.1))
        second = asyncio.create_task(session.play_announcement("u2", "the door is open", 0.1))
        await asyncio.sleep(0.3)
        assert log == []  # nothing plays over the turn
        session._end_run()
        await asyncio.gather(first, second)
        return log

    log = asyncio.run(scenario())
    assert [(event, text) for event, text, _ in log] == [
        ("start", "dinner's ready"), ("end", "dinner's ready"), ("start", "the door is open"), ("end", "the door is open")
    ]


def test_an_announcement_pauses_the_rooms_music_then_resumes_it(example_config_dir: Path) -> None:
    from test_satellite import _session

    calls: list[tuple[str, str]] = []

    class Music:
        async def hold(self, device: str) -> str | None:
            calls.append(("hold", device))
            return "player-1"

        async def release(self, player_id: str) -> None:
            calls.append(("release", player_id))

    async def scenario() -> None:
        session, client = _session(example_config_dir)

        async def announcement(url: str, timeout: float, **kwargs: Any) -> None:
            calls.append(("play", kwargs["text"]))

        client.send_voice_assistant_announcement_await_response = announcement  # type: ignore[method-assign]
        session.services.music = Music()  # type: ignore[assignment]
        await session.play_announcement("u1", "dinner's ready", 0.1)

    asyncio.run(scenario())
    assert calls == [("hold", "office"), ("play", "dinner's ready"), ("release", "player-1")]


def test_music_pauses_once_for_several_announcements_in_a_room() -> None:
    from victoria_voice_assistant.config.schema import MusicConfig
    from victoria_voice_assistant.tools.music import MusicService

    class Backend:
        def __init__(self, state: str) -> None:
            self.state = state
            self.commands: list[tuple[str, str]] = []

        async def players(self) -> list[dict[str, Any]]:
            return [{"player_id": "kitchen-speaker", "name": "Kitchen speaker", "available": True, "volume": 40, "state": self.state}]

        async def command(self, player_id: str, action: str, level: int | None = None) -> None:
            self.commands.append((player_id, action))

    config = MusicConfig.model_validate({"server": "http://music.test:8095", "rooms": {"kitchen": {"player": "kitchen-speaker", "satellite": "kitchen-1"}}})
    config.rooms["kitchen"].satellites = ["kitchen-1", "kitchen-2"]

    async def scenario(state: str) -> list[tuple[str, str]]:
        backend = Backend(state)
        music = MusicService(config, backend)  # type: ignore[arg-type]
        first = await music.hold("kitchen-1")
        second = await music.hold("kitchen-2")
        for held in (first, second):
            if held:
                await music.release(held)
        return backend.commands

    assert asyncio.run(scenario("playing")) == [("kitchen-speaker", "pause"), ("kitchen-speaker", "resume")]
    assert asyncio.run(scenario("paused")) == []  # nothing playing: nothing paused or resumed


def test_the_asking_satellite_gets_its_reply_first_then_the_announcement(example_config_dir: Path) -> None:
    """The announce tool runs inside the asking satellite's turn, and that satellite waits for its turn to
    end before playing an announcement. The tool must not wait for the playback, or the turn waits on itself
    (2026-11-05: 27 s, until the turn gave up)."""
    from test_satellite import FakeSpeech

    class Speech(FakeSpeech):
        async def synthesize(self, text: str) -> tuple[bytes, int]:
            return (np.zeros(2400, dtype=np.int16)).tobytes(), 24000

    order: list[str] = []
    turn_open = asyncio.Event()

    async def scenario() -> None:
        turn_open.set()

        async def play(url: str, text: str, seconds: float) -> None:
            while turn_open.is_set():  # as SatelliteSession waits for a turn in progress
                await asyncio.sleep(0.01)
            order.append(f"announcement: {text}")

        manager = esphome.SatelliteManager(load_config(example_config_dir), SimpleNamespace(satellites=None), Speech())  # type: ignore[arg-type]
        manager.links = {"office": SimpleNamespace(session=SimpleNamespace(name="office", audio_host="10.0.0.5", play_announcement=play), settings=SimpleNamespace(room="office"))}
        reached = await asyncio.wait_for(manager.announce("dinner's ready"), 1.0)  # the tool returns at once
        order.append("reply: Announced everywhere.")
        turn_open.clear()  # the turn ends
        await asyncio.wait_for(asyncio.gather(*manager._playing), 1.0)
        assert reached == ["office"]

    asyncio.run(scenario())
    assert order == ["reply: Announced everywhere.", "announcement: dinner's ready"]


def test_an_announcement_waits_for_the_reply_to_finish_playing(example_config_dir: Path) -> None:
    """The turn ends when its reply is sent, but the device is still playing it; an announcement sent then
    is dropped (2026-11-05). It waits until the reply has had time to play."""
    from test_satellite import _session

    async def scenario() -> tuple[float, float]:
        session, client = _session(example_config_dir)
        started: list[float] = []

        async def announcement(url: str, timeout: float, **kwargs: Any) -> None:
            started.append(time.monotonic())

        client.send_voice_assistant_announcement_await_response = announcement  # type: ignore[method-assign]
        await session.handle_start("c1", 0, None, "Victoria")
        queued = asyncio.create_task(session.play_announcement("u1", "dinner's ready", 0.1))
        await asyncio.sleep(0.05)
        await session.deliver([((np.zeros(9600, dtype=np.int16)).tobytes(), 24000)], "Announced everywhere.")  # 0.4 s
        replied = time.monotonic()
        await asyncio.wait_for(queued, 3.0)
        return replied, started[0]

    replied, announced = asyncio.run(scenario())
    assert announced - replied >= 0.4 + esphome.REPLY_GAP_S - 0.05  # after the reply, not over it
