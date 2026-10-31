"""The built-in `tasks` tool (architecture §4.1): "What's Hermes working on?", "Is the meal plan done?",
"Cancel the research task." Answers are spoken directly, with no second model call.
"""

from __future__ import annotations

import re
from typing import Any

from victoria_voice_assistant.agents.speech import first_sentences, plain_text
from victoria_voice_assistant.agents.tasks import Task, TaskService
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "what", "whats", "task", "job", "about", "please", "from",
    "you", "your", "are", "was", "is", "to", "a", "an", "of", "on", "in", "me", "my", "it", "its", "done",
}
ENDED = {"failed": "didn't finish", "timed_out": "took too long and was stopped", "cancelled": "was cancelled"}


def _label(name: str) -> str:
    return name[:1].upper() + name[1:]


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9']+", text.lower()) if word not in STOPWORDS and len(word) > 1}


def _gist(request: str, limit: int = 8) -> str:
    words = request.strip().rstrip("?.!").split()
    return " ".join(words[:limit]) + ("..." if len(words) > limit else "")


def _matches(tasks: list[Task], agent: str | None, about: str | None) -> list[Task]:
    """Tasks for the named agent and/or sharing words with `about`, best first. All of them if neither is given."""
    wanted = _words(about or "")
    scored = []
    for task in tasks:
        if agent and task.agent != agent:
            continue
        overlap = len(wanted & _words(task.request)) if wanted else 0
        if wanted and not overlap:
            continue
        scored.append((overlap, task.created_at, task))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0][0] if scored else 0
    return [task for overlap, _, task in scored if overlap == best]


def _describe(tasks: list[Task]) -> str:
    return ", ".join(f"{_label(t.agent)} on {_gist(t.request, 5)}" for t in tasks[:-1]) + (
        f" or {_label(tasks[-1].agent)} on {_gist(tasks[-1].request, 5)}" if len(tasks) > 1 else ""
    )


async def tasks_tool(tasks: TaskService, arguments: dict[str, Any]) -> ToolOutcome:
    action = str(arguments.get("action") or "list")
    agent = str(arguments.get("agent") or "").strip().lower() or None
    about = str(arguments.get("about") or "").strip() or None

    if action == "list":
        active = [t for t in tasks.active() if not agent or t.agent == agent]
        if not active:
            who = f"{_label(agent)} isn't" if agent else "Nothing is"
            return ToolOutcome(result={"running": []}, speak=f"{who} working on anything right now.")
        lines = [f"{_label(t.agent)} is working on {_gist(t.request)}." for t in active[:3]]
        more = f" And {len(active) - 3} more." if len(active) > 3 else ""
        return ToolOutcome(result={"running": [t.snapshot() for t in active]}, speak=" ".join(lines) + more)

    if action == "status":
        found = _matches(tasks.recent(), agent, about)
        if not found:
            return ToolOutcome(result={"found": None}, speak="I don't see a task like that.")
        task = found[0]
        name = _label(task.agent)
        if task.active:
            speak = f"{name} is still working on {_gist(task.request)}."
        elif task.state == "finished":
            speak = f"Yes, {name} finished. {first_sentences(plain_text(task.result or ''))}".strip()
        else:
            speak = f"{name}'s task {ENDED.get(task.state, task.state)}."
        return ToolOutcome(result={"found": task.snapshot()}, speak=speak)

    if action == "cancel":
        active = tasks.active()
        if not active:
            return ToolOutcome(result={"cancelled": None}, speak="Nothing is running.")
        found = _matches(active, agent, about)
        if not found:
            return ToolOutcome(result={"cancelled": None}, speak="I don't see a running task like that.")
        if len(found) > 1:
            question = f"Which one: {_describe(found)}?"
            return ToolOutcome(
                result={"choices": [t.snapshot() for t in found]},
                speak=question,
                pending=PendingQuestion(kind="clarify", question=question, options=[t.request for t in found]),
            )
        task = found[0]
        tasks.cancel(task.id)
        return ToolOutcome(result={"cancelled": task.id}, speak=f"Cancelled {_label(task.agent)}'s task.")

    return ToolOutcome(result={"error": f"unknown action {action!r}"}, speak="Sorry, I can't do that with tasks.")
