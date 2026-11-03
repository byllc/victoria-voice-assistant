from __future__ import annotations

import asyncio
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
        return await manager.announce("dinner's ready", ["kitchen", "office", "den"])

    reached = asyncio.run(scenario())
    assert reached == ["kitchen-1", "kitchen-2", "office"] and synthesized == ["dinner's ready"]
    assert len({audio for _, audio, _ in played}) == 1  # one file, the same for every satellite
