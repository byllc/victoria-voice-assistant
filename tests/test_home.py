from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import McpServerConfig, ReplyConfig, ToolConfig, EntityScope, TimersConfig, WeatherConfig
from victoria_voice_assistant.tools.home import (
    Confirmations,
    HaEntity,
    HomeAssistant,
    HomeControl,
    ServiceTool,
    entities_from_registries,
    resolve,
)
from victoria_voice_assistant.tools.registry import Services, ToolContext, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

SERVER = McpServerConfig(url="http://ha.test/api/mcp", token="t")


def entity(entity_id: str, name: str, labelled: bool = True, state: str = "off", area: str | None = None, **attributes) -> HaEntity:
    return HaEntity(entity_id=entity_id, name=name, area=area, labelled=labelled, state=state, attributes=attributes)


HOUSE = [
    entity("switch.bedroom_light", "bedroom light", area="Bedroom"),
    entity("light.piano_light", "piano light", area="Living room"),
    entity("light.kitchen_island", "kitchen island light", area="Kitchen"),
    entity("light.kitchen_cabinets", "Kitchen cabinets", area="Kitchen"),
    entity("light.porch", "Upper Porch Light", state="unavailable"),
    entity("switch.front_porch_outlet", "front porch outlet", labelled=False),
    entity("scene.bright_kitchen", "Bright kitchen", state="unknown"),
    entity("climate.hall", "hall thermostat", state="heat", current_temperature=68, temperature=70),
    entity("lock.front_door", "Front Door Lock", state="unlocked"),
    entity("lock.side_door", "Side Door Lock", labelled=False, state="locked"),
]


class FakeHome(HomeAssistant):
    """A stand-in Home Assistant: fixed entities, recorded intent and service calls, states from the entity list."""

    def __init__(self, entities=HOUSE, intent_reply: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.services: list[tuple[str, str, dict]] = []
        self.house = {e.entity_id: e for e in entities}

        async def intent(name: str, arguments: dict[str, Any]) -> str:
            self.calls.append((name, arguments))
            return intent_reply or json.dumps({"response_type": "action_done", "data": {"success": [{"name": arguments["name"]}], "failed": []}})

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                found = self.house[request.url.path.rsplit("/", 1)[-1]]
                return httpx.Response(200, json={"state": found.state, "attributes": found.attributes})
            domain, service = request.url.path.split("/")[-2:]
            self.services.append((domain, service, json.loads(request.content)))
            return httpx.Response(200, json=[])

        super().__init__(SERVER, intent_caller=intent, transport=httpx.MockTransport(respond))
        self._entities = list(entities)
        self._loaded_at = float("inf")  # never reload


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("target", "labelled", "unlabelled"),
    [
        ("the bedroom light", ["switch.bedroom_light"], []),
        ("bedroom lights", ["switch.bedroom_light"], []),
        ("kitchen", ["light.kitchen_island", "light.kitchen_cabinets"], []),  # an area
        ("the kitchen lights", ["light.kitchen_island", "light.kitchen_cabinets"], []),
        ("front porch outlet", [], ["switch.front_porch_outlet"]),
        ("front door", ["lock.front_door"], []),  # part of a name
        ("garage heater", [], []),
    ],
)
def test_resolve(target, labelled, unlabelled) -> None:
    found, refused = resolve(target, HOUSE)
    assert [e.entity_id for e in found] == labelled and [e.entity_id for e in refused] == unlabelled


def test_turn_on_and_off_name_the_entity_exactly() -> None:
    home = FakeHome()
    control = HomeControl(home)
    on = run(control.handle({"action": "turn_on", "target": "the bedroom light"}))
    off = run(control.handle({"action": "turn_off", "target": "kitchen lights"}))
    assert on.speak == "Turned on the bedroom light."
    assert off.speak == "Turned off the kitchen island light and Kitchen cabinets."
    assert home.calls[0] == ("HassTurnOn", {"name": "bedroom light", "domain": ["switch"]})
    assert [c[0] for c in home.calls[1:]] == ["HassTurnOff", "HassTurnOff"]


def test_only_voice_labelled_entities_are_controlled() -> None:
    home = FakeHome()
    outcome = run(HomeControl(home).handle({"action": "turn_on", "target": "front porch outlet"}))
    assert outcome.speak == "The front porch outlet isn't set up for voice control." and home.calls == []


def test_ambiguous_singular_asks_which_one() -> None:
    home = FakeHome()
    outcome = run(HomeControl(home).handle({"action": "turn_on", "target": "kitchen light"}))
    assert outcome.pending is not None and outcome.pending.kind == "clarify"
    assert outcome.speak == "Which one: kitchen island light and Kitchen cabinets?" and home.calls == []


def test_unavailable_scene_and_unknown() -> None:
    home = FakeHome()
    control = HomeControl(home)
    assert run(control.handle({"action": "turn_on", "target": "porch light"})).speak == "The Upper Porch Light isn't responding."
    assert run(control.handle({"action": "turn_on", "target": "bright kitchen"})).speak == "Done: Bright kitchen."
    assert run(control.handle({"action": "turn_on", "target": "garage heater"})).speak == "I couldn't find garage heater."


