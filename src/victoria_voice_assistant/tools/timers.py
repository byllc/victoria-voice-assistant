from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from victoria_voice_assistant.config.schema import TimersConfig
from victoria_voice_assistant.tools.duration import duration_label, parse_duration, speak_duration
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome

logger = logging.getLogger(__name__)

ORDINALS = {2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth"}
COUNTS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
# Timers finishing within this window are announced together.
ANNOUNCE_GROUP_S = 0.5


@dataclass
class Timer:
    id: str
    device: str
    name: str
    duration_s: float
    ends_at: float
    state: Literal["running", "ringing"]
    created_at: float
    ring_started_at: float | None = None

    def remaining(self, now: float) -> float:
        return max(0.0, self.ends_at - now)

    def to_client(self, now: float) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "remaining_s": round(self.remaining(now), 1),
            "duration_s": self.duration_s,
            "state": self.state,
        }


@dataclass
class TimerEvent:
    kind: Literal["changed", "ring", "ring_stopped"]
    device: str
    text: str | None = None
    names: list[str] | None = None


TimerListener = Callable[[TimerEvent], Awaitable[None]]


def normalize_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = re.sub(r"[^\w\s'-]", "", name.lower().replace("_", " ")).strip()
    cleaned = re.sub(r"^(the|my|a)\s+", "", cleaned)
    cleaned = re.sub(r"\s*timers?$", "", cleaned).strip()
    return cleaned or None


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _title(name: str) -> str:
    return name[:1].upper() + name[1:]


