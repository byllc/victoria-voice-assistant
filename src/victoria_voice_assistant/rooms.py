"""The device registry (architecture §4, Stage 13): which room a device is in, and what that room has.

`tools.yaml` lists the rooms (`rooms:`, each with its player, Home Assistant area and Echo) and each
satellite names its room (`satellites.<name>.room`, by default the room with its own name). Music, the
Alexa relay and home control all ask here.
"""

from __future__ import annotations

from victoria_voice_assistant.config.schema import RoomConfig, ToolsConfig


def room_of(tools: ToolsConfig, device: str) -> str | None:
    """The room a device is in: a satellite's room, or a device named after a room."""
    satellite = tools.satellites.get(device)
    if satellite is not None:
        return satellite.room
    return device if device in tools.rooms else None


def room_config(tools: ToolsConfig, device: str) -> RoomConfig | None:
    room = room_of(tools, device)
    return tools.rooms.get(room) if room else None
