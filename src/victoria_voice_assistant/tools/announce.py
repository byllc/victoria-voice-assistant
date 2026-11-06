"""The `announce` built-in (Stage 13): "announce dinner's ready", "tell the kitchen I'm on my way".

The message is said on the satellites in the rooms named (from the device registry, by name or alias),
or everywhere when no room is named. It is synthesized once and the same audio goes to each satellite,
close enough together for speech (not sample-accurate). Timers and notifications still go only to
their own room.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from victoria_voice_assistant.config.schema import ToolsConfig
from victoria_voice_assistant.tools.outcome import ToolOutcome

EVERYWHERE = {"everywhere", "all", "all rooms", "every room", "the whole house", "whole house", "house"}


class Announcer(Protocol):
    async def announce(self, text: str, rooms: list[str] | None = None) -> list[str]: ...


def _key(text: str) -> str:
    return re.sub(r"^(the|my|our)\s+", "", text.strip().lower())


def _rooms(asked: Any, tools: ToolsConfig) -> tuple[list[str] | None, list[str]]:
    """(rooms to announce in, or None for everywhere; names that match no room)."""
    names = [asked] if isinstance(asked, str) else list(asked or [])
    names = [n for part in names for n in re.split(r",|\band\b", str(part)) if n.strip()]
    if not names or any(_key(n) in EVERYWHERE for n in names):
        return None, []
    known = {_key(alias): room for room, config in tools.rooms.items() for alias in [room, *config.aliases]}
    found, unknown = [], []
    for name in names:
        room = known.get(_key(name))
        if room is None:
            unknown.append(name.strip())
        elif room not in found:
            found.append(room)
    return found, unknown


def _list(rooms: list[str]) -> str:
    named = [f"the {room}" for room in rooms]
    return named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]


async def announce(arguments: dict[str, Any], tools: ToolsConfig, announcer: Announcer) -> ToolOutcome:
    message = str(arguments.get("message") or "").strip()
    if not message:
        return ToolOutcome(result={"error": "no message"}, speak="What should I announce?")
    rooms, unknown = _rooms(arguments.get("rooms"), tools)
    if unknown:
        return ToolOutcome(result={"error": "unknown room", "rooms": unknown}, speak=f"I don't know a room called {unknown[0]}.", error=True)
    played = await announcer.announce(message, rooms)
    if not played:
        where = f"in {_list(rooms)}" if rooms else "anywhere"
        return ToolOutcome(result={"played": []}, speak=f"There's no satellite online {where}.", error=True)
    return ToolOutcome(
        result={"played": played, "message": message},
        speak="Announced everywhere." if rooms is None else f"Announced in {_list(rooms)}.",
    )
