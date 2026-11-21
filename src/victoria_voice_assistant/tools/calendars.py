"""The `calendar` tool: the calendars on the Mac running the assistant, through EventKit.

Every account added to macOS is reached (iCloud, Google, Exchange), except the calendars named
under `calendar.skip` in `tools.yaml`. The tool reads, adds, changes and deletes events. Adding
just happens and the reply says what was added; a change or a delete asks first, and runs on a yes.
(Feature 20261112-184425. A second backend, Google's own API, is planned behind the same protocol.)
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock, timedelta
from typing import Any, Callable, Protocol

from victoria_voice_assistant.config.schema import CalendarConfig
from victoria_voice_assistant.tools.duration import parse_duration
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome
from victoria_voice_assistant.tools.reminders import (
    MATCH_AT_LEAST,
    MONTHS,
    WEEKDAYS,
    Access,
    _day_of,
    _join,
    _plain,
    _time_of,
    best_matches,
    list_score,
    numbered,
    parse_due,
    speak_due,
    spoken_number,
)

logger = logging.getLogger(__name__)

SETUP_STEPS = (
    "Calendar access isn't granted to the assistant. At the Mac running it, answer the Calendar prompt "
    "(or run `uv run assistant calendar grant`), or allow full access in System Settings > Privacy & Security > Calendars."
)
MAX_LISTED = 12
MAX_CHOICES = 5  # how many events a "which one?" question offers
SHOWN_FOR_S = 600.0  # how long "number 2" refers to the last numbered list shown on that device
CONFIRM_FOR_S = 120.0  # how long a change or a delete waits for its yes
SEARCH_AHEAD_DAYS = 365  # how far forward "when is the dentist?" looks
MIN_FREE_MINUTES = 30  # a gap shorter than this isn't said as free time
PART_HOURS = {"morning": (6, 12), "afternoon": (12, 17), "evening": (17, 22), "tonight": (17, 24), "night": (17, 24)}
REPEATS = ("daily", "weekdays", "weekly", "monthly", "yearly")
PARAMS = {"action", "title", "when", "end", "duration", "all_day", "calendar", "location", "notes", "alert", "repeat", "new_title", "new_when", "scope"}


@dataclass
class CalendarInfo:
    name: str  # what it's called here: its title, with the account added when two accounts have one by that title
    writable: bool = True
    account: str = ""
    title: str = ""  # its title in the Calendar app; the name when left empty

    def __post_init__(self) -> None:
        self.title = self.title or self.name


def account_label(account: str) -> str:
    """How an account is said after a calendar's title: "iCloud", "Google", or an address's domain."""
    if "@" not in account:
        return account or "this Mac"
    domain = account.rsplit("@", 1)[1].lower()
    return "Google" if domain in ("gmail.com", "googlemail.com") else domain


def calendar_names(calendars: list[tuple[str, str, str]]) -> dict[str, str]:
    """Each calendar's name, from (id, title, account): the title, or "Family (iCloud)" when two share a title.

    Two calendars that would still share a name (the same title in two accounts that read the same)
    are numbered.
    """
    counts: dict[str, int] = {}
    for _, title, _ in calendars:
        counts[_plain(title)] = counts.get(_plain(title), 0) + 1
    names: dict[str, str] = {}
    taken: dict[str, int] = {}
    for key, title, account in calendars:
        name = title if counts[_plain(title)] == 1 else f"{title} ({account_label(account)})"
        taken[_plain(name)] = taken.get(_plain(name), 0) + 1
        names[key] = name if taken[_plain(name)] == 1 else f"{name} {taken[_plain(name)]}"
    return names


@dataclass
class CalendarEvent:
    id: str
    title: str
    calendar: str
    start: datetime  # local time
    end: datetime  # an all-day event's is in its last day
    all_day: bool = False
    location: str = ""
    notes: str = ""
    url: str = ""
    attendees: list[str] = field(default_factory=list)  # "Sam (accepted)"
    repeats: bool = False
    alert_minutes: int | None = None  # minutes before the start


class CalendarBackend(Protocol):
    """What the tool needs from a calendar account. Blocking calls; the service runs them off the event loop."""

    def access(self) -> Access: ...
    def request_access(self) -> bool: ...
    def refresh(self) -> None: ...
    def calendars(self) -> list[CalendarInfo]: ...
    def default_calendar(self) -> str | None: ...
    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...
    def create(self, draft: CalendarEvent, repeat: str | None) -> CalendarEvent: ...
    def update(self, event: CalendarEvent, changes: dict[str, Any], future: bool) -> CalendarEvent: ...
    def delete(self, event: CalendarEvent, future: bool) -> None: ...
    def create_calendar(self, name: str) -> str: ...


# --- spoken times and ranges --------------------------------------------------------------------


@dataclass
class Range:
    start: datetime
    end: datetime
    label: str  # "today", "Friday afternoon", "this week"

    @property
    def days(self) -> int:
        return max(1, (self.end - timedelta(seconds=1)).toordinal() - self.start.toordinal() + 1)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, clock(0, 0))


def speak_time(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    minutes = f":{moment.minute:02d}" if moment.minute else ""
    return f"{hour}{minutes} {'AM' if moment.hour < 12 else 'PM'}"


def speak_span(event: CalendarEvent, now: datetime) -> str:
    """ "Friday at 2 PM to 3 PM", "tomorrow, all day", "Friday, all day through Sunday"."""
    if event.all_day:
        through = f" through {speak_due(event.end, False, now)}" if event.end.date() > event.start.date() else ""
        return f"{speak_due(event.start, False, now)}, all day{through}"
    return f"{speak_due(event.start, True, now)} to {speak_time(event.end)}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _on(label: str) -> str:
    """ "on Friday", "on November 20"; "today" and "this week" stay as they are."""
    first = label.split()[0].lower() if label else ""
    return f"on {label}" if first in WEEKDAYS or first in MONTHS else label


def parse_range(text: str | None, now: datetime) -> Range | None:
    """The stretch of time a spoken phrase means: "today", "Friday afternoon", "this week", "November 20".

    Nothing said means today. None when the phrase can't be read.
    """
    lowered = " ".join((text or "today").lower().replace(",", " ").split())
    today = now.date()
    if re.search(r"\bthis week\b", lowered):
        return Range(_midnight(today), _midnight(today + timedelta(days=7 - today.weekday())), "this week")
    if re.search(r"\bnext week\b", lowered):
        monday = today + timedelta(days=7 - today.weekday())
        return Range(_midnight(monday), _midnight(monday + timedelta(days=7)), "next week")
    if re.search(r"\bweekend\b", lowered):
        saturday = today - timedelta(days=1) if today.weekday() == 6 else today + timedelta(days=(5 - today.weekday()) % 7)
        return Range(_midnight(max(saturday, today)), _midnight(saturday + timedelta(days=2)), "this weekend")
    if re.search(r"\b(this|next) month\b", lowered):
        first_next = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        if "next month" in lowered:
            after = date(first_next.year + (first_next.month == 12), first_next.month % 12 + 1, 1)
            return Range(_midnight(first_next), _midnight(after), "next month")
        return Range(_midnight(today), _midnight(first_next), "this month")
    several = re.search(r"\bnext (\d+|few|couple of) days\b", lowered)
    if several:
        count = int(several.group(1)) if several.group(1).isdigit() else 3
        return Range(_midnight(today), _midnight(today + timedelta(days=max(1, min(count, 31)))), f"in the next {count} days")
    part = next((name for name in PART_HOURS if re.search(rf"\b{name}\b", lowered)), None)
    day = _day_of(lowered, today)
    if day is None:
        if part is None and lowered != "now":
            return None
        day = today
    label = speak_due(_midnight(day), False, now)
    if part is None:
        return Range(_midnight(day), _midnight(day + timedelta(days=1)), label)
    first, last = PART_HOURS[part]
    if day == today:
        label = "tonight" if part in ("tonight", "night") else f"this {part}"
    else:
        label = f"{label} {'night' if part == 'tonight' else part}"
    return Range(_midnight(day) + timedelta(hours=first), _midnight(day) + timedelta(hours=last), label)


def parse_alert(text: Any) -> int | None:
    """ "15 minutes before", "an hour" as minutes before the start."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text) if text >= 0 else None
    if not isinstance(text, str) or not text.strip():
        return None
    seconds = parse_duration(re.sub(r"\b(before|ahead|early|earlier)\b", "", text.lower()).strip())
    return round(seconds / 60) if seconds is not None and seconds >= 0 else None


