from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from victoria_voice_assistant.agents.relay import relay
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import AgentConfig, MusicConfig, MusicRoomConfig, TimersConfig, WeatherConfig
from victoria_voice_assistant.tools.music import Choice, MusicService, pick, search_phrases
from victoria_voice_assistant.tools.registry import Services, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService


def run(coro):
    return asyncio.run(coro)


LIBRARY = [
    Choice("artist", "Miles Davis", "", "plex", "library://artist/1"),
    Choice("album", "Pangaea", "Miles Davis", "plex", "library://album/2"),
    Choice("track", "Footprints", "Miles Davis", "plex", "plex://track/3"),
    Choice("track", "Footprints", "Wayne Shorter", "spotify", "spotify://track/4"),
    Choice("playlist", "Discover Weekly", "", "spotify", "spotify://playlist/5"),
    Choice("playlist", "Dinner Jazz", "", "plex", "library://playlist/6"),
]


class FakeMusic:
    def __init__(self, sources=("plex", "spotify"), down: bool = False) -> None:
        self.down = down
        self._sources = set(sources)
        self.players_ = [
            {"player_id": "ex", "name": "Exercise room speaker", "available": True, "volume": 50, "state": "idle", "now": None},
            {"player_id": "mini", "name": "Bill’s Mac mini", "available": True, "volume": 30, "state": "idle", "now": None},
            {"player_id": "tv", "name": "[LG] webOS TV", "available": False, "volume": None, "state": "idle", "now": None},
        ]
        self.played: list[tuple[str, str]] = []
        self.commands: list[tuple[str, str, int | None]] = []
        self.searches: list[str] = []

    async def players(self) -> list[dict[str, Any]]:
        if self.down:
            raise ConnectionError("refused")
        return self.players_

    async def sources(self) -> set[str]:
        return self._sources

    async def search(self, query: str) -> list[Choice]:
        self.searches.append(query)
        words = set(query.lower().split())
        return [c for c in LIBRARY if words & set(f"{c.name} {c.artist}".lower().split())]

    async def play(self, player_id: str, uri: str) -> None:
        self.played.append((player_id, uri))
        for p in self.players_:
            if p["player_id"] == player_id:
                p["state"], p["now"] = "playing", uri

    async def command(self, player_id: str, action: str, level: int | None = None) -> None:
        self.commands.append((player_id, action, level))
        for p in self.players_:
            if p["player_id"] == player_id and action == "volume":
                p["volume"] = level


def service(backend: FakeMusic | None = None, **settings) -> tuple[MusicService, FakeMusic]:
    backend = backend or FakeMusic()
    config = MusicConfig(server="http://ma.test:8095", token="t", source_order=["plex", "spotify"], **settings)
    return MusicService(config, backend), backend


def test_search_phrases() -> None:
    assert search_phrases("some Miles Davis") == ["Miles Davis"]
    assert search_phrases("Footprints by Miles Davis") == ["Footprints by Miles Davis", "Footprints", "Miles Davis"]
    assert search_phrases("Kind of Blue Miles Davis") == ["Kind of Blue Miles Davis", "Kind of Blue", "Miles Davis"]


@pytest.mark.parametrize(
    ("query", "sources", "expected"),
    [
        ("Miles Davis", ["plex", "spotify"], "library://artist/1"),
        ("Footprints by Miles Davis", ["plex", "spotify"], "plex://track/3"),  # the track, not the artist
        ("Footprints", ["spotify"], "spotify://track/4"),
        ("Pangaea", ["plex"], "library://album/2"),
        ("dinner jazz playlist", ["plex", "spotify"], "library://playlist/6"),
        ("Discover Weekly", ["plex"], None),  # only on Spotify
    ],
)
def test_pick(query, sources, expected) -> None:
    choice = pick(query, LIBRARY, sources)
    assert (choice.uri if choice else None) == expected


def test_with_no_place_it_plays_only_on_the_device_that_heard() -> None:
    music, backend = service()
    refused = run(music.handle("web", {"action": "play", "query": "Miles Davis"}))  # no speaker of its own
    assert refused.speak == "I can't play music on this device yet. Name a speaker, like the exercise room."
    assert refused.pending is None and backend.played == []  # never "where should I play it?"
    played = run(music.handle("web", {"action": "play", "query": "Miles Davis", "room": "the exercise room"}))
    assert played.speak == "Playing Miles Davis from Plex on the Exercise room speaker."
    again = run(music.handle("web", {"action": "play", "query": "Pangaea"}))  # not the last one either
    assert again.speak.startswith("I can't play music on this device")
    skipped = run(music.handle("web", {"action": "next"}))  # controls still act on what's playing
    assert skipped.speak == "Skipping." and backend.played == [("ex", "library://artist/1")]


