"""What each satellite is doing, for the dashboard's satellite view (architecture §4.1, Stage 13).

The satellite sessions report here (connected or not, the voice state, the latest request and reply);
the dashboard adds each one's room and running timers. Listeners hear about every change, so the web
clients' dashboards follow along. No ESPHome import: the dashboard reads this without the adapter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

VoiceState = Literal["idle", "listening", "thinking", "replying", "announcing"]


@dataclass
class SatelliteStatus:
    name: str
    room: str | None = None
    online: bool = False
    state: VoiceState = "idle"
    request: str | None = None  # the latest thing heard
    reply: str | None = None  # the latest thing said
    changed_at: float = 0.0


class SatelliteBoard:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._satellites: dict[str, SatelliteStatus] = {}
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def configured(self, rooms: dict[str, str | None]) -> None:
        """The satellites in `tools.yaml` now, with their rooms: new ones appear offline, removed ones go."""
        for name in list(self._satellites):
            if name not in rooms:
                del self._satellites[name]
        for name, room in rooms.items():
            self._satellites.setdefault(name, SatelliteStatus(name, changed_at=self._clock())).room = room
        self._changed()

    def update(self, name: str, **changes: Any) -> None:
        status = self._satellites.setdefault(name, SatelliteStatus(name))
        if all(getattr(status, key) == value for key, value in changes.items()):
            return
        for key, value in changes.items():
            setattr(status, key, value)
        status.changed_at = self._clock()
        self._changed()

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(status) for status in sorted(self._satellites.values(), key=lambda s: s.name)]

    def _changed(self) -> None:
        for listener in list(self._listeners):
            listener()
