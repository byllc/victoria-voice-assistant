"""The built-in `notifications` tool (architecture §10.3): "Any notifications?", "What did Hermes say?",
"Clear my notifications", "Do not disturb until 8 a.m.". Answers are spoken directly, with no second model call.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from victoria_voice_assistant.agents.speech import first_sentences, plain_text
from victoria_voice_assistant.notifications.service import Notification, NotificationService, _label, _names
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome

if TYPE_CHECKING:
    from victoria_voice_assistant.agents.tasks import TaskService

# Read this many aloud; the rest stay on the screen.
READ_AT_MOST = 3
OFF = {"", "off", "none", "stop", "cancel", "end", "no"}
CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(a\.?\s*m\.?|p\.?\s*m\.?)?$")


def parse_until(text: str, now: datetime) -> datetime | None:
    """'8 a.m.', '8:30pm', '07:00', 'noon', 'midnight' -> the next time it's that o'clock. None if not a time."""
    value = text.strip().lower().removeprefix("until ").strip()
    if value in {"noon", "midday"}:
        hour, minute = 12, 0
    elif value == "midnight":
        hour, minute = 0, 0
    else:
        match = CLOCK.match(value)
        if match is None:
            return None
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridiem = (match.group(3) or "").replace(".", "").replace(" ", "")
        if minute > 59 or hour > 23 or (meridiem and not 1 <= hour <= 12):
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    until = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return until if until > now else until + timedelta(days=1)


def spoken_time(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    minutes = f":{moment.minute:02d}" if moment.minute else ""
    return f"{hour}{minutes} {'a.m.' if moment.hour < 12 else 'p.m.'}"


def _sentence(text: str) -> str:
    return text if text.endswith(".") else text + "."


def _read_out(items: list[Notification]) -> str:
    return " ".join(f"{item.headline} {item.summary}".strip() for item in items)


def _full_text(items: list[Notification]) -> str | None:
    texts = [f"{_label(item.source)}: {item.full}" for item in items if item.full and item.full != item.summary]
    return "\n\n".join(texts) or None


async def notifications_tool(
    service: NotificationService, arguments: dict[str, Any], device: str, tasks: TaskService | None = None
) -> ToolOutcome:
    action = str(arguments.get("action") or "list")
    source = str(arguments.get("source") or "").strip().lower() or None

    if action == "list":
        unread = [i for i in service.unread(device) if not source or i.source == source]
        dnd = service.dnd_until
        quiet = " " + _sentence(f"Do not disturb is on until {spoken_time(datetime.fromtimestamp(dnd))}") if dnd else ""
        if not unread:
            return ToolOutcome(result={"unread": []}, speak=f"No new notifications.{quiet}")
        count = "one notification" if len(unread) == 1 else f"{len(unread)} notifications"
        question = f"You have {count}, from {_names(unread)}.{quiet} Want to hear {'it' if len(unread) == 1 else 'them'}?"
        return ToolOutcome(
            result={"unread": [i.snapshot() for i in unread]},
            speak=question,
            pending=PendingQuestion(
                kind="yes_no", question=question, on_yes=("notifications", {"action": "read", "ids": [i.id for i in unread]})
            ),
        )

    if action == "read":
        ids = arguments.get("ids")
        if ids:  # "yes" to "Want to hear it?"
            items = [item for i in ids if (item := service.get(str(i))) is not None]
        elif source:
            item = service.last_from(source)
            if item is None:
                return _last_task_result(tasks, source)
            service.acknowledge([item])
            return ToolOutcome(
                result={"read": [item.snapshot()]},
                speak=f"{_label(source)} said: {item.summary}",
                chat=_full_text([item]),
                chat_title=f"{source}: full reply",
            )
        else:
            items = service.unread(device)
        if not items:
            return ToolOutcome(result={"read": []}, speak="No new notifications.")
        spoken, rest = items[:READ_AT_MOST], items[READ_AT_MOST:]
        speak = _read_out(spoken) + (f" And {len(rest)} more on the screen." if rest else "")
        # A question stays unread until it's answered; reading it out makes the next turn its answer.
        question = next((i for i in spoken if i.kind == "question" and i.task_id), None)
        service.acknowledge([i for i in spoken if i.kind != "question"])
        return ToolOutcome(
            result={"read": [i.snapshot() for i in spoken]},
            speak=speak,
            chat=_full_text(spoken),
            chat_title="Full text",
            pending=PendingQuestion(kind="answer", question=speak, task_id=question.task_id) if question else None,
        )

    if action == "dismiss":
        unread = [i for i in service.unread(device) if not source or i.source == source]
        if not unread:
            return ToolOutcome(result={"dismissed": 0}, speak="There's nothing to clear.")
        service.acknowledge(unread)
        return ToolOutcome(result={"dismissed": len(unread)}, speak="Okay, cleared.")

    if action == "do_not_disturb":
        until_text = str(arguments.get("until") or "").strip()
        if until_text.lower() in OFF:
            service.set_dnd(None)
            return ToolOutcome(result={"do_not_disturb": None}, speak="Do not disturb is off.")
        now = datetime.fromtimestamp(service.now())
        minutes = arguments.get("minutes")
        until = now + timedelta(minutes=float(minutes)) if minutes else parse_until(until_text, now)
        if until is None:
            return ToolOutcome(result={"error": f"not a time: {until_text!r}"}, speak="Until what time?")
        service.set_dnd(until.timestamp())
        return ToolOutcome(
            result={"do_not_disturb": until.isoformat(timespec="minutes")},
            speak=_sentence(f"Do not disturb until {spoken_time(until)}"),
        )

    return ToolOutcome(result={"error": f"unknown action {action!r}"}, speak="Sorry, I can't do that with notifications.")


def _last_task_result(tasks: TaskService | None, source: str) -> ToolOutcome:
    """A result that came back while we were waiting never became a notification: use the task record."""
    finished = [t for t in (tasks.recent(50) if tasks else []) if t.agent == source and t.state == "finished" and t.result]
    if not finished:
        return ToolOutcome(result={"read": []}, speak=f"I don't have anything from {_label(source)}.")
    task = finished[0]
    text = plain_text(task.result or "")
    return ToolOutcome(
        result={"read": [task.snapshot()]},
        speak=f"{_label(source)} said: {first_sentences(text)}",
        chat=task.result if first_sentences(text) != text else None,
        chat_title=f"{source}: full reply",
    )