def test_a_browser_with_its_speaker_off_asks_what_when_nothing_is_named() -> None:
    music, backend = service()

    async def send(message: dict) -> None:
        pass

    music.set_speaker("laptop", None, send)
    asked = run(music.handle("web", {"action": "play"}, client_id="laptop"))
    assert asked.speak == "What should I play?" and asked.pending is not None


def test_rooms_aliases_and_a_satellites_own_room() -> None:
    rooms = {
        "gym": MusicRoomConfig(player="Exercise room speaker", aliases=["workout room"]),
        "office": MusicRoomConfig(player="mini", satellite="office-sat"),
    }
    music, backend = service(rooms=rooms)
    run(music.handle("web", {"action": "play", "query": "Miles Davis", "room": "workout room"}))
    run(music.handle("office-sat", {"action": "play", "query": "Pangaea"}))  # its own room, no question
    on_mini = run(music.handle("web", {"action": "play", "query": "Pangaea", "room": "mac mini"}))
    assert backend.played == [("ex", "library://artist/1"), ("mini", "library://album/2"), ("mini", "library://album/2")]
    assert on_mini.speak.endswith("on Bill’s Mac mini.")


def test_sources() -> None:
    music, backend = service(FakeMusic(sources=("plex",)))
    no_spotify = run(music.handle("web", {"action": "play", "query": "Discover Weekly", "source": "spotify", "room": "exercise room"}))
    assert no_spotify.speak == "Spotify isn't connected in Music Assistant."
    missing = run(music.handle("web", {"action": "play", "query": "Discover Weekly", "room": "exercise room"}))
    assert missing.speak == "I couldn't find Discover Weekly." and backend.played == []


def test_controls_act_on_whats_playing() -> None:
    music, backend = service()
    run(music.handle("web", {"action": "play", "query": "Miles Davis", "room": "exercise room"}))
    said = [
        run(music.handle("kitchen", {"action": action, **extra})).speak
        for action, extra in [("pause", {}), ("resume", {}), ("skip", {}), ("previous", {}), ("volume", {"level": 140}), ("volume_down", {})]
    ]
    assert said == [
        "Paused on the Exercise room speaker.", "Resuming on the Exercise room speaker.", "Skipping.", "Going back.",
        "Volume 100 on the Exercise room speaker.", "Turned down the Exercise room speaker.",
    ]
    assert [c[1] for c in backend.commands] == ["pause", "resume", "next", "previous", "volume", "volume_down"]
    assert run(music.handle("kitchen", {"action": "now_playing"})).speak == "library://artist/1, on the Exercise room speaker."


def test_nothing_playing_unknown_offline_and_down() -> None:
    music, _ = service()
    assert run(music.handle("web", {"action": "now_playing"})).speak == "Nothing is playing."
    assert run(music.handle("web", {"action": "play", "query": "x", "room": "garage"})).speak == "I don't know a speaker called garage."
    assert run(music.handle("web", {"action": "play", "query": "x", "room": "tv"})).speak == "[LG] webOS TV isn't available right now."
    down, _ = service(FakeMusic(down=True))
    outcome = run(down.handle("web", {"action": "play", "query": "Miles Davis"}))
    assert outcome.speak == "I can't reach the music server right now." and outcome.error


def test_ducking_lowers_and_restores() -> None:
    music, backend = service(duck_level=20)
    run(music.handle("web", {"action": "play", "query": "Miles Davis", "room": "exercise room"}))
    run(music.duck("web"))
    run(music.duck("web"))  # once only
    ducked = backend.players_[0]["volume"]
    run(music.unduck("web"))
    assert ducked == 20 and backend.players_[0]["volume"] == 50
    assert [c for c in backend.commands if c[1] == "volume"] == [("ex", "volume", 20), ("ex", "volume", 50)]
    quiet, quiet_backend = service(duck_level=20)
    run(quiet.duck("web"))  # nothing playing for this device: nothing to duck
    assert quiet_backend.commands == []


