"""The `reminders` tool: Apple Reminders on the Mac running the assistant, through EventKit.

Only the lists named in `tools.yaml` (`reminders.lists`) are reached; the rest stay private.
The tool adds, lists and completes reminders; it never deletes or edits (a misheard
"delete" can't be undone). Reminders syncs through iCloud and alerts on its own.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import date, datetime, time as clock, timedelta
from typing import Any, Awaitable, Callable, Literal, Protocol

from victoria_voice_assistant.config.schema import RemindersConfig
from victoria_voice_assistant.tools.duration import parse_duration
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome

logger = logging.getLogger(__name__)

Access = Literal["granted", "denied", "not_determined", "restricted", "write_only"]
SETUP_STEPS = (
    "Reminders access isn't granted to the assistant. At the Mac running it, answer the Reminders prompt "
    "(or run `uv run assistant reminders grant`), or allow it in System Settings > Privacy & Security > Reminders."
)
MAX_LISTED = 15
SHOWN_FOR_S = 600.0  # how long "number 4" refers to the last numbered list shown on that device
AMBIGUOUS_WITHIN = 0.1  # names scoring this close to the best are asked about, not guessed
MATCH_AT_LEAST = 0.75  # how close a spoken list name must be (case and small misspellings forgiven)
FETCH_TIMEOUT_S = 10.0


class MissingList(LookupError):
    def __init__(self, name: str, visible: int) -> None:
        super().__init__(f"no Reminders list called {name!r} ({visible} lists visible)")
        self.name = name


@dataclass
class ReminderItem:
    id: str
    title: str
    list: str
    due: datetime | None = None
    has_time: bool = False  # False: due on a day, with no time
    completed: bool = False
    completed_at: datetime | None = None  # when it was checked off, when Reminders says


class ReminderBackend(Protocol):
    """What the tool needs from Reminders. Blocking calls; the service runs them off the event loop."""

    def access(self) -> Access: ...
    def request_access(self) -> bool: ...
    def list_names(self) -> list[str]: ...
    def incomplete(self, lists: list[str], due_before: datetime | None) -> list[ReminderItem]: ...
    def add(self, list_name: str, title: str, due: datetime | None, has_time: bool) -> ReminderItem: ...
    def complete(self, item_id: str) -> None: ...
    def create_list(self, name: str) -> str: ...
    def default_list_name(self) -> str | None: ...
    def all_items(self, list_name: str) -> list[ReminderItem]: ...
    def set_completed(self, item_id: str, completed: bool) -> None: ...
    def delete(self, item_id: str) -> None: ...


# --- spoken due dates -------------------------------------------------------------------------

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
PARTS_OF_DAY = {"morning": 9, "afternoon": 14, "evening": 18, "tonight": 20, "night": 20}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _time_of(text: str) -> clock | None:
    """The clock time in a spoken phrase: "at 9", "9:30 pm", "noon", "in the morning"."""
    if re.search(r"\bnoon\b|\bmidday\b", text):
        return clock(12, 0)
    if re.search(r"\bmidnight\b", text):
        return clock(0, 0)
    words = "|".join(_NUMBER_WORDS)
    pattern = rf"\b(?:at\s+)?(\d{{1,2}}|{words})(?::(\d{{2}}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)?(?=\s|$)"
    for match in re.finditer(pattern, text):
        said_at = re.search(rf"\bat\s+{re.escape(match.group(1))}\b", text)
        if not (match.group(2) or match.group(3) or said_at):
            continue  # a number that isn't a time ("November 20")
        hour = int(match.group(1)) if match.group(1).isdigit() else _NUMBER_WORDS[match.group(1)]
        minute = int(match.group(2) or 0)
        suffix = (match.group(3) or "").replace(".", "")
        if hour > 23 or minute > 59:
            return None
        part = next((PARTS_OF_DAY[name] for name in PARTS_OF_DAY if re.search(rf"\b{name}\b", text)), None)
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        elif suffix in ("", "oclock", "o'clock") and hour <= 12:
            if part is not None and part >= 14 and hour < 12:
                hour += 12  # "at 6 in the evening"
            elif part is None and 1 <= hour <= 7:
                hour += 12  # a bare "at 3" means the afternoon
        return clock(hour, minute)
    for name, hour in PARTS_OF_DAY.items():
        if re.search(rf"\b{name}\b", text):
            return clock(hour, 0)
    return None


def _day_of(text: str, today: date) -> date | None:
    if "day after tomorrow" in text:
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b|\btonight\b|\bthis (?:morning|afternoon|evening)\b", text):
        return today
    if re.search(r"\b(?:this )?weekend\b", text):
        return today + timedelta(days=(5 - today.weekday()) % 7)
    for index, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", text):
            ahead = (index - today.weekday()) % 7 or 7  # "Friday" said on a Friday is next week's
            return today + timedelta(days=ahead)
    if re.search(r"\bnext week\b", text):
        return today + timedelta(days=7 - today.weekday())  # next Monday
    month_names = "|".join(MONTHS)
    match = re.search(rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text) or re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_names})\b", text
    )
    if match:
        first, second = match.groups()
        month_name, day_text = (first, second) if not first.isdigit() else (second, first)
        month = MONTHS.index(month_name) + 1
        try:
            found = date(today.year, month, int(day_text))
        except ValueError:
            return None
        return found if found >= today else date(today.year + 1, month, int(day_text))
    match = re.search(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", text)
    if match:
        day_number = int(match.group(1))
        year, month = today.year, today.month
        if day_number < today.day:
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        try:
            return date(year, month, day_number)
        except ValueError:
            return None
    return None


def parse_due(text: str | None, now: datetime) -> tuple[datetime, bool] | None:
    """A spoken due time ("tomorrow at 9", "Friday", "in 2 hours") as local time, and whether it has a time of day.

    None when nothing in it can be read as a time or a day.
    """
    if not text or not text.strip():
        return None
    lowered = " ".join(text.lower().replace(",", " ").split())
    relative = re.fullmatch(r"in (.+)", lowered)
    if relative:
        seconds = parse_duration(relative.group(1))
        if seconds is not None and seconds > 0:
            return (now + timedelta(seconds=seconds)).replace(second=0, microsecond=0), True
    day = _day_of(lowered, now.date())
    at = _time_of(lowered)
    if day is None and at is None:
        return None
    if day is None:
        assert at is not None
        day = now.date() if at > now.time() else now.date() + timedelta(days=1)
    if at is None:
        return datetime.combine(day, clock(0, 0)), False
    return datetime.combine(day, at), True


def speak_due(due: datetime, has_time: bool, now: datetime) -> str:
    """ "today at 5 PM", "tomorrow at 9:30 AM", "Friday", "November 20"."""
    days = (due.date() - now.date()).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif days == -1:
        day = "yesterday"
    elif 1 < days < 7:
        day = WEEKDAYS[due.weekday()].capitalize()
    else:
        day = f"{MONTHS[due.month - 1].capitalize()} {due.day}"
    if not has_time:
        return day
    hour = due.hour % 12 or 12
    minutes = f":{due.minute:02d}" if due.minute else ""
    return f"{day} at {hour}{minutes} {'AM' if due.hour < 12 else 'PM'}"


# --- matching ---------------------------------------------------------------------------------

_SKIP = {"the", "a", "an", "to", "my", "of", "for", "some", "reminder", "reminders", "item"}


def _words(text: str) -> set[str]:
    return {word.rstrip("s") for word in re.findall(r"[a-z0-9']+", text.lower()) if word not in _SKIP}


def best_matches(items: list[ReminderItem], title: str) -> list[ReminderItem]:
    """The reminders whose titles share the most words with ``title``; an exact title wins outright."""
    exact = [item for item in items if item.title.strip().lower() == title.strip().lower()]
    if exact:
        return exact
    wanted = _words(title)
    if not wanted:
        return []
    scored = [(len(wanted & _words(item.title)), item) for item in items]
    best = max((score for score, _ in scored), default=0)
    return [item for score, item in scored if score == best] if best else []


# --- the service ------------------------------------------------------------------------------


class RemindersService:
    """The `reminders` tool's actions on the named lists."""

    def __init__(self, settings: RemindersConfig | None, backend: ReminderBackend | None = None) -> None:
        self.settings = settings
        self._backend = backend
        self._asking = False
        self._ensured: set[str] = set()
        self._shown: dict[str, tuple[str, list[Any], float]] = {}  # device -> the last numbered list it was shown
        self.on_granted: Callable[[], None] | None = None  # set by the app: rebuild the tools once access arrives

    @property
    def backend(self) -> ReminderBackend:
        if self._backend is None:
            self._backend = EventKitBackend()
        return self._backend

    def update_settings(self, settings: RemindersConfig | None) -> None:
        self.settings = settings

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
                logger.exception("Reminders access request failed", extra={"event": "reminders_access_failed"})
                granted = False
            finally:
                self._asking = False
            logger.info("Reminders access answered", extra={"event": "reminders_access", "status": "granted" if granted else "refused"})
            if granted and self.on_granted is not None:
                self.on_granted()

        logger.info("Asking for Reminders access", extra={"event": "reminders_access_asked"})
        threading.Thread(target=ask, name="reminders-access", daemon=True).start()

    def every_list(self) -> bool:
        return self.settings is not None and self.settings.lists == "all"

    def lists(self) -> list[str]:
        """The lists the tool may use: those named in config, or with `all` every list in the account."""
        if self.settings is None:
            return []
        return self.backend.list_names() if self.every_list() else list(self.settings.lists)

    def ensure_lists(self) -> None:
        """Create the configured lists Reminders doesn't have (Bill, 2026-11-11: "lets use event kit").

        Once per list name; the only write besides add and complete, and nothing is ever deleted or renamed.
        """
        if self.every_list():
            return  # nothing named, so nothing to create
        wanted = [name for name in self.lists() if name not in self._ensured]
        if not wanted:
            return
        present = {_plain(name) for name in self.backend.list_names()}
        for name in wanted:
            if _plain(name) not in present:
                try:
                    where = self.backend.create_list(name)
                except Exception as exc:  # noqa: BLE001 - said when the list is used ("There's no ... list")
                    logger.warning("Reminders list not created", extra={"event": "reminders_list_failed", "reason": str(exc)})
                    continue
                logger.info("Reminders list created", extra={"event": "reminders_list_created", "status": where})
            self._ensured.add(name)

    def default_list(self) -> str | None:
        if self.settings is None:
            return None
        if self.settings.default_list:
            return self.settings.default_list
        if self.every_list():
            return self.backend.default_list_name()
        return self.settings.lists[0] if self.settings.lists else None

    def _remember(self, device: str, kind: str, entries: list[Any]) -> None:
        self._shown[device] = (kind, entries, time.monotonic())

    def _shown_entry(self, device: str, kind: str, number: int) -> Any | None:
        """The ``number``th entry of the last numbered list of ``kind`` shown to ``device``, while it's recent."""
        shown = self._shown.get(device)
        if shown is None or shown[0] != kind or time.monotonic() - shown[2] > SHOWN_FOR_S:
            return None
        entries = shown[1]
        return entries[number - 1] if 1 <= number <= len(entries) else None

    def _resolve_list(self, spoken: str, device: str, names: list[str]) -> str | ToolOutcome:
        """The list a spoken name or number means, forgiving case and spelling; else what to say."""
        # A list's own name first: "2026", "My List" and "List" are names before they're numbers or filler words.
        exact = next((name for name in names if _plain(name) == _plain(spoken)), None)
        if exact is not None:
            return exact
        number = spoken_number(spoken)
        if number is not None:
            chosen = self._shown_entry(device, "lists", number)
            if chosen is not None:
                return chosen
            return ToolOutcome(result={"error": f"no list number {number}"}, speak=f"I don't have a list number {number}. Ask what lists there are first.")
        ranked = sorted(((list_score(spoken, name), name) for name in names), reverse=True)
        if ranked and ranked[0][0] >= MATCH_AT_LEAST:
            best = ranked[0][0]
            close = [name for score, name in ranked if score >= MATCH_AT_LEAST and best - score < AMBIGUOUS_WITHIN]
            if len(close) == 1:
                return close[0]
            question = f"Which list: {_join(close, 'or')}?"
            return ToolOutcome(result={"question": question, "options": close}, speak=question, pending=PendingQuestion(kind="clarify", question=question, options=close))
        if self.every_list():
            self._remember(device, "lists", names)
            return ToolOutcome(
                result={"error": f"no list called {spoken}", "lists": names},
                speak=numbered(f"There's no {spoken} list. The lists are:", names),
            )
        return ToolOutcome(result={"error": f"no list called {spoken}", "lists": names}, speak=f"I can only use these lists: {_join(names)}.")

    async def handle(self, arguments: dict[str, Any], now: datetime | None = None, device: str = "web") -> ToolOutcome:
        now = now or datetime.now()
        action = str(arguments.get("action") or "")
        # Reminders is asked off the event loop (it also carries the audio), and once for the whole request.
        if await asyncio.to_thread(self.access) != "granted":
            return ToolOutcome(result={"error": "no access"}, speak="I can't reach Reminders yet.", error=True)
        names, default = await asyncio.to_thread(lambda: (self.lists(), self.default_list()))
        raw_list = arguments.get("list")
        spoken_list = str(raw_list).strip() if isinstance(raw_list, (str, int)) and str(raw_list).strip() else None
        list_name: str | None = None
        if spoken_list:
            resolved = self._resolve_list(spoken_list, device, names)
            if isinstance(resolved, ToolOutcome):
                return resolved
            list_name = resolved
        try:
            if action == "lists":
                self._remember(device, "lists", names)
                header = f"You have {len(names)} list{'s' if len(names) != 1 else ''}:"
                return ToolOutcome(result={"lists": names, "default": default}, speak=numbered(header, names))
            if action == "add":
                return await self._add(arguments, list_name or default, now)
            if action == "list":
                return await self._list(arguments, list_name, names, now, device)
            if action == "complete":
                return await self._complete(arguments, list_name, names, now, device)
        except TimeoutError:
            logger.warning("Reminders didn't answer", extra={"event": "reminders_timeout", "kind": action})
            return ToolOutcome(result={"error": "Reminders didn't answer"}, speak="Reminders didn't answer.", error=True)
        except MissingList as exc:  # renamed or removed in Reminders since it was named in tools.yaml
            logger.warning("Reminders list missing", extra={"event": "reminders_failed", "kind": action, "reason": str(exc)})
            return ToolOutcome(result={"error": str(exc)}, speak=f"There's no {exc.name} list in Reminders.", error=True)
        except (LookupError, RuntimeError) as exc:  # a reminder deleted on the phone, a save refused
            logger.warning("Reminders refused", extra={"event": "reminders_failed", "kind": action, "reason": str(exc)})
            return ToolOutcome(result={"error": str(exc)}, speak="Reminders didn't take that.", error=True)
        return ToolOutcome(result={"error": f"unknown action {action!r}", "actions": ["add", "list", "complete", "lists"]}, error=True)

    async def _add(self, arguments: dict[str, Any], target: str | None, now: datetime) -> ToolOutcome:
        title = str(arguments.get("title") or "").strip()
        if not title:
            return ToolOutcome(result={"error": "no title"}, speak="What should the reminder say?")
        if target is None:
            return ToolOutcome(result={"error": "no lists configured"}, error=True)
        due_text = arguments.get("due") if isinstance(arguments.get("due"), str) else None
        due: datetime | None = None
        has_time = False
        if due_text and due_text.strip():
            parsed = parse_due(due_text, now)
            if parsed is None:
                return ToolOutcome(result={"error": f"couldn't read the time {due_text!r}"}, speak=f"When is that due? I didn't catch “{due_text}”.")
            due, has_time = parsed
        await asyncio.to_thread(self.backend.add, target, title, due, has_time)
        logger.info("Reminder added", extra={"event": "reminder_added", "status": "due" if due is not None else "no due time"})
        when = f", due {speak_due(due, has_time, now)}" if due is not None else ""
        # Always says the list (Bill, 2026-11-11: "I've added X to Y").
        return ToolOutcome(result={"added": title, "list": target, "due": due.isoformat() if due else None}, speak=f"I've added “{title}” to {target}{when}.")

    async def _list(self, arguments: dict[str, Any], list_name: str | None, names: list[str], now: datetime, device: str = "web") -> ToolOutcome:
        when = arguments.get("when")
        lists = [list_name] if list_name else names
        due_before: datetime | None = None
        if when == "overdue":
            due_before = now
        elif when == "today":
            due_before = datetime.combine(now.date() + timedelta(days=1), clock(0, 0))
        elif when == "this_week":
            due_before = datetime.combine(now.date() + timedelta(days=7 - now.weekday()), clock(0, 0))
        items = await asyncio.to_thread(self.backend.incomplete, lists, due_before)
        if when == "overdue":
            items = [item for item in items if item.due is not None and (item.due < now if item.has_time else item.due.date() < now.date())]
        items.sort(key=lambda item: (item.due is None, item.due or now))
        found = [
            {"title": item.title, "list": item.list} | ({"due": speak_due(item.due, item.has_time, now)} if item.due else {})
            for item in items[:MAX_LISTED]
        ]
        result: dict[str, Any] = {"reminders": found}
        self._remember(device, "reminders", items[:MAX_LISTED])  # "number 2 is done"
        if len(items) > MAX_LISTED:
            result["more"] = len(items) - MAX_LISTED
        scope = list_name or "your lists"
        phrase = {"overdue": "overdue", "today": "due today", "this_week": "due this week"}.get(str(when), "")
        if not found:
            return ToolOutcome(result=result, speak=f"Nothing {phrase} on {scope}." if phrase else f"Nothing on {scope}.")
        count = len(items)
        header = f"{count} reminder{'s' if count != 1 else ''}{' ' + phrase if phrase else ''} on {scope}:"
        lines = [
            item["title"]
            + (f" ({item['list']}" + (f", {item['due']})" if "due" in item else ")") if list_name is None else (f" ({item['due']})" if "due" in item else ""))
            for item in found
        ]
        more = [f"and {count - MAX_LISTED} more"] if count > MAX_LISTED else []
        return ToolOutcome(result=result, speak=numbered(header, lines, more))

    async def _complete(self, arguments: dict[str, Any], list_name: str | None, names: list[str], now: datetime, device: str = "web") -> ToolOutcome:
        title = str(arguments.get("title") or "").strip()
        if not title:
            return ToolOutcome(result={"error": "no title"}, speak="Which reminder?")
        number = spoken_number(title)
        if number is not None:
            shown = self._shown_entry(device, "reminders", number)
            if shown is None:
                return ToolOutcome(result={"error": f"no reminder number {number}"}, speak=f"I don't have a reminder number {number}. Ask what's on the list first.")
            await asyncio.to_thread(self.backend.complete, shown.id)
            logger.info("Reminder completed", extra={"event": "reminder_completed"})
            return ToolOutcome(result={"completed": shown.title, "list": shown.list}, speak=f"Marked “{shown.title}” done.")
        lists = [list_name] if list_name else names
        items = await asyncio.to_thread(self.backend.incomplete, lists, None)
        matches = best_matches(items, title)
        if not matches:
            return ToolOutcome(result={"error": f"nothing matches {title!r}"}, speak=f"I couldn't find “{title}” on {list_name or 'your lists'}.")
        names = sorted({item.title.casefold(): item.title for item in matches}.values())  # "Milk" and "milk" are one
        if len(names) > 1:
            question = f"Which one: {_join(names, 'or')}?"
            return ToolOutcome(
                result={"question": question, "options": names},
                speak=question,
                pending=PendingQuestion(kind="clarify", question=question, options=names),
            )
        item = matches[0]
        await asyncio.to_thread(self.backend.complete, item.id)
        logger.info("Reminder completed", extra={"event": "reminder_completed"})
        return ToolOutcome(result={"completed": item.title, "list": item.list}, speak=f"Marked “{item.title}” done.")


