"""Small `python` tools that run inside the service."""

from __future__ import annotations

from datetime import datetime

from victoria_voice_assistant.tools.outcome import ToolOutcome


def current_time() -> ToolOutcome:
    now = datetime.now().astimezone()
    return ToolOutcome(
        result={"time": now.strftime("%-I:%M %p"), "date": now.strftime("%A, %B %-d, %Y")},
        speak=f"It's {now.strftime('%-I:%M %p')}.",
    )


def current_date() -> ToolOutcome:
    now = datetime.now().astimezone()
    return ToolOutcome(
        result={"date": now.strftime("%A, %B %-d, %Y")},
        speak=f"It's {now.strftime('%A, %B %-d')}.",
    )
