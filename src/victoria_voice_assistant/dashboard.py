"""The task dashboard's data (architecture §4.1), sent to the web client over its chat websocket."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from victoria_voice_assistant.agents.runner import AgentRunner
    from victoria_voice_assistant.agents.tasks import Task
    from victoria_voice_assistant.tools.registry import Services


def task_view(runner: AgentRunner, task: Task) -> dict[str, Any]:
    return {
        **task.snapshot(),
        "label": runner.describe(task),
        "progress": [{"at": at, "text": text} for at, text in task.progress],
        "cancel_note": runner.cancel_note(task) if task.active else None,
        "resend": runner.resend_choices(task),
    }


def dashboard(services: Services) -> dict[str, Any]:
    """Every task (newest first), the running and waiting counts, this month's spend per budgeted pool, and health."""
    runner = services.agents
    tasks = runner.tasks.recent(limit=10_000) if runner is not None else []
    spend = []
    if runner is not None:
        for name, pool in runner.config.models.pools.items():
            if pool.budget is not None:
                spend.append(
                    {"pool": name, "used_usd": round(runner.spend.month_total(name), 4), "budget_usd": pool.budget.monthly_usd}
                )
    return {
        "type": "dashboard",
        "tasks": [task_view(runner, task) for task in tasks] if runner is not None else [],
        "summary": {
            "running": sum(1 for t in tasks if t.state in ("queued", "running")),
            "waiting": sum(1 for t in tasks if t.state == "waiting"),
            "spend": spend,
            "health": services.health.snapshot() if services.health is not None else [],
        },
    }