class TimerService:
    """Named countdown timers per device, persisted in SQLite and rung through listeners.

    With ``database_path=None`` timers live only in memory (used by ``tools test``).
    """

    def __init__(
        self,
        settings: TimersConfig | None,
        database_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or TimersConfig()
        self.database_path = database_path
        self.clock = clock
        self._timers: dict[str, Timer] = {}
        self._handles: dict[str, asyncio.TimerHandle] = {}
        self._announce: dict[str, asyncio.TimerHandle] = {}
        self._listeners: list[TimerListener] = []
        self._tasks: set[asyncio.Task] = set()

    # Lifecycle -----------------------------------------------------------

    def add_listener(self, listener: TimerListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: TimerListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def start(self) -> None:
        """Load persisted timers and schedule them; ring any that finished while stopped."""
        now = self.clock()
        for timer in self._load():
            if timer.state == "running" and timer.ends_at <= now:
                if now - timer.ends_at >= self.settings.ring_max_s:
                    logger.info(
                        "Timer finished while the service was down",
                        extra={"event": "timer_missed", "timer": timer.name},
                    )
                    self._delete(timer.id)
                    continue
                timer.state = "ringing"
                timer.ring_started_at = timer.ends_at
                self._save(timer)
            if timer.state == "ringing" and now - (timer.ring_started_at or now) >= self.settings.ring_max_s:
                self._delete(timer.id)
                continue
            self._timers[timer.id] = timer
            self._schedule(timer)
            if timer.state == "ringing":
                self._schedule_announce(timer.device)
        logger.info("Timers restored", extra={"event": "timers_restored", "count": len(self._timers)})

    async def close(self) -> None:
        for handle in [*self._handles.values(), *self._announce.values()]:
            handle.cancel()
        self._handles.clear()
        self._announce.clear()
        for task in list(self._tasks):
            task.cancel()

    def update_settings(self, settings: TimersConfig | None) -> None:
        self.settings = settings or TimersConfig()

    # Queries -------------------------------------------------------------

    def timers(self, device: str) -> list[Timer]:
        return sorted(
            (timer for timer in self._timers.values() if timer.device == device),
            key=lambda timer: timer.ends_at,
        )

    def ringing(self, device: str) -> list[Timer]:
        return [timer for timer in self.timers(device) if timer.state == "ringing"]

    def snapshot(self, device: str) -> list[dict[str, Any]]:
        now = self.clock()
        return [timer.to_client(now) for timer in self.timers(device)]

    def _find(self, device: str, name: str | None) -> list[Timer]:
        timers = self.timers(device)
        wanted = normalize_name(name)
        if wanted is None:
            return timers
        exact = [timer for timer in timers if timer.name == wanted]
        if exact:
            return exact
        words = set(wanted.split())
        return [
            timer
            for timer in timers
            if wanted in timer.name or timer.name in wanted or words & set(timer.name.split())
        ]

    # Tool entry point ------------------------------------------------------

    async def handle(self, device: str, arguments: dict[str, Any]) -> ToolOutcome:
        action = str(arguments.get("action") or "").strip().lower().replace(" ", "_")
        name = arguments.get("name") or None
        duration = arguments.get("duration") or None
        if action in ("start", "set", "create", "new"):
            return await self.start_timer(device, name, duration)
        if action in ("cancel", "delete", "remove", "clear"):
            return await self.cancel(device, name)
        if action in ("add_time", "add", "extend"):
            return await self.add_time(device, name, duration, sign=1)
        if action in ("remove_time", "subtract_time", "subtract", "reduce"):
            return await self.add_time(device, name, duration, sign=-1)
        if action in ("stop", "silence", "dismiss"):
            return await self.stop_ringing_outcome(device)
        if action in ("status", "check", "list", "query", ""):
            if action == "" and duration:
                return await self.start_timer(device, name, duration)
            return self.status(device, name)
        return ToolOutcome(
            result={"error": f"unknown action {action!r}"},
            speak="I can start, cancel, change, or check timers.",
            error=True,
        )

    # Actions -------------------------------------------------------------

    async def start_timer(self, device: str, name: str | None, duration: str | None) -> ToolOutcome:
        seconds = parse_duration(duration)
        if seconds is None or seconds <= 0:
            question = "How long should the timer be?"
            return ToolOutcome(
                result={"needs": "duration"},
                speak=question,
                pending=PendingQuestion(kind="clarify", question=question),
            )
        return await self.start_seconds(device, name, seconds)

    async def start_seconds(self, device: str, name: str | None, seconds: float) -> ToolOutcome:
        if len(self.timers(device)) >= self.settings.max_per_device:
            return ToolOutcome(
                result={"error": "too many timers"},
                speak=f"You already have {self.settings.max_per_device} timers. Cancel one first.",
                error=True,
            )
        base = normalize_name(name) or duration_label(seconds)
        # "another pasta timer" may arrive as "pasta 2"; number it ourselves.
        base = re.sub(r"\s+\d+$", "", base) or base
        taken = {timer.name for timer in self.timers(device)}
        stored, ordinal = base, 1
        while stored in taken:
            ordinal += 1
            stored = f"{base} {ordinal}"
        now = self.clock()
        timer = Timer(
            id=uuid.uuid4().hex,
            device=device,
            name=stored,
            duration_s=seconds,
            ends_at=now + seconds,
            state="running",
            created_at=now,
        )
        self._timers[timer.id] = timer
        self._save(timer)
        self._schedule(timer)
        self._emit(TimerEvent("changed", device))
        logger.info(
            "Timer started",
            extra={"event": "timer_started", "timer": timer.name, "duration_s": seconds},
        )
        spoken_name = f"{ORDINALS.get(ordinal, f'number {ordinal}')} {base}" if ordinal > 1 else base
        speak = f"{_title(spoken_name)} timer set for {speak_duration(seconds)}."
        return ToolOutcome(result={"started": timer.name, "duration": speak_duration(seconds)}, speak=speak)

    async def cancel(self, device: str, name: str | None) -> ToolOutcome:
        timers = self.timers(device)
        if not timers:
            return ToolOutcome(result={"timers": []}, speak="You don't have any timers.")
        if normalize_name(name) in ("all", "every", "all the", "all of the", "everything"):
            for timer in timers:
                self._remove(timer)
            self._emit(TimerEvent("changed", device))
            return ToolOutcome(result={"cancelled": [t.name for t in timers]}, speak="All timers cancelled.")
        matches = self._find(device, name)
        if not matches:
            names = [timer.name for timer in timers]
            return ToolOutcome(
                result={"error": "no such timer", "timers": names},
                speak=f"I couldn't find a {normalize_name(name)} timer. You have {_and(names)}.",
            )
        if len(matches) > 1:
            return self._which(matches, "cancel")
        timer = matches[0]
        self._remove(timer)
        self._emit(TimerEvent("changed", device))
        if timer.state == "ringing":
            self._emit(TimerEvent("ring_stopped", device, names=[timer.name]))
        logger.info("Timer cancelled", extra={"event": "timer_cancelled", "timer": timer.name})
        return ToolOutcome(result={"cancelled": timer.name}, speak=f"{_title(timer.name)} timer cancelled.")

    async def add_time(self, device: str, name: str | None, duration: str | None, sign: int) -> ToolOutcome:
        seconds = parse_duration(duration)
        if seconds is None or seconds == 0:
            question = "How much time?"
            return ToolOutcome(
                result={"needs": "duration"},
                speak=question,
                pending=PendingQuestion(kind="clarify", question=question),
            )
        delta = sign * seconds
        running = [timer for timer in self._find(device, name) if timer.state == "running"]
        if not running:
            if not self.timers(device):
                return ToolOutcome(result={"timers": []}, speak="You don't have any timers.")
            names = [timer.name for timer in self.timers(device)]
            return ToolOutcome(
                result={"error": "no such timer", "timers": names},
                speak=f"I couldn't find that timer. You have {_and(names)}.",
            )
        if len(running) > 1:
            return self._which(running, "change")
        timer = running[0]
        now = self.clock()
        timer.ends_at = max(now + 1, timer.ends_at + delta)
        timer.duration_s = max(1.0, timer.duration_s + delta)
        self._save(timer)
        self._schedule(timer)
        self._emit(TimerEvent("changed", device))
        left = speak_duration(timer.remaining(now))
        verb = f"Added {speak_duration(abs(delta))} to" if delta > 0 else f"Took {speak_duration(abs(delta))} off"
        logger.info("Timer changed", extra={"event": "timer_changed", "timer": timer.name, "delta_s": delta})
        return ToolOutcome(
            result={"timer": timer.name, "remaining": left},
            speak=f"{verb} the {timer.name} timer. {_title(left)} left.",
        )

    def status(self, device: str, name: str | None) -> ToolOutcome:
        timers = self.timers(device)
        now = self.clock()
        if not timers:
            return ToolOutcome(result={"timers": []}, speak="You don't have any timers.")

        def describe(timer: Timer) -> str:
            return f"{timer.name}, ringing" if timer.state == "ringing" else f"{timer.name}, {speak_duration(timer.remaining(now))}"

        result = {"timers": [{"name": t.name, "remaining": speak_duration(t.remaining(now)), "state": t.state} for t in timers]}
        if normalize_name(name) is not None and normalize_name(name) not in ("all", "every"):
            matches = self._find(device, name)
            if not matches:
                return ToolOutcome(
                    result=result,
                    speak=f"I couldn't find a {normalize_name(name)} timer. You have {_and([t.name for t in timers])}.",
                )
            if len(matches) == 1:
                timer = matches[0]
                if timer.state == "ringing":
                    return ToolOutcome(result=result, speak=f"The {timer.name} timer is done.")
                return ToolOutcome(
                    result=result,
                    speak=f"The {timer.name} timer has {speak_duration(timer.remaining(now))} left.",
                )
            timers = matches
        if len(timers) == 1:
            return ToolOutcome(result=result, speak=f"You have one timer: {describe(timers[0])}.")
        count = COUNTS.get(len(timers), str(len(timers)))
        return ToolOutcome(result=result, speak=f"You have {count} timers: {'; '.join(describe(t) for t in timers)}.")

    async def stop_ringing(self, device: str) -> list[str]:
        ringing = self.ringing(device)
        for timer in ringing:
            self._remove(timer)
        if ringing:
            names = [timer.name for timer in ringing]
            self._emit(TimerEvent("ring_stopped", device, names=names))
            self._emit(TimerEvent("changed", device))
            logger.info("Timer ringing stopped", extra={"event": "timer_stopped", "timers": names})
        return [timer.name for timer in ringing]

    async def stop_ringing_outcome(self, device: str) -> ToolOutcome:
        names = await self.stop_ringing(device)
        if not names:
            return ToolOutcome(result={"stopped": []}, speak="No timer is ringing.")
        return ToolOutcome(result={"stopped": names}, speak="Okay.")

    def _which(self, matches: list[Timer], verb: str) -> ToolOutcome:
        names = [timer.name for timer in matches]
        question = f"Which timer: {_join(names)}?"
        return ToolOutcome(
            result={"ambiguous": names, "action": verb},
            speak=question,
            pending=PendingQuestion(kind="clarify", question=question, options=names),
        )

    # Scheduling ------------------------------------------------------------

    def _schedule(self, timer: Timer) -> None:
        handle = self._handles.pop(timer.id, None)
        if handle is not None:
            handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. a synchronous test): nothing rings
        now = self.clock()
        if timer.state == "running":
            delay = max(0.0, timer.ends_at - now)
            self._handles[timer.id] = loop.call_later(delay, self._fire, timer.id)
        else:
            delay = max(0.0, (timer.ring_started_at or now) + self.settings.ring_max_s - now)
            self._handles[timer.id] = loop.call_later(delay, self._ring_timeout, timer.id)

    def _fire(self, timer_id: str) -> None:
        timer = self._timers.get(timer_id)
        if timer is None or timer.state != "running":
            return
        timer.state = "ringing"
        timer.ring_started_at = self.clock()
        self._save(timer)
        self._schedule(timer)
        logger.info("Timer finished", extra={"event": "timer_ringing", "timer": timer.name})
        self._emit(TimerEvent("changed", timer.device))
        self._schedule_announce(timer.device)

    def _schedule_announce(self, device: str) -> None:
        if device in self._announce:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._announce[device] = loop.call_later(ANNOUNCE_GROUP_S, self._announce_ringing, device)

    def _announce_ringing(self, device: str) -> None:
        self._announce.pop(device, None)
        names = [timer.name for timer in self.ringing(device)]
        if not names:
            return
        if len(names) == 1:
            text = f"Your {names[0]} timer is done."
        else:
            text = f"Your {_and(names)} timers are done."
        self._emit(TimerEvent("ring", device, text=text, names=names))

    def _ring_timeout(self, timer_id: str) -> None:
        timer = self._timers.get(timer_id)
        if timer is None:
            return
        self._remove(timer)
        logger.info("Timer stopped ringing after ring_max_s", extra={"event": "timer_ring_timeout", "timer": timer.name})
        self._emit(TimerEvent("ring_stopped", timer.device, names=[timer.name]))
        self._emit(TimerEvent("changed", timer.device))

    def _remove(self, timer: Timer) -> None:
        handle = self._handles.pop(timer.id, None)
        if handle is not None:
            handle.cancel()
        self._timers.pop(timer.id, None)
        self._delete(timer.id)

    def _emit(self, event: TimerEvent) -> None:
        for listener in list(self._listeners):
            task = asyncio.get_running_loop().create_task(listener(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # Persistence -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        assert self.database_path is not None
        return sqlite3.connect(self.database_path)

    def _load(self) -> list[Timer]:
        if self.database_path is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, device, name, duration_s, ends_at, state, created_at, ring_started_at FROM timers"
            ).fetchall()
        return [Timer(*row) for row in rows]

    def _save(self, timer: Timer) -> None:
        if self.database_path is None:
            return
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO timers "
                "(id, device, name, duration_s, ends_at, state, created_at, ring_started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    timer.id,
                    timer.device,
                    timer.name,
                    timer.duration_s,
                    timer.ends_at,
                    timer.state,
                    timer.created_at,
                    timer.ring_started_at,
                ),
            )

    def _delete(self, timer_id: str) -> None:
        if self.database_path is None:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM timers WHERE id = ?", (timer_id,))