def _moved(event: CalendarEvent, text: str, now: datetime) -> tuple[datetime, bool] | None:
    """Where "move it to 3" or "to Friday" puts an event: a new start, and whether it's (still) all day.

    Only a time said keeps the day; only a day said keeps the time.
    """
    lowered = " ".join(text.lower().replace(",", " ").split())
    if re.fullmatch(r"(to )?\d{1,2}(:\d{2})?", lowered):
        lowered = "at " + lowered.removeprefix("to ")
    if lowered.startswith("in "):
        return parse_due(lowered, now)
    day = _day_of(lowered, now.date())
    at = _time_of(lowered)
    if day is None and at is None:
        return None
    if at is None:
        return datetime.combine(day or event.start.date(), event.start.time()), event.all_day
    return datetime.combine(day or event.start.date(), at), False


def _calendars_said(names: list[str]) -> str:
    """ "the Work calendar", "the Family (iCloud) and Family (Google) calendars"."""
    return f"the {names[0]} calendar" if len(names) == 1 else f"the {_join(names)} calendars"


def _looks_like_title(value: Any) -> bool:
    """A made-up argument's value that can stand for the event's name: plain words, not a length or a time.

    The model also breaks its own arguments ("finish_time": "2 hours',title:"), so quotes, colons
    and braces rule a value out (2026-11-20).
    """
    if not isinstance(value, str) or not value.strip() or re.search(r"['\"{}:\[\]]", value):
        return False
    lowered = value.lower()
    return parse_duration(lowered) is None and _time_of(lowered) is None