def test_music_wiring(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUSIC_ASSISTANT_TOKEN", raising=False)
    services = Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()))
    entry = build_registry(load_config(example_config_dir).tools, load_config(example_config_dir).agents, services).get("music")
    assert entry.status == "auto_stub" and "MUSIC_ASSISTANT_TOKEN" in (entry.reason or "")
    monkeypatch.setenv("MUSIC_ASSISTANT_TOKEN", "t")
    config = load_config(example_config_dir)
    entry = build_registry(config.tools, config.agents, services).get("music")
    assert entry.status == "ready" and entry.reply == "template" and "source" in entry.properties


# The Alexa relay ------------------------------------------------------------------------------


class FakeHome:
    def __init__(self, entity_ids: list[str]) -> None:
        self.entity_ids = entity_ids
        self.services: list[tuple[str, str, dict]] = []

    async def entities(self):
        class E:
            def __init__(self, entity_id):
                self.entity_id = entity_id

        return [E(e) for e in self.entity_ids]

    async def call_service(self, domain, service, data):
        self.services.append((domain, service, data))


class FakeHub:
    def __init__(self, home: FakeHome) -> None:
        self.home = home

    def get(self, server, label):
        return self.home


def _alexa(targets: dict[str, str]) -> AgentConfig:
    return AgentConfig(name="alexa", kind="relay", via="ha_service", service="media_player.play_media", content_type="custom", targets=targets, reply="Asking Alexa.")


def _relay_setup(example_config_dir: Path, monkeypatch, entity_ids):
    monkeypatch.setenv("HA_TOKEN", "t")
    config = load_config(example_config_dir)
    home = FakeHome(entity_ids)
    services = Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()))
    services.home = FakeHub(home)  # type: ignore[assignment]
    return config, services, home


def test_relay_sends_the_words_to_the_rooms_echo(example_config_dir: Path, monkeypatch) -> None:
    config, services, home = _relay_setup(example_config_dir, monkeypatch, ["media_player.kitchen_echo", "media_player.office_echo"])
    agent = _alexa({"kitchen": "media_player.kitchen_echo", "office": "media_player.office_echo"})
    sent = run(relay(agent, "play some jazz in the kitchen", "web", config, services))
    asked = run(relay(agent, "what's the weather", "web", config, services))
    assert sent.speak == "Asking Alexa."
    assert home.services == [
        ("media_player", "play_media", {"entity_id": "media_player.kitchen_echo", "media_content_type": "custom", "media_content_id": "play some jazz"})
    ]
    assert asked.speak == "Which Alexa: kitchen or office?" and asked.pending is not None


def test_relay_not_set_up(example_config_dir: Path, monkeypatch) -> None:
    config, services, home = _relay_setup(example_config_dir, monkeypatch, [])
    no_targets = run(relay(_alexa({}), "play jazz", "web", config, services))
    missing = run(relay(_alexa({"kitchen": "media_player.kitchen_echo"}), "play jazz", "web", config, services))
    for outcome in (no_targets, missing):
        assert outcome.speak == "Alexa isn't set up yet." and "Alexa Media Player" in (outcome.chat or "")
    assert home.services == []


# The browser as a speaker ("play it here") ---------------------------------------------------------


def test_a_browser_speaker_plays_its_own_requests() -> None:
    music, backend = service()
    # Music Assistant gives the browser its own id and keeps the Sendspin client id as the output id.
    backend.players_.append({"player_id": "browser-1", "name": "Victoria (Safari on iPhone)", "available": True, "volume": 80, "state": "idle", "now": None, "outputs": ["sendspin-client-id"]})
    music.set_speaker("phone", "sendspin-client-id")
    here = run(music.handle("web", {"action": "play", "query": "Pangaea"}, client_id="phone"))
    other = run(music.handle("web", {"action": "play", "query": "Pangaea"}, client_id="laptop"))  # no player on that page
    named = run(music.handle("web", {"action": "play", "query": "Pangaea", "room": "exercise room"}, client_id="phone"))
    paused = run(music.handle("web", {"action": "pause"}, client_id="phone"))
    assert here.speak == "Playing Pangaea by Miles Davis from Plex here."
    assert other.speak == "Music isn't available in this browser. Name a speaker, like the exercise room."
    assert named.speak.endswith("on the Exercise room speaker.")  # a named place still wins
    assert paused.speak == "Paused here."  # its own speaker was playing
    assert backend.played[0] == ("browser-1", "library://album/2")
    music.set_speaker("phone", None)
    assert music.speakers == {}