def test_set_brightness_and_temperature() -> None:
    home = FakeHome()
    control = HomeControl(home)
    light = run(control.handle({"action": "set", "target": "piano light", "value": "40%"}))
    heat = run(control.handle({"action": "set", "target": "hall thermostat", "value": "72 degrees"}))
    half = run(control.handle({"action": "set", "target": "piano light", "value": "half"}))
    assert light.speak == "Set the piano light to 40 percent."
    assert heat.speak == "Set the hall thermostat to 72 degrees."
    assert half.speak == "Set the piano light to 50 percent."
    assert home.calls[:2] == [
        ("HassLightSet", {"name": "piano light", "brightness": 40}),
        ("HassClimateSetTemperature", {"name": "hall thermostat", "temperature": 72}),
    ]
    assert run(control.handle({"action": "set", "target": "bedroom light", "value": "50"})).speak == "I can only turn the bedroom light on or off."


def test_query_reads_the_state() -> None:
    home = FakeHome()
    control = HomeControl(home)
    assert run(control.handle({"action": "query", "target": "bedroom light"})).speak == "The bedroom light is off."
    assert run(control.handle({"action": "query", "target": "hall thermostat"})).speak == "The hall thermostat is heat, set to 70. It's 68 degrees now."
    assert home.calls == []  # state reads go to the REST API, not the intents


def test_a_refused_intent_is_reported() -> None:
    home = FakeHome(intent_reply=json.dumps({"response_type": "action_done", "data": {"success": [], "failed": [{"name": "bedroom light"}]}}))
    outcome = run(HomeControl(home).handle({"action": "turn_on", "target": "bedroom light"}))
    assert outcome.speak == "Home Assistant couldn't do that for the bedroom light." and outcome.error


LOCK = ToolConfig(
    name="lock_door",
    description="Lock a door.",
    kind="ha_service",
    service="lock.lock",
    entities=EntityScope(label="voice", domains=["lock"]),
    confirm=True,
    reply=ReplyConfig(template="{name} is locked."),
)


def test_locks_need_a_spoken_yes() -> None:
    home = FakeHome()
    confirmations = Confirmations()
    tool = ServiceTool(LOCK, home, confirmations)
    asked = run(tool.handle({"target": "front door"}))
    assert asked.speak == "Lock the Front Door Lock?" and home.services == []
    name, arguments = asked.pending.on_yes
    assert name == "lock_door" and asked.pending.kind == "yes_no"
    assert "_confirm" not in json.dumps(asked.result)  # the code never reaches the model
    forged = run(tool.handle({"target": "front door", "_confirm": "guess"}))
    assert forged.pending is not None and home.services == []  # a made-up code just asks again
    done = run(tool.handle(arguments))  # the "yes"
    assert done.speak == "The Front Door Lock is locked."
    assert home.services == [("lock", "lock", {"entity_id": "lock.front_door"})]
    again = run(tool.handle(arguments))  # a code works once
    assert again.pending is not None and len(home.services) == 1


def test_confirmations_expire() -> None:
    now = [0.0]
    confirmations = Confirmations(clock=lambda: now[0])
    code = confirmations.issue("lock_door")
    now[0] += 31
    assert not confirmations.redeem("lock_door", code)
    assert not confirmations.redeem("other", confirmations.issue("lock_door"))


def test_unlabelled_lock_is_refused() -> None:
    home = FakeHome()
    outcome = run(ServiceTool(LOCK, home, Confirmations()).handle({"target": "side door"}))
    assert outcome.speak == "The Side Door Lock isn't set up for voice control." and home.services == []


def test_entities_from_registries() -> None:
    labels = [{"label_id": "voice", "name": "voice"}, {"label_id": "x", "name": "other"}]
    registry = [
        {"entity_id": "switch.a", "labels": ["voice"], "area_id": "bed", "aliases": ["lamp a"], "name": None},
        {"entity_id": "switch.b", "labels": ["x"], "area_id": None, "aliases": []},
        {"entity_id": "switch.c", "labels": ["voice"], "disabled_by": "user"},
    ]
    areas = [{"area_id": "bed", "name": "Bedroom"}]
    states = [
        {"entity_id": "switch.a", "state": "on", "attributes": {"friendly_name": "A"}},
        {"entity_id": "switch.b", "state": "off", "attributes": {"friendly_name": "B"}},
        {"entity_id": "switch.c", "state": "off", "attributes": {}},
    ]
    found = entities_from_registries("voice", labels, registry, areas, states)
    assert [(e.entity_id, e.name, e.labelled, e.area, e.aliases) for e in found] == [
        ("switch.a", "A", True, "Bedroom", ["lamp a"]),
        ("switch.b", "B", False, None, []),
    ]


def test_registry_wiring(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_TOKEN", raising=False)
    config = load_config(example_config_dir)
    services = Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()))
    registry = build_registry(config.tools, config.agents, services)
    stub = registry.get("home_control")
    assert stub.status == "auto_stub" and "HA_TOKEN" in (stub.reason or "")  # no token: setup steps instead
    monkeypatch.setenv("HA_TOKEN", "t")
    config = load_config(example_config_dir)
    registry = build_registry(config.tools, config.agents, services)
    for name in ("home_control", "lock_door"):
        entry = registry.get(name)
        assert entry.status == "ready" and entry.speaks_directly, name
    assert services.home is not None and len(services.home.instances) == 1  # one shared connection