def free_gaps(events: list[CalendarEvent], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """The stretches between ``start`` and ``end`` with no timed event, at least MIN_FREE_MINUTES long."""
    busy = sorted((max(e.start, start), min(e.end, end)) for e in events if not e.all_day and e.end > start and e.start < end)
    gaps: list[tuple[datetime, datetime]] = []
    at = start
    for begins, ends in busy:
        if begins - at >= timedelta(minutes=MIN_FREE_MINUTES):
            gaps.append((at, begins))
        at = max(at, ends)
    if end - at >= timedelta(minutes=MIN_FREE_MINUTES):
        gaps.append((at, end))
    return gaps


# --- the service --------------------------------------------------------------------------------


class CalendarService:
    """The `calendar` tool's actions."""

    def __init__(self, settings: CalendarConfig | None = None, backend: CalendarBackend | None = None) -> None:
        self.settings = settings or CalendarConfig()
        self.tool_name = "calendar"
        self._backend = backend
        self._asking = False
        self._shown: dict[str, tuple[list[CalendarEvent], float]] = {}  # device -> the last numbered events it was shown
        self._waiting: dict[str, tuple[dict[str, Any], float]] = {}  # device -> the change or delete waiting for a yes
        self.on_granted: Callable[[], None] | None = None  # set by the app: rebuild the tools once access arrives

    @property
    def backend(self) -> CalendarBackend:
        if self._backend is None:
            self._backend = EventKitBackend()
        return self._backend

    def update(self, settings: CalendarConfig | None, tool_name: str) -> None:
        self.settings = settings or CalendarConfig()
        self.tool_name = tool_name

    def access(self) -> Access:
        return self.backend.access()

    def ask_for_access(self) -> None:
        """Ask macOS once, without waiting: the question is answered at the Mac."""
        if self._asking or self.access() != "not_determined":
            return
        self._asking = True

        def ask() -> None:
            try:
                granted = self.backend.request_access()
            except Exception:  # noqa: BLE001 - logged; the tool stays a stub
                logger.exception("Calendar access request failed", extra={"event": "calendar_access_failed"})
                granted = False
            finally:
                self._asking = False
            logger.info("Calendar access answered", extra={"event": "calendar_access", "status": "granted" if granted else "refused"})
            if granted and self.on_granted is not None:
                self.on_granted()

        logger.info("Asking for Calendar access", extra={"event": "calendar_access_asked"})
        threading.Thread(target=ask, name="calendar-access", daemon=True).start()

    # --- reading the account ---

    def _reached(self) -> list[CalendarInfo]:
        """The calendars the tool may use. A title under `skip` leaves out every calendar with it; a name, just that one."""
        skip = {_plain(name) for name in self.settings.skip}
        reached = (info for info in self.backend.calendars() if _plain(info.name) not in skip and _plain(info.title) not in skip)
        return sorted(reached, key=lambda info: info.name.lower())

    def _default(self, infos: list[CalendarInfo]) -> str | None:
        """Where new events go. A calendar named in config that isn't there is never replaced by another."""
        if self.settings.default_calendar:
            wanted = _plain(self.settings.default_calendar)
            named = [info.name for info in infos if _plain(info.name) == wanted] or [info.name for info in infos if _plain(info.title) == wanted]
            return named[0] if len(named) == 1 else self.settings.default_calendar
        wanted_name = self.backend.default_calendar()
        named_default = next((info.name for info in infos if wanted_name and _plain(info.name) == _plain(wanted_name)), None)
        return named_default or next((info.name for info in infos if info.writable), None)

    def _events(self, start: datetime, end: datetime, infos: list[CalendarInfo], only: list[str] | None = None) -> list[CalendarEvent]:
        names = {_plain(name) for name in only} if only else {_plain(info.name) for info in infos}
        found = [event for event in self.backend.events(start, end) if _plain(event.calendar) in names]
        return sorted(found, key=lambda event: (event.start.date(), not event.all_day, event.start, event.title.lower()))

    def _resolve_calendar(self, spoken: str, infos: list[CalendarInfo]) -> list[str] | ToolOutcome:
        """The calendars a spoken name means: one, or every calendar with that title ("family" for both Family calendars)."""
        said = re.sub(r"\b(the|my|our|calendars?)\b", "", spoken, flags=re.IGNORECASE)
        said = " ".join(said.split()) or spoken
        exact = [info.name for info in infos if _plain(info.name) == _plain(said)]
        if exact:
            return exact
        titled = [info.name for info in infos if _plain(info.title) == _plain(said)]
        if titled:
            return titled
        scored = sorted(((max(list_score(said, info.name), list_score(said, info.title)), info.name) for info in infos), reverse=True)
        if scored and scored[0][0] >= MATCH_AT_LEAST:
            best = scored[0][0]
            return sorted((name for score, name in scored if score >= best - 0.01), key=str.lower)
        return ToolOutcome(result={"error": f"no calendar called {spoken!r}", "calendars": [info.name for info in infos]}, speak=f"There's no {said} calendar.")

    def _remember(self, device: str, events: list[CalendarEvent]) -> None:
        self._shown[device] = (events, time.monotonic())

    def _line(self, event: CalendarEvent, now: datetime, with_day: bool) -> str:
        at = "all day" if event.all_day else speak_time(event.start)
        if with_day:
            return f"{_cap(speak_due(event.start, False, now))}, {at}: {event.title}"
        return f"{_cap(at)}: {event.title}"

    def _fields(self, event: CalendarEvent, now: datetime) -> dict[str, Any]:
        found: dict[str, Any] = {"title": event.title, "when": speak_span(event, now), "calendar": event.calendar}
        for key in ("location", "notes", "url"):
            if getattr(event, key):
                found[key] = getattr(event, key)
        if event.attendees:
            found["invited"] = event.attendees
        if event.repeats:
            found["repeats"] = True
        if event.alert_minutes is not None:
            found["alert_minutes_before"] = event.alert_minutes
        return found

    # --- the actions ---

    async def handle(self, arguments: dict[str, Any], now: datetime | None = None, device: str = "web") -> ToolOutcome:
        now = now or datetime.now()
        action = str(arguments.get("action") or "")
        if await asyncio.to_thread(self.access) != "granted":
            return ToolOutcome(result={"error": "no access"}, speak="I can't reach the calendar yet.", error=True)
        try:
            return await asyncio.to_thread(self._run, action, arguments, now, device)
        except TimeoutError:
            logger.warning("Calendar didn't answer", extra={"event": "calendar_timeout", "kind": action})
            return ToolOutcome(result={"error": "the calendar didn't answer"}, speak="The calendar didn't answer.", error=True)
        except (LookupError, RuntimeError) as exc:  # an event deleted on the phone, a save refused
            logger.warning("Calendar refused", extra={"event": "calendar_failed", "kind": action, "reason": str(exc)})
            return ToolOutcome(result={"error": str(exc)}, speak="The calendar didn't take that.", error=True)

    def _run(self, action: str, arguments: dict[str, Any], now: datetime, device: str) -> ToolOutcome:
        if action != "confirm":
            self._waiting.pop(device, None)  # a new request drops a change or delete that was waiting for a yes
        self.backend.refresh()  # what changed on the phone or in the Calendar app since the last request
        infos = self._reached()
        only: list[str] | None = None
        spoken = arguments.get("calendar")
        if isinstance(spoken, str) and spoken.strip():
            resolved = self._resolve_calendar(spoken.strip(), infos)
            if isinstance(resolved, ToolOutcome):
                return resolved
            only = resolved
        if action in ("find", "details", "change", "delete") and not str(arguments.get("title") or "").strip():
            # The model sometimes puts the event's name where the day goes ("when": "Zebra checkpoint").
            # or under a name of its own making ("time_on_which_event_is_scheduled", 2026-11-12).
            said = arguments.get("when")
            stray = next((value.strip() for key, value in arguments.items() if key not in PARAMS and _looks_like_title(value)), None)
            if isinstance(said, str) and said.strip() and parse_range(said, now) is None:
                arguments = {**arguments, "title": said, "when": None}
            elif stray is not None:
                arguments = {**arguments, "title": stray}
        if action == "calendars":
            names = [info.name for info in infos]
            header = f"You have {len(names)} calendar{'s' if len(names) != 1 else ''}:"
            return ToolOutcome(result={"calendars": names, "default": self._default(infos)}, speak=numbered(header, names))
        if action == "list":
            return self._list(arguments, infos, only, now, device)
        if action == "next":
            return self._next(infos, only, now, device)
        if action == "find":
            return self._find(arguments, infos, only, now, device)
        if action == "details":
            found = self._one(arguments, infos, only, now, device)
            return found if isinstance(found, ToolOutcome) else self._details(found, now)
        if action == "free":
            return self._free(arguments, infos, now)
        if action == "add":
            return self._add(arguments, infos, only, now)
        if action == "change":
            return self._change(arguments, infos, only, now, device)
        if action == "delete":
            return self._delete(arguments, infos, only, now, device)
        if action == "confirm":
            return self._confirm(now, device)
        actions = ["list", "next", "find", "details", "free", "calendars", "add", "change", "delete"]
        return ToolOutcome(result={"error": f"unknown action {action!r}", "actions": actions}, error=True)

    def _list(self, arguments: dict[str, Any], infos: list[CalendarInfo], only: list[str] | None, now: datetime, device: str) -> ToolOutcome:
        said = arguments.get("when") if isinstance(arguments.get("when"), str) else None
        span = parse_range(said, now)
        if span is None:
            return ToolOutcome(result={"error": f"couldn't read the day {said!r}"}, speak=f"Which day? I didn't catch “{said}”.")
        events = self._events(span.start, span.end, infos, only)
        where = f" on {_calendars_said(only)}" if only else ""
        if not events:
            return ToolOutcome(result={"events": [], "when": span.label}, speak=f"Nothing on your calendar {_on(span.label)}{where}.")
        shown = events[:MAX_LISTED]
        self._remember(device, shown)
        count = len(events)
        header = f"{count} event{'s' if count != 1 else ''} {_on(span.label)}{where}:"
        lines = [self._line(event, now, with_day=span.days > 1) for event in shown]
        more = [f"and {count - MAX_LISTED} more"] if count > MAX_LISTED else []
        result: dict[str, Any] = {"events": [self._fields(event, now) for event in shown], "when": span.label}
        if more:
            result["more"] = count - MAX_LISTED
        return ToolOutcome(result=result, speak=numbered(header, lines, more))

    def _next(self, infos: list[CalendarInfo], only: list[str] | None, now: datetime, device: str) -> ToolOutcome:
        events = self._events(now, now + timedelta(days=SEARCH_AHEAD_DAYS), infos, only)
        ahead = [event for event in events if (event.start.date() > now.date() if event.all_day else event.start > now)]
        if not ahead:
            return ToolOutcome(result={"events": []}, speak="Nothing coming up on your calendar.")
        event = min(ahead, key=lambda e: e.start)
        self._remember(device, [event])
        return ToolOutcome(result={"next": self._fields(event, now)}, speak=f"Next is {event.title}, {speak_span(event, now)}.")

    def _matches(self, arguments: dict[str, Any], infos: list[CalendarInfo], only: list[str] | None, now: datetime) -> list[CalendarEvent] | ToolOutcome:
        """The coming events a spoken title means: each repeating event once, at its next time."""
        title = str(arguments.get("title") or "").strip()
        said = arguments.get("when") if isinstance(arguments.get("when"), str) and arguments["when"].strip() else None
        if said:
            span = parse_range(said, now)
            if span is None:
                return ToolOutcome(result={"error": f"couldn't read the day {said!r}"}, speak=f"Which day? I didn't catch “{said}”.")
            start, end = span.start, span.end
        else:
            start, end = _midnight(now.date()), now + timedelta(days=SEARCH_AHEAD_DAYS)
        events = [event for event in self._events(start, end, infos, only) if event.end > now or event.all_day]
        seen: set[str] = set()
        once = [event for event in events if not (event.id in seen or seen.add(event.id))]  # type: ignore[func-returns-value]
        found = best_matches(once, title) if title else once  # type: ignore[arg-type]
        if not found and said and title:
            # The model adds a day the user didn't say ("when": "today", 2026-11-12): look past it.
            return self._matches({**arguments, "when": None}, infos, only, now)
        return found  # type: ignore[return-value]

    def _find(self, arguments: dict[str, Any], infos: list[CalendarInfo], only: list[str] | None, now: datetime, device: str) -> ToolOutcome:
        title = str(arguments.get("title") or "").strip()
        if not title:
            return ToolOutcome(result={"error": "no title"}, speak="Which event?")
        matches = self._matches(arguments, infos, only, now)
        if isinstance(matches, ToolOutcome):
            return matches
        if not matches:
            return ToolOutcome(result={"events": []}, speak=f"I couldn't find “{title}” on your calendar.")
        shown = matches[:MAX_CHOICES]
        self._remember(device, shown)
        result = {"events": [self._fields(event, now) for event in shown]}
        if len(matches) == 1:
            # "Where is the dentist?" and "tell me about the dentist" come here too, so the place is said.
            place = f", at {matches[0].location}" if matches[0].location else ""
            return ToolOutcome(result=result, speak=f"{matches[0].title} is {speak_span(matches[0], now)}{place}.")
        lines = [f"{event.title}, {speak_span(event, now)}" for event in shown]
        return ToolOutcome(result=result, speak=numbered(f"{len(matches)} events match “{title}”:", lines))

    def _one(self, arguments: dict[str, Any], infos: list[CalendarInfo], only: list[str] | None, now: datetime, device: str) -> CalendarEvent | ToolOutcome:
        """The one event a title, or a number from the last listing, means; else what to say."""
        title = str(arguments.get("title") or "").strip()
        if not title:
            return ToolOutcome(result={"error": "no title"}, speak="Which event?")
        number = spoken_number(title)
        if number is not None:
            events, at = self._shown.get(device, ([], 0.0))
            if time.monotonic() - at > SHOWN_FOR_S or not 1 <= number <= len(events):
                return ToolOutcome(result={"error": f"no event number {number}"}, speak=f"I don't have an event number {number}. Ask what's on the calendar first.")
            return events[number - 1]
        matches = self._matches(arguments, infos, only, now)
        if isinstance(matches, ToolOutcome):
            return matches
        if not matches:
            return ToolOutcome(result={"error": f"nothing matches {title!r}"}, speak=f"I couldn't find “{title}” on your calendar.")
        if len(matches) == 1:
            return matches[0]
        shown = matches[:MAX_CHOICES]
        self._remember(device, shown)
        options = [f"{event.title}, {speak_span(event, now)}" for event in shown]
        question = numbered("Which one?", options)
        return ToolOutcome(result={"question": "Which one?", "options": options}, speak=question, pending=PendingQuestion(kind="clarify", question=question, options=options))

    def _details(self, event: CalendarEvent, now: datetime) -> ToolOutcome:
        parts = [f"{event.title}: {speak_span(event, now)}"]
        if event.location:
            parts.append(f"at {event.location}")
        parts.append(f"on the {event.calendar} calendar")
        text = ", ".join(parts) + "."
        if event.repeats:
            text += " It repeats."
        if event.attendees:
            text += f" Invited: {_join(event.attendees)}."
        if event.notes:
            text += f" Notes: {event.notes.strip()}"
        return ToolOutcome(result=self._fields(event, now), speak=text, chat=event.url or None)

    def _free(self, arguments: dict[str, Any], infos: list[CalendarInfo], now: datetime) -> ToolOutcome:
        said = arguments.get("when") if isinstance(arguments.get("when"), str) else None
        span = parse_range(said, now)
        if span is None:
            return ToolOutcome(result={"error": f"couldn't read the day {said!r}"}, speak=f"Which day? I didn't catch “{said}”.")
        events = self._events(span.start, span.end, infos)
        lines: list[str] = []
        free: list[dict[str, str]] = []
        day = span.start.date()
        while _midnight(day) < span.end and len(lines) < 7:
            first = max(span.start, _midnight(day) + timedelta(hours=self.settings.free_from), now.replace(second=0, microsecond=0))
            last = min(span.end, _midnight(day) + timedelta(hours=self.settings.free_until))
            name = speak_due(_midnight(day), False, now)
            gaps = free_gaps(events, first, last) if last > first else []
            whole = len(gaps) == 1 and gaps[0] == (first, last)
            said_gaps = _join([f"from {speak_time(a)} to {speak_time(b)}" for a, b in gaps])
            lines.append(f"{_cap(name)}: " + ("free" if whole else said_gaps or "nothing free"))
            free.append({"day": name, "free": "all of it" if whole else said_gaps or "none"})
            day += timedelta(days=1)
        all_day = [event.title for event in events if event.all_day]
        note = f" All day: {_join(sorted(set(all_day)))}." if all_day else ""
        result = {"when": span.label, "free": free, "all_day": all_day}
        if len(lines) == 1:
            answer = free[0]["free"]
            if answer == "all of it":
                return ToolOutcome(result=result, speak=f"You're free {_on(span.label)}.{note}")
            if answer == "none":
                return ToolOutcome(result=result, speak=f"You have nothing free {_on(span.label)}.{note}")
            return ToolOutcome(result=result, speak=f"{_cap(_on(span.label))} you're free {answer}.{note}")
        return ToolOutcome(result=result, speak="\n".join([f"Free time {_on(span.label)}:", *lines]) + (f"\n{note.strip()}" if note else ""))

    def _writable(self, name: str, infos: list[CalendarInfo]) -> ToolOutcome | None:
        info = next((info for info in infos if _plain(info.name) == _plain(name)), None)
        if info is None:
            return ToolOutcome(result={"error": f"no calendar called {name!r} that the tool reaches"}, speak=f"There's no {name} calendar.", error=True)
        if not info.writable:
            return ToolOutcome(result={"error": f"{name!r} is read-only"}, speak=f"The {name} calendar is read-only.", error=True)
        return None

    def _ending(self, arguments: dict[str, Any], start: datetime, all_day: bool, now: datetime, default: timedelta) -> datetime | ToolOutcome:
        """When an event that starts at ``start`` ends: from `end` ("at 3", "Sunday"), `duration`, or ``default``."""
        end_text = arguments.get("end") if isinstance(arguments.get("end"), str) and arguments["end"].strip() else None
        length_text = arguments.get("duration") if isinstance(arguments.get("duration"), str) and arguments["duration"].strip() else None
        if end_text:
            lowered = " ".join(end_text.lower().split())
            if re.fullmatch(r"\d{1,2}(:\d{2})?", lowered):
                lowered = "at " + lowered
            day, at = _day_of(lowered, start.date() - timedelta(days=1)), _time_of(lowered)
            if day is None and at is None:
                return ToolOutcome(result={"error": f"couldn't read the end {end_text!r}"}, speak=f"When does it end? I didn't catch “{end_text}”.")
            if all_day:
                return datetime.combine(day or start.date(), clock(23, 59))
            end = datetime.combine(day or start.date(), at or start.time())
            if at is not None and end <= start and day is None:
                end += timedelta(hours=12) if end + timedelta(hours=12) > start and at.hour < 12 else timedelta(days=1)
            return end if end > start else start + default
        if length_text:
            seconds = parse_duration(length_text)
            if seconds is None or seconds <= 0:
                return ToolOutcome(result={"error": f"couldn't read the length {length_text!r}"}, speak=f"How long is it? I didn't catch “{length_text}”.")
            if all_day:
                return datetime.combine(start.date() + timedelta(days=max(0, round(seconds / 86400) - 1)), clock(23, 59))
            return start + timedelta(seconds=seconds)
        return datetime.combine(start.date(), clock(23, 59)) if all_day else start + default

    def _add(self, arguments: dict[str, Any], infos: list[CalendarInfo], only: list[str] | None, now: datetime) -> ToolOutcome:
        title = str(arguments.get("title") or "").strip()
        if not title:
            return ToolOutcome(result={"error": "no title"}, speak="What's the event called?")
        said = arguments.get("when") if isinstance(arguments.get("when"), str) else None
        parsed = parse_due(said, now)
        if parsed is None:
            return ToolOutcome(result={"error": "no start time" if not said else f"couldn't read the time {said!r}"}, speak="When is it?" if not said else f"When is it? I didn't catch “{said}”.")
        start, has_time = parsed
        all_day = bool(arguments.get("all_day")) or not has_time
        if all_day:
            start = _midnight(start.date())
        end = self._ending(arguments, start, all_day, now, timedelta(minutes=self.settings.default_minutes))
        if isinstance(end, ToolOutcome):
            return end
        if only and len(only) > 1:
            question = f"Which calendar: {_join(only, 'or')}?"
            return ToolOutcome(result={"question": question, "options": only}, speak=question, pending=PendingQuestion(kind="clarify", question=question, options=only))
        target = only[0] if only else self._default(infos)
        if target is None:
            return ToolOutcome(result={"error": "no calendar to add to"}, speak="There's no calendar I can add to.", error=True)
        refused = self._writable(target, infos)
        if refused is not None:
            return refused
        repeat = str(arguments.get("repeat") or "").strip().lower() or None
        if repeat is not None and repeat not in REPEATS:
            return ToolOutcome(result={"error": f"unknown repeat {repeat!r}", "repeats": list(REPEATS)}, error=True)
        draft = CalendarEvent(
            id="",
            title=title,
            calendar=target,
            start=start,
            end=end,
            all_day=all_day,
            location=str(arguments.get("location") or "").strip(),
            notes=str(arguments.get("notes") or "").strip(),
            alert_minutes=parse_alert(arguments.get("alert")),
        )
        made = self.backend.create(draft, repeat)
        logger.info("Calendar event added", extra={"event": "calendar_event_added", "status": "repeating" if repeat else "single"})
        extras = (f", repeating {repeat}" if repeat else "") + (f", with an alert {made.alert_minutes} minutes before" if made.alert_minutes else "")
        return ToolOutcome(result={"added": self._fields(made, now)}, speak=f"I've added “{made.title}” to {made.calendar}, {speak_span(made, now)}{extras}.")

    def _scope(self, event: CalendarEvent, arguments: dict[str, Any], verb: str) -> bool | ToolOutcome:
        """For a repeating event: whether the change is for this one and all after it. Asks when it wasn't said."""
        if not event.repeats:
            return False
        said = str(arguments.get("scope") or "").strip().lower()
        if said in ("this", "future"):
            return said == "future"
        question = f"{event.title} repeats. {verb} just this one, or all of them from now on?"
        options = ["just this one", "all of them from now on"]
        return ToolOutcome(result={"question": question, "options": options}, speak=question, pending=PendingQuestion(kind="clarify", question=question, options=options))

    def _ask(self, device: str, question: str, waiting: dict[str, Any]) -> ToolOutcome:
        self._waiting[device] = (waiting, time.monotonic())
        return ToolOutcome(
            result={"question": question},
            speak=question,
            pending=PendingQuestion(kind="yes_no", question=question, on_yes=(self.tool_name, {"action": "confirm"})),
        )

    def _change(self, arguments: dict[str, Any], infos: list[CalendarInfo], only: list[str] | None, now: datetime, device: str) -> ToolOutcome:
        event = self._one(arguments, infos, only, now, device)
        if isinstance(event, ToolOutcome):
            return event
        refused = self._writable(event.calendar, infos)
        if refused is not None:
            return refused
        changes: dict[str, Any] = {}
        said: list[str] = []
        new_title = str(arguments.get("new_title") or "").strip()
        if new_title and new_title != event.title:
            changes["title"] = new_title
            said.append(f"rename it “{new_title}”")
        start, all_day = event.start, event.all_day
        new_when = arguments.get("new_when") if isinstance(arguments.get("new_when"), str) and arguments["new_when"].strip() else None
        if new_when:
            moved = _moved(event, new_when, now)
            if moved is None:
                return ToolOutcome(result={"error": f"couldn't read the time {new_when!r}"}, speak=f"Move it to when? I didn't catch “{new_when}”.")
            start, all_day = moved
        length = timedelta(minutes=self.settings.default_minutes) if event.all_day and not all_day else event.end - event.start
        resized = any(isinstance(arguments.get(key), str) and arguments[key].strip() for key in ("end", "duration"))
        if new_when or resized:
            end = self._ending(arguments, start, all_day, now, length) if resized else start + length
            if isinstance(end, ToolOutcome):
                return end
            changes |= {"start": start, "end": end, "all_day": all_day}
            after = CalendarEvent(id="", title="", calendar="", start=start, end=end, all_day=all_day)
            said.append(f"move it to {speak_span(after, now)}" if new_when else f"make it end at {speak_time(end)}")
        for key, phrase in (("location", "set the place to {}"), ("notes", "set the notes to “{}”")):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip() and value.strip() != getattr(event, key):
                changes[key] = value.strip()
                said.append(phrase.format(value.strip()))
        alert = parse_alert(arguments.get("alert"))
        if alert is not None and alert != event.alert_minutes:
            changes["alert_minutes"] = alert
            said.append(f"alert {alert} minutes before")
        if not changes:
            return ToolOutcome(result={"error": "nothing to change", "event": self._fields(event, now)}, speak=f"What should I change about {event.title}?")
        future = self._scope(event, arguments, "Change")
        if isinstance(future, ToolOutcome):
            return future
        tail = ", for this one and all after it" if future else ""
        question = f"{event.title}, {speak_span(event, now)}: {_join(said)}{tail}?"
        return self._ask(device, question, {"kind": "change", "event": event, "changes": changes, "future": future})

    def _delete(self, arguments: dict[str, Any], infos: list[CalendarInfo], only: list[str] | None, now: datetime, device: str) -> ToolOutcome:
        event = self._one(arguments, infos, only, now, device)
        if isinstance(event, ToolOutcome):
            return event
        refused = self._writable(event.calendar, infos)
        if refused is not None:
            return refused
        future = self._scope(event, arguments, "Delete")
        if isinstance(future, ToolOutcome):
            return future
        tail = ", and all after it" if future else ""
        return self._ask(device, f"Delete {event.title}, {speak_span(event, now)}{tail}?", {"kind": "delete", "event": event, "future": future})

    def _confirm(self, now: datetime, device: str) -> ToolOutcome:
        waiting, at = self._waiting.pop(device, ({}, 0.0))
        if not waiting or time.monotonic() - at > CONFIRM_FOR_S:
            return ToolOutcome(result={"error": "nothing is waiting for a yes"}, speak="There's nothing waiting to confirm.")
        event: CalendarEvent = waiting["event"]
        if waiting["kind"] == "delete":
            self.backend.delete(event, waiting["future"])
            logger.info("Calendar event deleted", extra={"event": "calendar_event_deleted"})
            return ToolOutcome(result={"deleted": event.title}, speak=f"Deleted {event.title}.")
        changed = self.backend.update(event, waiting["changes"], waiting["future"])
        logger.info("Calendar event changed", extra={"event": "calendar_event_changed", "kind": ",".join(sorted(waiting["changes"]))})
        return ToolOutcome(result={"changed": self._fields(changed, now)}, speak=f"Done. {changed.title} is {speak_span(changed, now)}.")


# --- EventKit -----------------------------------------------------------------------------------


class EventKitBackend:
    """The Mac's calendars through EventKit (PyObjC), in this process. macOS asks once for access."""

    def __init__(self) -> None:
        import EventKit  # noqa: PLC0415 - macOS only, loaded when first used

        self.ek = EventKit
        self.store = EventKit.EKEventStore.alloc().init()

    def access(self) -> Access:
        status = self.ek.EKEventStore.authorizationStatusForEntityType_(self.ek.EKEntityTypeEvent)
        return {
            self.ek.EKAuthorizationStatusNotDetermined: "not_determined",
            self.ek.EKAuthorizationStatusRestricted: "restricted",
            self.ek.EKAuthorizationStatusDenied: "denied",
            self.ek.EKAuthorizationStatusFullAccess: "granted",
            self.ek.EKAuthorizationStatusWriteOnly: "write_only",
        }.get(status, "denied")

    def request_access(self) -> bool:
        done = threading.Event()
        answer: list[bool] = []

        def finished(granted: bool, _error: Any) -> None:
            answer.append(bool(granted))
            done.set()

        self.store.requestFullAccessToEventsWithCompletion_(finished)
        done.wait()  # until someone answers at the Mac
        if answer and answer[0]:
            self.store.reset()  # see the calendars that were hidden before access
        return bool(answer and answer[0])

    def refresh(self) -> None:
        # This process runs no Cocoa run loop, so the store never hears that the calendar database changed:
        # a calendar renamed in the Calendar app stayed "Untitled" here until a restart (2026-11-12).
        self.store.reset()

    def _all(self) -> list[Any]:
        return list(self.store.calendarsForEntityType_(self.ek.EKEntityTypeEvent))

    @staticmethod
    def _account(calendar: Any) -> str:
        return str(calendar.source().title()) if calendar.source() is not None else ""

    def _names(self) -> dict[str, str]:
        """Each calendar's name by its identifier (`calendar_names`): two Family calendars get their accounts."""
        return calendar_names([(str(c.calendarIdentifier()), str(c.title()), self._account(c)) for c in self._all()])

    def _name(self, calendar: Any) -> str:
        return self._names().get(str(calendar.calendarIdentifier()), str(calendar.title()))

    def calendars(self) -> list[CalendarInfo]:
        names = self._names()
        return [
            CalendarInfo(
                name=names[str(c.calendarIdentifier())],
                writable=bool(c.allowsContentModifications()),
                account=self._account(c),
                title=str(c.title()),
            )
            for c in self._all()
        ]

    def default_calendar(self) -> str | None:
        calendar = self.store.defaultCalendarForNewEvents()
        return self._name(calendar) if calendar is not None else None

    @staticmethod
    def _date(moment: datetime) -> Any:
        from Foundation import NSDate  # noqa: PLC0415

        return NSDate.dateWithTimeIntervalSince1970_(moment.timestamp())

    def _event(self, event: Any, names: dict[str, str] | None = None) -> CalendarEvent:
        people: list[str] = []
        states = {
            self.ek.EKParticipantStatusAccepted: "accepted",
            self.ek.EKParticipantStatusDeclined: "declined",
            self.ek.EKParticipantStatusTentative: "maybe",
        }
        for person in event.attendees() or []:
            name = str(person.name() or "").strip()
            if name:
                state = states.get(person.participantStatus())
                people.append(f"{name} ({state})" if state else name)
        alarms = event.alarms() or []
        alert = round(-alarms[0].relativeOffset() / 60) if alarms and alarms[0].absoluteDate() is None else None
        return CalendarEvent(
            id=str(event.eventIdentifier()),
            title=str(event.title() or ""),
            calendar=names.get(str(event.calendar().calendarIdentifier()), str(event.calendar().title())) if names else self._name(event.calendar()),
            start=datetime.fromtimestamp(event.startDate().timeIntervalSince1970()),
            end=datetime.fromtimestamp(event.endDate().timeIntervalSince1970()),
            all_day=bool(event.isAllDay()),
            location=str(event.location() or ""),
            notes=str(event.notes() or ""),
            url=str(event.URL().absoluteString()) if event.URL() is not None else "",
            attendees=people,
            repeats=bool(event.hasRecurrenceRules()),
            alert_minutes=alert if alert is not None and alert >= 0 else None,
        )

    def _raw(self, start: datetime, end: datetime) -> list[Any]:
        predicate = self.store.predicateForEventsWithStartDate_endDate_calendars_(self._date(start), self._date(end), None)
        return list(self.store.eventsMatchingPredicate_(predicate) or [])

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        names = self._names()  # once for the whole fetch, not per event
        return [self._event(event, names) for event in self._raw(start, end)]

    def _occurrence(self, event: CalendarEvent) -> Any:
        """The stored event, at this occurrence when it repeats."""
        for raw in self._raw(event.start - timedelta(minutes=1), event.start + timedelta(days=1)):
            if str(raw.eventIdentifier()) == event.id and abs(raw.startDate().timeIntervalSince1970() - event.start.timestamp()) < 1:
                return raw
        raw = self.store.eventWithIdentifier_(event.id)
        if raw is None:
            raise LookupError("that event is gone")
        return raw

    def _repeat_rule(self, repeat: str) -> Any:
        ek = self.ek
        frequency = {
            "daily": ek.EKRecurrenceFrequencyDaily,
            "weekdays": ek.EKRecurrenceFrequencyWeekly,
            "weekly": ek.EKRecurrenceFrequencyWeekly,
            "monthly": ek.EKRecurrenceFrequencyMonthly,
            "yearly": ek.EKRecurrenceFrequencyYearly,
        }[repeat]
        if repeat != "weekdays":
            return ek.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(frequency, 1, None)
        days = [ek.EKRecurrenceDayOfWeek.dayOfWeek_(number) for number in range(2, 7)]  # Monday is 2
        return ek.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(
            frequency, 1, days, None, None, None, None, None, None
        )

    def _apply(self, raw: Any, values: dict[str, Any]) -> None:
        if "title" in values:
            raw.setTitle_(values["title"])
        if "all_day" in values:
            raw.setAllDay_(bool(values["all_day"]))
        if "start" in values:
            raw.setStartDate_(self._date(values["start"]))
        if "end" in values:
            raw.setEndDate_(self._date(values["end"]))
        if "location" in values:
            raw.setLocation_(values["location"] or None)
        if "notes" in values:
            raw.setNotes_(values["notes"] or None)
        if values.get("alert_minutes") is not None:
            for alarm in list(raw.alarms() or []):
                raw.removeAlarm_(alarm)
            raw.addAlarm_(self.ek.EKAlarm.alarmWithRelativeOffset_(-60.0 * values["alert_minutes"]))

    def create(self, draft: CalendarEvent, repeat: str | None) -> CalendarEvent:
        names = self._names()
        calendar = next((c for c in self._all() if _plain(names[str(c.calendarIdentifier())]) == _plain(draft.calendar)), None)
        if calendar is None:
            raise LookupError(f"no calendar called {draft.calendar!r}")
        raw = self.ek.EKEvent.eventWithEventStore_(self.store)
        raw.setCalendar_(calendar)
        self._apply(
            raw,
            {"title": draft.title, "all_day": draft.all_day, "start": draft.start, "end": draft.end, "alert_minutes": draft.alert_minutes}
            | ({"location": draft.location} if draft.location else {})
            | ({"notes": draft.notes} if draft.notes else {}),
        )
        if repeat:
            raw.addRecurrenceRule_(self._repeat_rule(repeat))
        ok, error = self.store.saveEvent_span_commit_error_(raw, self.ek.EKSpanThisEvent, True, None)
        if not ok:
            raise RuntimeError(f"Calendar refused the event: {error}")
        return self._event(raw)

    def update(self, event: CalendarEvent, changes: dict[str, Any], future: bool) -> CalendarEvent:
        raw = self._occurrence(event)
        self._apply(raw, changes)
        span = self.ek.EKSpanFutureEvents if future else self.ek.EKSpanThisEvent
        ok, error = self.store.saveEvent_span_commit_error_(raw, span, True, None)
        if not ok:
            raise RuntimeError(f"Calendar refused the change: {error}")
        return self._event(raw)

    def delete(self, event: CalendarEvent, future: bool) -> None:
        raw = self._occurrence(event)
        span = self.ek.EKSpanFutureEvents if future else self.ek.EKSpanThisEvent
        ok, error = self.store.removeEvent_span_commit_error_(raw, span, True, None)
        if not ok:
            raise RuntimeError(f"Calendar refused to remove it: {error}")

    def create_calendar(self, name: str) -> str:
        """A new calendar on this Mac ("On My Mac" when there is one, else the default account). Returns which."""
        local = next((source for source in self.store.sources() if source.sourceType() == self.ek.EKSourceTypeLocal), None)
        default = self.store.defaultCalendarForNewEvents()
        source = local or (default.source() if default is not None else None)
        if source is None:
            raise RuntimeError("Calendar has no account to put a calendar in")
        calendar = self.ek.EKCalendar.calendarForEntityType_eventStore_(self.ek.EKEntityTypeEvent, self.store)
        calendar.setTitle_(name)
        calendar.setSource_(source)
        ok, error = self.store.saveCalendar_commit_error_(calendar, True, None)
        if not ok:
            raise RuntimeError(f"Calendar refused the new calendar: {error}")
        return "on this Mac" if source is local else f"in {source.title()}"