_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
}
_COUNT_WORDS = _NUMBER_WORDS | {"thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}


def spoken_number(text: str) -> int | None:
    """ "4", "number 4", "four", "the fourth one", "#4" as 4; None when it isn't just a number."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in ("number", "no", "the", "one", "list", "item")]
    if not words:
        return 1 if re.fullmatch(r"\W*(the )?one\W*", text.lower()) else None
    if len(words) != 1:
        return None
    word = re.sub(r"(st|nd|rd|th)$", "", words[0]) if re.fullmatch(r"\d+(st|nd|rd|th)", words[0]) else words[0]
    if word.isdigit():
        return int(word) or None
    return _COUNT_WORDS.get(word) or _ORDINAL_WORDS.get(word)


_NAME_FILLER = {"the", "my", "list", "lists", "a", "on", "show", "me", "what's", "whats"}


def list_score(spoken: str, name: str, name_weight: float = 0.2) -> float:
    """How closely a spoken list name matches a list's name, 0-1: case, word order and small misspellings don't matter.

    Mostly how many of the spoken words are in the name, then how much of the name was said, so
    "house" prefers House over Carriage House Needs; spelled-out similarity covers the rest.
    """
    said = [w for w in re.findall(r"[a-z0-9']+", spoken.lower()) if w not in _NAME_FILLER]
    have = [w for w in re.findall(r"[a-z0-9']+", name.lower()) if w not in _NAME_FILLER]
    if not said or not have:
        return 0.0

    def close(a: str, b: str) -> bool:
        return a == b or SequenceMatcher(None, a, b).ratio() >= 0.8

    said_found = sum(any(close(w, h) for h in have) for w in said) / len(said)
    name_found = sum(any(close(h, w) for w in said) for h in have) / len(have)
    by_words = (1 - name_weight) * said_found + name_weight * name_found
    whole = SequenceMatcher(None, " ".join(said), " ".join(have)).ratio()
    return max(by_words, whole)


def numbered(header: str, lines: list[str], after: list[str] | None = None) -> str:
    """A numbered list, one per line: shown as a list in chat, read in order by voice."""
    body = [f"{index}. {line}" for index, line in enumerate(lines, 1)]
    return "\n".join([header, *body, *(after or [])])


def _plain(name: str) -> str:
    return " ".join(name.split()).casefold()


def _join(names: list[str], word: str = "and") -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} {word} {names[-1]}"


# --- one list, read whole (the groceries tool) -------------------------------------------------


def item_score(said: str, name: str) -> float:
    """How closely a spoken item matches an item on the list: case, plurals and small misspellings don't matter.

    Stricter than list names: all of the item's name has to be said too, so "eggs" isn't Egg noodles.
    """
    return list_score(said, name, name_weight=0.5)


ITEM_MATCH_AT_LEAST = 0.8


def split_items(said: str) -> list[str]:
    """ "carrots, milk and pizza" as three items; without a comma it's one ("mac and cheese")."""
    if "," not in said:
        return [said] if said else []
    parts = [part.strip() for part in said.split(",")]
    parts = [re.sub(r"^(and|&)\s+", "", part) for part in parts]
    last = re.split(r"\s+(?:and|&)\s+", parts[-1], maxsplit=1)  # "..., milk and pizza"
    parts = parts[:-1] + last
    return [part for part in parts if part]


class ListTool:
    """A tool tied to one Reminders list (feature 20261112-024118; the first is groceries).

    Unchecked means we need it, checked means we have it. Questions get the whole list for the model;
    need, got and remove answer directly. Remove deletes the item: on this tool only (Bill, 2026-11-12).
    """

    def __init__(self, reminders: RemindersService, list_name: str) -> None:
        self.reminders = reminders
        self.list_name = list_name

    def ensure_list(self) -> None:
        """Make the list when Reminders doesn't have it (once)."""
        key = f"list:{self.list_name}"
        if key in self.reminders._ensured:
            return
        backend = self.reminders.backend
        if _plain(self.list_name) not in {_plain(name) for name in backend.list_names()}:
            try:
                where = backend.create_list(self.list_name)
            except Exception as exc:  # noqa: BLE001 - said when the list is used
                logger.warning("Reminders list not created", extra={"event": "reminders_list_failed", "reason": str(exc)})
                return
            logger.info("Reminders list created", extra={"event": "reminders_list_created", "status": where})
        self.reminders._ensured.add(key)

    def _match(self, items: list[ReminderItem], said: str, strict: bool) -> list[ReminderItem]:
        """The items ``said`` means: one, several close ones (ask which), or none.

        ``strict`` (adding: "we're out of eggs") needs the item's whole name, so Egg noodles stays alone;
        checking off or removing something already there takes part of a name ("onions": Red or White?).
        """
        exact = [item for item in items if _plain(item.title) == _plain(said)]
        if exact:
            return exact
        score, least = (item_score, ITEM_MATCH_AT_LEAST) if strict else (list_score, MATCH_AT_LEAST)
        ranked = sorted(((score(said, item.title), index) for index, item in enumerate(items)), reverse=True)
        if not ranked or ranked[0][0] < least:
            return []
        best = ranked[0][0]
        return [items[index] for value, index in ranked if value >= least and best - value < AMBIGUOUS_WITHIN]

    async def handle(self, arguments: dict[str, Any]) -> ToolOutcome:
        action = str(arguments.get("action") or "read")
        said = str(arguments.get("item") or "").strip()
        if action == "read":
            return await self._guarded(action, self._read)
        if action not in ("need", "got", "remove"):
            return ToolOutcome(result={"error": f"unknown action {action!r}", "actions": ["read", "need", "got", "remove"]}, error=True)
        if not said:
            return ToolOutcome(result={"error": "no item"}, speak="Which item?", direct=True)
        return await self.handle_many(action, split_items(said))

    async def handle_many(self, action: str, wanted: list[str]) -> ToolOutcome:
        """One action on one or more items ("carrots, milk and pizza"): the list is read once, each item is
        handled on its own, and the answers are said together."""

        async def run(items: list[ReminderItem]) -> ToolOutcome:
            outcomes = [await self._one(action, one, items) for one in wanted]
            if len(outcomes) == 1:
                return outcomes[0]
            return ToolOutcome(
                result={"items": [outcome.result for outcome in outcomes]},
                speak=self._together(outcomes),
                direct=True,
                error=all(outcome.error for outcome in outcomes),
                # A "Which one?" among them stays open for the answer.
                pending=next((outcome.pending for outcome in outcomes if outcome.pending is not None), None),
            )

        return await self._guarded(action, run)

    async def _guarded(self, action: str, run: Callable[[list[ReminderItem]], Awaitable[ToolOutcome]]) -> ToolOutcome:
        """Reads the list (off the event loop) and runs ``run`` on it, with Reminders' failures said plainly."""
        name = self.list_name
        try:
            if await asyncio.to_thread(self.reminders.access) != "granted":
                return ToolOutcome(result={"error": "no access"}, speak="I can't reach Reminders yet.", error=True)
            return await run(await asyncio.to_thread(self.reminders.backend.all_items, name))
        except MissingList as exc:
            logger.warning("Reminders list missing", extra={"event": "reminders_failed", "kind": action, "reason": str(exc)})
            return ToolOutcome(result={"error": str(exc)}, speak=f"There's no {name} list in Reminders.", error=True)
        except TimeoutError:
            logger.warning("Reminders didn't answer", extra={"event": "reminders_timeout", "kind": action})
            return ToolOutcome(result={"error": "Reminders didn't answer"}, speak="Reminders didn't answer.", error=True)
        except (LookupError, RuntimeError) as exc:
            logger.warning("Reminders refused", extra={"event": "reminders_failed", "kind": action, "reason": str(exc)})
            return ToolOutcome(result={"error": str(exc)}, speak="Reminders didn't take that.", error=True)

    async def _read(self, items: list[ReminderItem]) -> ToolOutcome:
        need = [item.title for item in items if not item.completed]
        have = sorted({item.title.casefold(): item.title for item in items if item.completed}.values(), key=str.casefold)
        return ToolOutcome(result={"list": self.list_name, "need": need, "have": have, "note": "need = unchecked (to buy); have = checked off"})

    async def _one(self, action: str, said: str, items: list[ReminderItem]) -> ToolOutcome:
        """One item. ``items`` is the list as read, and is kept in step with each change made here."""
        name = self.list_name
        backend = self.reminders.backend
        pool = [item for item in items if not item.completed] if action == "got" else items
        matches = self._match(pool, said, strict=action == "need")
        names = sorted({item.title.casefold(): item.title for item in matches}.values())
        if len(names) > 1:
            question = f"Which one: {_join(names, 'or')}?"
            return ToolOutcome(result={"question": question, "options": names}, speak=question, pending=PendingQuestion(kind="clarify", question=question, options=names))
        if action == "need":
            return await self._need(said, matches, items)
        if not matches:
            where = "unchecked on" if action == "got" else "on"
            return ToolOutcome(result={"error": f"{said} isn't {where} {name}"}, speak=f"“{said}” isn't {where} {name}.", direct=True)
        if action == "got":
            for item in matches:  # the same item twice ("Milk", "milk") is checked off once, together
                await asyncio.to_thread(backend.set_completed, item.id, True)
                item.completed = True
            logger.info("List item checked", extra={"event": "list_item_checked"})
            return ToolOutcome(result={"checked": matches[0].title, "list": name}, speak=f"I've checked off “{matches[0].title}” on {name}.", direct=True)
        for item in matches:
            await asyncio.to_thread(backend.delete, item.id)
            items.remove(item)
        logger.info("List item removed", extra={"event": "list_item_removed"})
        return ToolOutcome(result={"removed": matches[0].title, "list": name}, speak=f"I've taken “{matches[0].title}” off {name}.", direct=True)

    def _together(self, outcomes: list[ToolOutcome]) -> str:
        """Several items' answers as one sentence per kind: "I've added “Carrots”, “Milk” and “Eggs” to Groceries." """
        name = self.list_name
        groups: dict[str, list[str]] = {}
        others: list[str] = []
        for outcome in outcomes:
            result = outcome.result if isinstance(outcome.result, dict) else {}
            kind = next((key for key in ("added", "unchecked", "checked", "removed", "already") if key in result), None)
            if kind is None or outcome.pending is not None:
                others.append(outcome.speak or "")
            else:
                groups.setdefault(kind, []).append(f"“{result[kind]}”")
        said = {
            "added": lambda items: f"I've added {items} to {name}.",
            "unchecked": lambda items: f"I've put {items} back on {name}.",
            "checked": lambda items: f"I've checked off {items} on {name}.",
            "removed": lambda items: f"I've taken {items} off {name}.",
        }
        sentences = []
        for kind, items in groups.items():
            if kind == "already":
                sentences.append(f"{_join(items)} {'is' if len(items) == 1 else 'are'} already on {name}.")
            else:
                sentences.append(said[kind](_join(items)))
        return " ".join([*sentences, *others]).strip()

    async def _need(self, said: str, matches: list[ReminderItem], items: list[ReminderItem]) -> ToolOutcome:
        """ "Add ice cream", "we're out of pizza": on the list, unchecked."""
        name = self.list_name
        unchecked = [item for item in matches if not item.completed]
        if unchecked:
            return ToolOutcome(result={"already": unchecked[0].title, "list": name}, speak=f"“{unchecked[0].title}” is already on {name}.", direct=True)
        if matches:  # checked off before: uncheck it
            item = matches[0]
            await asyncio.to_thread(self.reminders.backend.set_completed, item.id, False)
            item.completed = False
            logger.info("List item unchecked", extra={"event": "list_item_unchecked"})
            return ToolOutcome(result={"unchecked": item.title, "list": name}, speak=f"I've put “{item.title}” back on {name}.", direct=True)
        title = said[:1].upper() + said[1:]
        items.append(await asyncio.to_thread(self.reminders.backend.add, name, title, None, False))
        logger.info("List item added", extra={"event": "list_item_added"})
        return ToolOutcome(result={"added": title, "list": name}, speak=f"I've added “{title}” to {name}.", direct=True)


# --- EventKit ---------------------------------------------------------------------------------


class EventKitBackend:
    """Apple Reminders through EventKit (PyObjC), in this process. macOS asks once for access."""

    def __init__(self) -> None:
        import EventKit  # noqa: PLC0415 - macOS only, loaded when first used

        self.ek = EventKit
        self.store = EventKit.EKEventStore.alloc().init()

    def access(self) -> Access:
        status = self.ek.EKEventStore.authorizationStatusForEntityType_(self.ek.EKEntityTypeReminder)
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

        self.store.requestFullAccessToRemindersWithCompletion_(finished)
        done.wait()  # until someone answers at the Mac
        if answer and answer[0]:
            self.store.reset()  # see the lists that were hidden before access
        return bool(answer and answer[0])

    def _calendars(self, names: list[str]) -> list[Any]:
        wanted = {_plain(name) for name in names}  # "Victoria test " in Reminders is still Victoria Test
        return [c for c in self.store.calendarsForEntityType_(self.ek.EKEntityTypeReminder) if _plain(str(c.title())) in wanted]

    def list_names(self) -> list[str]:
        return sorted(c.title() for c in self.store.calendarsForEntityType_(self.ek.EKEntityTypeReminder))

    def _item(self, reminder: Any) -> ReminderItem:
        due, has_time = None, False
        components = reminder.dueDateComponents()
        if components is not None:
            from Foundation import NSCalendar, NSDateComponentUndefined  # noqa: PLC0415

            moment = NSCalendar.currentCalendar().dateFromComponents_(components)
            if moment is not None:
                due = datetime.fromtimestamp(moment.timeIntervalSince1970())
                has_time = components.hour() != NSDateComponentUndefined
        return ReminderItem(
            id=str(reminder.calendarItemIdentifier()),
            title=str(reminder.title() or ""),
            list=str(reminder.calendar().title()),
            due=due,
            has_time=has_time,
        )

    def incomplete(self, lists: list[str], due_before: datetime | None) -> list[ReminderItem]:
        calendars = self._calendars(lists)
        if not calendars:
            return []
        from Foundation import NSDate  # noqa: PLC0415

        end = NSDate.dateWithTimeIntervalSince1970_(due_before.timestamp()) if due_before else None
        predicate = self.store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, end, calendars)
        done = threading.Event()
        found: list[Any] = []

        def fetched(reminders: Any) -> None:
            found.extend(reminders or [])
            done.set()

        self.store.fetchRemindersMatchingPredicate_completion_(predicate, fetched)
        if not done.wait(FETCH_TIMEOUT_S):
            raise TimeoutError("Reminders didn't answer")
        return [self._item(reminder) for reminder in found]

    def add(self, list_name: str, title: str, due: datetime | None, has_time: bool) -> ReminderItem:
        calendars = self._calendars([list_name])
        if not calendars:
            raise MissingList(list_name, len(self.store.calendarsForEntityType_(self.ek.EKEntityTypeReminder)))
        reminder = self.ek.EKReminder.reminderWithEventStore_(self.store)
        reminder.setTitle_(title)
        reminder.setCalendar_(calendars[0])
        if due is not None:
            from Foundation import NSDate, NSDateComponents  # noqa: PLC0415

            components = NSDateComponents.alloc().init()
            components.setYear_(due.year)
            components.setMonth_(due.month)
            components.setDay_(due.day)
            if has_time:
                components.setHour_(due.hour)
                components.setMinute_(due.minute)
                # The alert on Bill's devices comes from this alarm, not from the assistant.
                reminder.addAlarm_(self.ek.EKAlarm.alarmWithAbsoluteDate_(NSDate.dateWithTimeIntervalSince1970_(due.timestamp())))
            reminder.setDueDateComponents_(components)
        ok, error = self.store.saveReminder_commit_error_(reminder, True, None)
        if not ok:
            raise RuntimeError(f"Reminders refused the reminder: {error}")
        return self._item(reminder)

    def create_list(self, name: str) -> str:
        """A new list on this Mac ("On My Mac" when there is one, else the default account). Returns which."""
        local = next((source for source in self.store.sources() if source.sourceType() == self.ek.EKSourceTypeLocal), None)
        default = self.store.defaultCalendarForNewReminders()
        source = local or (default.source() if default is not None else None)
        if source is None:
            raise RuntimeError("Reminders has no account to put a list in")
        calendar = self.ek.EKCalendar.calendarForEntityType_eventStore_(self.ek.EKEntityTypeReminder, self.store)
        calendar.setTitle_(name)
        calendar.setSource_(source)
        ok, error = self.store.saveCalendar_commit_error_(calendar, True, None)
        if not ok:
            raise RuntimeError(f"Reminders refused the list: {error}")
        return "on this Mac" if source is local else f"in {source.title()}"

    def all_items(self, list_name: str) -> list[ReminderItem]:
        """Every reminder on one list, checked or not."""
        calendars = self._calendars([list_name])
        if not calendars:
            raise MissingList(list_name, len(self.store.calendarsForEntityType_(self.ek.EKEntityTypeReminder)))
        predicate = self.store.predicateForRemindersInCalendars_(calendars)
        done = threading.Event()
        found: list[Any] = []

        def fetched(reminders: Any) -> None:
            found.extend(reminders or [])
            done.set()

        self.store.fetchRemindersMatchingPredicate_completion_(predicate, fetched)
        if not done.wait(FETCH_TIMEOUT_S):
            raise TimeoutError("Reminders didn't answer")
        items = []
        for reminder in found:
            item = self._item(reminder)
            item.completed = bool(reminder.isCompleted())
            when = reminder.completionDate()
            item.completed_at = datetime.fromtimestamp(when.timeIntervalSince1970()) if when is not None else None
            items.append(item)
        return items

    def set_completed(self, item_id: str, completed: bool) -> None:
        reminder = self.store.calendarItemWithIdentifier_(item_id)
        if reminder is None:
            raise LookupError("that reminder is gone")
        reminder.setCompleted_(completed)
        ok, error = self.store.saveReminder_commit_error_(reminder, True, None)
        if not ok:
            raise RuntimeError(f"Reminders refused the change: {error}")

    def delete(self, item_id: str) -> None:
        reminder = self.store.calendarItemWithIdentifier_(item_id)
        if reminder is None:
            return  # already gone
        ok, error = self.store.removeReminder_commit_error_(reminder, True, None)
        if not ok:
            raise RuntimeError(f"Reminders refused to remove it: {error}")

    def default_list_name(self) -> str | None:
        calendar = self.store.defaultCalendarForNewReminders()
        return str(calendar.title()) if calendar is not None else None

    def complete(self, item_id: str) -> None:
        reminder = self.store.calendarItemWithIdentifier_(item_id)
        if reminder is None:
            raise LookupError("that reminder is gone")
        reminder.setCompleted_(True)
        ok, error = self.store.saveReminder_commit_error_(reminder, True, None)
        if not ok:
            raise RuntimeError(f"Reminders refused the change: {error}")