def test_the_serializer_passes_the_speaker() -> None:
    from victoria_voice_assistant.chat.serializer import ChatJsonSerializer

    serializer = ChatJsonSerializer()

    async def scenario():
        return [
            (await serializer.deserialize(text)).message
            for text in ['{"type": "music_speaker", "player_id": "abc"}', '{"type": "music_speaker", "player_id": null}']
        ]

    assert run(scenario()) == [
        {"type": "music_speaker", "player_id": "abc", "can_play": False},
        {"type": "music_speaker", "player_id": None, "can_play": False},
    ]


def test_asked_from_a_browser_with_its_speaker_off_it_turns_on_and_plays_there() -> None:
    music, backend = service()
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def scenario():
        music.set_speaker("laptop", None, send)  # the page has the player, its speaker off
        outcome = await music.handle("web", {"action": "play", "query": "Pangaea"}, client_id="laptop")
        assert sent == [{"type": "music_start", "title": "Pangaea", "artist": "Miles Davis", "kind": "album"}]
        assert backend.played == []
        # The browser connects; Music Assistant lists it a moment later.
        music.set_speaker("laptop", "sendspin-laptop", send)
        await asyncio.sleep(0.05)
        backend.players_.append({"player_id": "browser-2", "name": "Victoria (Chrome on Mac)", "available": True, "volume": 80, "state": "idle", "now": None, "outputs": ["sendspin-laptop"]})
        await asyncio.wait_for(asyncio.gather(*music._tasks), 5)
        return outcome

    outcome = run(scenario())
    assert outcome.speak == "Playing Pangaea by Miles Davis from Plex here."
    assert outcome.pending is None  # no "where should I play it?"
    assert backend.played == [("browser-2", "library://album/2")]


def test_a_player_that_never_starts_is_said_so(monkeypatch: pytest.MonkeyPatch) -> None:
    import victoria_voice_assistant.tools.music as module

    class Silent(FakeMusic):
        async def play(self, player_id: str, uri: str) -> None:
            self.played.append((player_id, uri))  # queued, but the speaker never plays (AirPlay off)

    monkeypatch.setattr(module, "PLAY_CHECK_S", 0.3)
    music, backend = service(Silent())
    outcome = run(music.handle("web", {"action": "play", "query": "Pangaea", "room": "Mac mini"}))
    assert outcome.error
    assert outcome.speak == "Bill’s Mac mini didn't start playing. It may be off or unreachable."


def test_the_web_interface_relays_sendspin_only_when_signed_in() -> None:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from victoria_voice_assistant.web.app import create_web_app

    locked = TestClient(create_web_app(password="pw", static_dir=None, sendspin_url="ws://ma.test:8927/sendspin"))
    assert locked.get("/api/music").json() == {"speaker": False}  # not signed in
    with pytest.raises(WebSocketDisconnect):
        with locked.websocket_connect("/sendspin") as ws:
            ws.receive_text()
    no_music = TestClient(create_web_app(password="", static_dir=None, sendspin_url=None))
    assert no_music.get("/api/music").json() == {"speaker": False}
    with_music = TestClient(create_web_app(password="", static_dir=None, sendspin_url="ws://ma.test:8927/sendspin"))
    assert with_music.get("/api/music").json() == {"speaker": True}
    assert MusicConfig(server="http://ma.test:8095").sendspin() == "ws://ma.test:8927/sendspin"



def test_artwork_comes_only_from_the_music_server() -> None:
    from fastapi.testclient import TestClient

    from victoria_voice_assistant.web.app import create_web_app

    open_app = TestClient(create_web_app(password="", static_dir=None, sendspin_url="ws://ma.test:8927/sendspin"))
    assert open_app.get("/api/music/artwork", params={"url": "http://evil.test/x.png"}).status_code == 400
    assert open_app.get("/api/music/artwork", params={"url": "file:///etc/passwd"}).status_code == 400
    # By address too: Music Assistant reports artwork at its IP while the config names it.
    by_ip = TestClient(create_web_app(password="", static_dir=None, sendspin_url="ws://localhost:8927/sendspin"))
    assert by_ip.get("/api/music/artwork", params={"url": "http://127.0.0.1:1/a.png"}).status_code == 502  # allowed, nothing there
    locked = TestClient(create_web_app(password="pw", static_dir=None, sendspin_url="ws://ma.test:8927/sendspin"))
    assert locked.get("/api/music/artwork", params={"url": "http://ma.test:8095/a.png"}).status_code == 401


def test_the_page_is_never_served_stale(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from victoria_voice_assistant.web.app import create_web_app

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "assets" / "index-abc.js").write_text("x")
    client = TestClient(create_web_app(password="", static_dir=tmp_path))
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/assets/index-abc.js").headers["cache-control"]
