from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from victoria_voice_assistant.agents.relay import relay
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import AgentConfig, ToolsConfig
from victoria_voice_assistant.rooms import room_config, room_of
from victoria_voice_assistant.tools.home import HomeControl
from test_home import HOUSE, FakeHome
from test_music import _relay_setup


def _tools(**data) -> ToolsConfig:
    return ToolsConfig.model_validate(data)


def test_satellites_belong_to_rooms_and_several_can_share_one() -> None:
    tools = _tools(
        rooms={"kitchen": {"aliases": ["cooking"], "player": "Kitchen speaker", "area": "Kitchen"}, "office": {"area": "Office"}},
        satellites={
            "kitchen-1": {"host": "a.local", "room": "kitchen"},
            "kitchen-2": {"host": "b.local", "room": "kitchen"},
            "office": {"host": "c.local"},  # its own name is its room
        },
        music={"server": "http://music.test:8095"},
    )
    assert room_of(tools, "kitchen-1") == room_of(tools, "kitchen-2") == "kitchen"
    assert room_of(tools, "office") == "office" and room_config(tools, "office").area == "Office"
    assert room_of(tools, "web") is None  # a browser with no room
    # The music service reads its rooms from the registry: rooms with a player, and their satellites.
    assert list(tools.music.rooms) == ["kitchen"]
    assert tools.music.rooms["kitchen"].satellites == ["kitchen-1", "kitchen-2"]
    assert tools.music.rooms["kitchen"].aliases == ["cooking"]


def test_the_older_music_rooms_fold_into_the_registry() -> None:
    tools = _tools(
        music={"server": "http://music.test:8095", "rooms": {"den": {"player": "Den speaker", "satellite": "sat-3", "aliases": ["tv room"]}}},
        satellites={"sat-3": {"host": "c.local"}},
    )
    assert tools.rooms["den"].player == "Den speaker" and tools.rooms["den"].aliases == ["tv room"]
    assert room_of(tools, "sat-3") == "den" and tools.music.rooms["den"].satellites == ["sat-3"]


def test_turn_off_the_lights_means_the_asking_rooms_area() -> None:
    home = FakeHome()
    control = HomeControl(home)
    here = asyncio.run(control.handle({"action": "turn_off", "target": "the lights"}, area="Kitchen"))
    nowhere = asyncio.run(control.handle({"action": "turn_off", "target": "the lights"}))
    named = asyncio.run(control.handle({"action": "turn_on", "target": "bedroom light"}, area="Kitchen"))
    assert here.speak == "Turned off the kitchen island light and Kitchen cabinets."
    assert nowhere.speak != here.speak  # no room: nothing to go on
    assert named.speak == "Turned on the bedroom light."  # a named device still wins over the room
    assert len(HOUSE) and home.calls[-1][1]["name"] == "bedroom light"


def test_relay_finds_the_asking_rooms_echo_in_the_registry(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, services, home = _relay_setup(example_config_dir, monkeypatch, ["media_player.kitchen_echo", "media_player.office_echo"])
    tools = config.tools.model_copy(update={
        "rooms": {**config.tools.rooms, **_tools(rooms={"kitchen": {"echo": "media_player.kitchen_echo"}, "office": {"echo": "media_player.office_echo"}}).rooms},
        "satellites": _tools(satellites={"office-sat": {"host": "o.local", "room": "office"}}).satellites,
    })
    config = config.model_copy(update={"tools": tools})
    alexa = AgentConfig(name="alexa", kind="relay", via="ha_service", service="media_player.play_media", content_type="custom", reply="Asking Alexa.")
    sent = asyncio.run(relay(alexa, "what's on my calendar", "office-sat", config, services))
    assert sent.speak == "Asking Alexa." and home.services[-1][2]["entity_id"] == "media_player.office_echo"


def test_the_example_config_has_a_registry(example_config_dir: Path) -> None:
    tools = load_config(example_config_dir).tools
    assert isinstance(tools.rooms, dict)
