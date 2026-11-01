"""The built-in music tool (architecture §8, built-in music; Stage 10) through Music Assistant.

Control goes through `music-assistant-client` over one connection to the configured server:
search, play, pause, resume, skip, volume and now playing. Music plays where the request says
(Bill, 2026-11-01): a room from `music.rooms` or any Music Assistant player by name ("in the
exercise room", "on the Mac mini"). A device with its own room (a satellite) uses that room; the
web client asks "Where should I play it?" the first time and then keeps using that player.
Search tries the sources in `source_order` (Plex, then Spotify) unless the request names one.
While someone talks to the assistant, music playing for that device is ducked to `duck_level`.
Replies are templates; no second model call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from victoria_voice_assistant.config.schema import MusicConfig
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10.0
FILLER = {"the", "a", "my", "our", "in", "on", "speaker", "speakers", "room", "music"}
SOURCES = {"plex": "Plex", "spotify": "Spotify"}
ACTIONS = ("play", "pause", "resume", "next", "previous", "volume", "volume_up", "volume_down", "now_playing", "stop")


def _key(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("’", "'").replace("'s", "s"))
    return {w for w in words if w not in FILLER}


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _the(name: str) -> str:
    """ "the Exercise room speaker", but "Bill's Mac mini" (no "the" before a possessive)."""
    first = name.split(" ", 1)[0]
    return name if first.endswith(("'s", "’s", "s'")) or name.startswith("[") else f"the {name}"


@dataclass
class Choice:
    """A playable search result."""

    kind: str  # artist, album, track, playlist
    name: str
    artist: str
    source: str
    uri: str

    def describe(self) -> str:
        if self.kind in ("album", "track") and self.artist:
            return f"{self.name} by {self.artist}"
        if self.kind == "playlist":
            return f"the {self.name} playlist"
        return self.name


class MusicBackend(Protocol):
    """What the tool needs from Music Assistant (the real client, or a stand-in in tests)."""

    async def players(self) -> list[dict[str, Any]]: ...  # player_id, name, available, volume, state, now (title)

    async def search(self, query: str) -> list[Choice]: ...

    async def sources(self) -> set[str]: ...

    async def play(self, player_id: str, uri: str) -> None: ...

    async def command(self, player_id: str, action: str, level: int | None = None) -> None: ...


class AssistantBackend:
    """Music Assistant through its Python client; connects on first use and reconnects after a drop."""

    def __init__(self, server: str, token: str) -> None:
        self.server = server
        self.token = token
        self._client: Any = None
        self._session: Any = None
        self._listener: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _connected(self) -> Any:
        async with self._lock:
            if self._client is not None and self._listener is not None and not self._listener.done():
                return self._client
            import aiohttp
            from music_assistant_client import MusicAssistantClient

            await self.close()
            self._session = aiohttp.ClientSession()
            client = MusicAssistantClient(self.server, self._session, token=self.token)
            try:
                await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_S)
                ready = asyncio.Event()
                self._listener = asyncio.get_running_loop().create_task(client.start_listening(ready), name="music-assistant")
                await asyncio.wait_for(ready.wait(), CONNECT_TIMEOUT_S)
            except BaseException:
                await self.close()
                raise
            self._client = client
            logger.info("Music Assistant connected", extra={"event": "music_connected"})
            return client

    async def players(self) -> list[dict[str, Any]]:
        client = await self._connected()
        found = []
        for player in client.players:
            queue = client.player_queues.get(player.active_source or "") or client.player_queues.get(player.player_id)
            current = player.current_media
            title = (current.title if current else None) or (queue.current_item.name if queue and queue.current_item else None)
            if current and current.artist and title:
                title = f"{title} by {current.artist}"
            found.append(
                {
                    "player_id": player.player_id,
                    "name": player.name,
                    "available": player.available,
                    "volume": player.volume_level,
                    "state": getattr(player.playback_state, "value", player.playback_state),
                    "now": title,
                }
            )
        return found

    async def sources(self) -> set[str]:
        client = await self._connected()
        return {provider.domain for provider in client.providers if provider.available}

    async def search(self, query: str) -> list[Choice]:
        from music_assistant_models.enums import MediaType

        client = await self._connected()
        results = await client.music.search(
            query, [MediaType.PLAYLIST, MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK], limit=5
        )
        choices = []
        for kind, items in (("playlist", results.playlists), ("artist", results.artists), ("album", results.albums), ("track", results.tracks)):
            for item in items:
                mappings = getattr(item, "provider_mappings", None) or []
                domains = {m.provider_domain for m in mappings} or {getattr(item, "provider", "")}
                artist = ", ".join(a.name for a in (getattr(item, "artists", None) or [])[:2])
                for domain in domains:
                    choices.append(Choice(kind, item.name, artist, domain.split("--")[0], item.uri))
        return choices

    async def play(self, player_id: str, uri: str) -> None:
        from music_assistant_models.enums import QueueOption

        client = await self._connected()
        await client.player_queues.play_media(player_id, uri, option=QueueOption.REPLACE)

    async def command(self, player_id: str, action: str, level: int | None = None) -> None:
        client = await self._connected()
        queues, players = client.player_queues, client.players
        if action == "pause":
            await queues.pause(player_id)
        elif action == "resume":
            await queues.resume(player_id)
        elif action == "stop":
            await queues.stop(player_id)
        elif action == "next":
            await queues.next(player_id)
        elif action == "previous":
            await queues.previous(player_id)
        elif action == "volume" and level is not None:
            await players.volume_set(player_id, level)
        elif action == "volume_up":
            await players.volume_up(player_id)
        elif action == "volume_down":
            await players.volume_down(player_id)

    async def close(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            self._listener = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 - closing a dead connection
                pass
            self._client = None
        if self._session is not None:
            await self._session.close()
            self._session = None


def _similarity(query: set[str], choice: Choice) -> float:
    """How well a result covers the request: 3 for an exact name, else twice the share of the request's
    words found in its name and artist, plus a little when the whole name was said."""
    name, artist = _key(choice.name), _key(choice.artist)
    if not query:
        return 0.0
    if name == query:
        return 3.0
    coverage = len(query & (name | artist)) / len(query)
    return 2 * coverage + (0.5 if name and name <= query else 0)


SPOKEN_FILLER = re.compile(r"\b(some|any|a little|songs?|tracks?|music|the album|the song|please)\b", re.IGNORECASE)


def search_phrases(query: str) -> list[str]:
    """What to search for: the request without filler, then its parts ("Kind of Blue" / "Miles Davis")."""
    cleaned = " ".join(SPOKEN_FILLER.sub(" ", query).split())
    phrases = [cleaned]
    if re.search(r"\bby\b", cleaned, re.IGNORECASE):
        phrases += [part.strip() for part in re.split(r"\bby\b", cleaned, maxsplit=1, flags=re.IGNORECASE) if part.strip()]
    else:
        words = cleaned.split()
        if len(words) >= 4:
            phrases += [" ".join(words[:-2]), " ".join(words[-2:])]  # "Kind of Blue" + "Miles Davis"
    return list(dict.fromkeys(p for p in phrases if p))


def pick(query: str, choices: list[Choice], sources: list[str]) -> Choice | None:
    """The best result from the first source (in order) that has a good match."""
    wanted = _key(re.sub(r"\b(some|songs?|tracks?|music|by)\b", " ", query.lower()))
    playlist = "playlist" in wanted
    wanted.discard("playlist")
    kind_order = {"playlist": 0, "artist": 1, "album": 2, "track": 3} if playlist else {"artist": 0, "album": 1, "playlist": 2, "track": 3}
    for source in sources:
        scored = [(_similarity(wanted, c), -kind_order[c.kind], c) for c in choices if c.source == source]
        scored = [entry for entry in scored if entry[0] >= 1.0]  # at least half the words
        if scored:
            return max(scored, key=lambda entry: (entry[0], entry[1]))[2]
    return None


class MusicService:
    def __init__(self, config: MusicConfig | None, backend: MusicBackend | None = None) -> None:
        self.config = config
        self.backend = backend or (
            AssistantBackend(str(config.server).rstrip("/"), config.token or "") if config and config.server else None
        )
        self.last_player: dict[str, str] = {}  # device -> the player its music last went to
        self._ducked: dict[str, int] = {}  # player -> volume before ducking

    def update_settings(self, config: MusicConfig | None) -> None:
        self.config = config

    # Where to play ------------------------------------------------------------------------

    def _room_for_device(self, device: str) -> str | None:
        for name, room in (self.config.rooms if self.config else {}).items():
            if room.satellite and room.satellite == device:
                return name
        return None

    def _match_player(self, place: str, players: list[dict[str, Any]]) -> dict[str, Any] | None:
        """A room from `music.rooms` (by name or alias) or a Music Assistant player (by name or id)."""
        wanted = _key(place)
        if not wanted:
            return None
        by_id = {p["player_id"]: p for p in players}
        for name, room in (self.config.rooms if self.config else {}).items():
            if any(_key(n) and _key(n) <= wanted for n in [name, *room.aliases]):
                return by_id.get(room.player) or next((p for p in players if _key(p["name"]) == _key(room.player)), None)
        exact = [p for p in players if _key(p["name"]) and _key(p["name"]) <= wanted]
        partial = [p for p in players if wanted <= _key(p["name"])]
        found = exact or partial
        return max(found, key=lambda p: len(_key(p["name"]))) if found else None

    def _where(self, device: str, players: list[dict[str, Any]]) -> dict[str, Any] | None:
        room = self._room_for_device(device)
        if room:
            return self._match_player(room, players)
        last = self.last_player.get(device)
        return next((p for p in players if p["player_id"] == last), None)

    def _ask_where(self, arguments: dict[str, Any], players: list[dict[str, Any]]) -> ToolOutcome:
        names = [p["name"] for p in players if p["available"]][:5]
        question = f"Where should I play it: {', '.join(names[:-1])} or {names[-1]}?" if len(names) > 1 else "Where should I play it?"
        return ToolOutcome(
            result={"needs": "room", "players": names},
            speak=question,
            pending=PendingQuestion(kind="clarify", question=question, options=names),
        )

    # The tool -------------------------------------------------------------------------------

    async def handle(self, device: str, arguments: dict[str, Any]) -> ToolOutcome:
        if self.backend is None:
            return ToolOutcome(result={"error": "not configured"}, speak="Music isn't set up yet.")
        action = str(arguments.get("action") or "play").lower().replace(" ", "_").replace("-", "_")
        action = {"skip": "next", "unpause": "resume", "louder": "volume_up", "quieter": "volume_down", "whats_playing": "now_playing"}.get(action, action)
        place = str(arguments.get("room") or "").strip()
        try:
            players = await self.backend.players()
        except Exception as exc:  # noqa: BLE001 - down, refused, or the token rejected
            logger.warning("Music Assistant unreachable", extra={"event": "music_unreachable", "reason": str(exc) or type(exc).__name__})
            return ToolOutcome(result={"error": "music server unreachable"}, speak="I can't reach the music server right now.", error=True)
        if place:
            player = self._match_player(place, players)
            if player is None:
                return ToolOutcome(result={"error": f"no speaker called {place!r}"}, speak=f"I don't know a speaker called {place}.")
        elif action == "play":
            player = self._where(device, players)
            if player is None:
                return self._ask_where(arguments, players)
        else:
            playing = [p for p in players if p["state"] == "playing"]
            player = self._where(device, players) if len(playing) != 1 else playing[0]
            if player is None:
                if action == "now_playing":
                    return ToolOutcome(result={"playing": []}, speak="Nothing is playing.")
                return self._ask_where(arguments, players)
        if not player["available"]:
            return ToolOutcome(result={"error": "speaker offline"}, speak=f"{_cap(_the(player['name']))} isn't available right now.")
        try:
            if action == "play":
                return await self._play(device, player, arguments)
            return await self._control(device, player, action, arguments)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Music command failed", extra={"event": "music_failed", "action": action, "reason": str(exc) or type(exc).__name__})
            return ToolOutcome(result={"error": str(exc)}, speak="The music server couldn't do that.", error=True)

    async def _play(self, device: str, player: dict[str, Any], arguments: dict[str, Any]) -> ToolOutcome:
        query = str(arguments.get("query") or "").strip()
        if not query:
            await self.backend.command(player["player_id"], "resume")  # type: ignore[union-attr]
            self.last_player[device] = player["player_id"]
            return ToolOutcome(result={"resumed": player["player_id"]}, speak=f"Resuming on {_the(player['name'])}.")
        requested = str(arguments.get("source") or "").strip().lower() or None
        order = [requested] if requested else list(self.config.source_order if self.config else ["plex", "spotify"])
        available = await self.backend.sources()  # type: ignore[union-attr]
        if requested and requested not in available:
            label = SOURCES.get(requested, requested.title())
            return ToolOutcome(result={"error": f"{requested} not connected"}, speak=f"{label} isn't connected in Music Assistant.")
        searchable = [s for s in order if s in available]
        choices: list[Choice] = []
        choice = None
        for phrase in search_phrases(query):
            choices += await self.backend.search(phrase)  # type: ignore[union-attr]
            choice = pick(query, choices, searchable)
            if choice is not None and _similarity(_key(SPOKEN_FILLER.sub(" ", query)), choice) >= 2.5:
                break  # a full match; no need to search the parts
        if choice is None:
            where = f" on {SOURCES.get(requested, requested)}" if requested else ""
            return ToolOutcome(result={"found": None}, speak=f"I couldn't find {query}{where}.")
        await self.backend.play(player["player_id"], choice.uri)  # type: ignore[union-attr]
        self.last_player[device] = player["player_id"]
        logger.info("Music playing", extra={"event": "music_play", "kind": choice.kind, "source": choice.source})
        source = SOURCES.get(choice.source, choice.source.title())
        return ToolOutcome(
            result={"playing": choice.describe(), "source": choice.source, "player": player["name"]},
            speak=f"Playing {choice.describe()} from {source} on {_the(player['name'])}.",
        )

    async def _control(self, device: str, player: dict[str, Any], action: str, arguments: dict[str, Any]) -> ToolOutcome:
        name = player["name"]
        if action == "now_playing":
            if player["state"] == "playing" and player["now"]:
                return ToolOutcome(result={"now": player["now"]}, speak=f"{player['now']}, on {_the(name)}.")
            return ToolOutcome(result={"now": None}, speak=f"Nothing is playing on {_the(name)}.")
        if action not in ACTIONS:
            return ToolOutcome(result={"error": f"unknown action {action!r}"}, speak="I can play, pause, skip, or change the volume.")
        level = arguments.get("level")
        if action == "volume":
            if level is None:
                return ToolOutcome(result={"error": "no level"}, speak="To what volume?")
            level = max(0, min(100, int(level)))
        await self.backend.command(player["player_id"], action, level)  # type: ignore[union-attr]
        self.last_player[device] = player["player_id"]
        speak = {
            "pause": f"Paused on {_the(name)}.",
            "stop": f"Stopped on {_the(name)}.",
            "resume": f"Resuming on {_the(name)}.",
            "next": "Skipping.",
            "previous": "Going back.",
            "volume": f"Volume {level} on {_the(name)}.",
            "volume_up": f"Turned up {_the(name)}.",
            "volume_down": f"Turned down {_the(name)}.",
        }[action]
        return ToolOutcome(result={"done": action, "player": name}, speak=speak)

    # Ducking (architecture §8: music drops while someone talks to the assistant) -------------------

    async def duck(self, device: str) -> None:
        if self.backend is None or self.config is None:
            return
        try:
            players = await self.backend.players()
            player = self._where(device, players)
            if player is None or player["state"] != "playing" or player["player_id"] in self._ducked:
                return
            volume = player["volume"]
            if volume is None or volume <= self.config.duck_level:
                return
            self._ducked[player["player_id"]] = volume
            await self.backend.command(player["player_id"], "volume", self.config.duck_level)
            logger.info("Music ducked", extra={"event": "music_duck", "count": 1})
        except Exception as exc:  # noqa: BLE001 - ducking is best-effort; never block a voice turn
            logger.warning("Ducking failed", extra={"event": "music_duck_failed", "reason": str(exc) or type(exc).__name__})

    async def unduck(self, device: str) -> None:
        if self.backend is None or not self._ducked:
            return
        for player_id, volume in list(self._ducked.items()):
            self._ducked.pop(player_id, None)
            try:
                await self.backend.command(player_id, "volume", volume)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unducking failed", extra={"event": "music_duck_failed", "reason": str(exc) or type(exc).__name__})

    async def close(self) -> None:
        if isinstance(self.backend, AssistantBackend):
            await self.backend.close()
