"""The spend log (architecture §7.1): every agent model call's tokens, cost and how its model was chosen."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from victoria_voice_assistant.config.schema import ModelConfig

logger = logging.getLogger(__name__)


def cost_usd(model: ModelConfig, input_tokens: int, output_tokens: int) -> float:
    if model.price is None:
        return 0.0
    return (input_tokens * model.price.input + output_tokens * model.price.output) / 1_000_000


@dataclass
class SpendEntry:
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    pool: str | None = None
    task_id: str | None = None
    reason: str | None = None  # how the model was chosen: override, decider, low_confidence, budget_downgrade...
    decider_choice: str | None = None
    confidence: float | None = None


class SpendLog:
    def __init__(self, database_path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.database_path = database_path
        self._clock = clock
        self._memory: list[tuple[str, SpendEntry]] = []  # used when there is no database (tests)

    def _month(self, at: float | None = None) -> str:
        return time.strftime("%Y-%m", time.localtime(self._clock() if at is None else at))

    def record(self, entry: SpendEntry) -> None:
        at = self._clock()
        month = self._month(at)
        logger.info(
            "Agent model spend",
            extra={
                "event": "spend",
                "agent": entry.agent,
                "model": entry.model,
                "task": entry.task_id,
                "reason": entry.reason,
                "choice": entry.decider_choice,
                "confidence": entry.confidence,
                "count": entry.input_tokens + entry.output_tokens,
                "cost_usd": round(entry.cost_usd, 6),
            },
        )
        if self.database_path is None:
            self._memory.append((month, entry))
            return
        row = {"at": at, "month": month, **asdict(entry)}
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                f"INSERT INTO spend ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values())
            )

    def month(self, back: int = 0) -> str:
        """The calendar month `back` months before this one, as the log names it ("2026-11")."""
        year, month = (int(part) for part in self._month().split("-"))
        index = year * 12 + month - 1 - back
        return f"{index // 12:04d}-{index % 12 + 1:02d}"

    def by_agent(self, month: str) -> dict[str, float]:
        """Each agent's spend in a month, in dollars; agents that spent nothing are left out."""
        if self.database_path is None:
            totals: dict[str, float] = {}
            for logged, entry in self._memory:
                if logged == month:
                    totals[entry.agent] = totals.get(entry.agent, 0.0) + entry.cost_usd
        else:
            with sqlite3.connect(self.database_path) as connection:
                totals = dict(connection.execute("SELECT agent, SUM(cost_usd) FROM spend WHERE month = ? GROUP BY agent", (month,)))
        return {agent: float(total) for agent, total in totals.items() if total and total > 0}

    def month_total(self, pool: str) -> float:
        """This calendar month's spend for a pool, in dollars."""
        month = self._month()
        if self.database_path is None:
            return sum(entry.cost_usd for m, entry in self._memory if m == month and entry.pool == pool)
        with sqlite3.connect(self.database_path) as connection:
            (total,) = connection.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE pool = ? AND month = ?", (pool, month)
            ).fetchone()
        return float(total)


def speak_usd(amount: float) -> str:
    """A dollar amount as it should be said: "$4.50", "12 cents", "less than a cent"."""
    if amount < 0.005:
        return "less than a cent"
    cents = round(amount * 100)
    if cents < 100:
        return f"{cents} cent{'s' if cents != 1 else ''}"
    return f"${cents / 100:,.2f}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def spend_answer(log: SpendLog, agent: str | None, budget_usd: float | None, last_month: bool) -> tuple[dict, str]:
    """What was spent on one agent, or on each agent when none is named: the result and what to say.

    `agent` is its configured name. `budget_usd` is that agent's monthly budget, said for this month only.
    """
    totals = log.by_agent(log.month(1 if last_month else 0))
    when = "last month" if last_month else "this month"
    did = "We spent" if last_month else "We've spent"
    if agent is not None:
        spent = totals.get(agent, 0.0)
        name = agent[:1].upper() + agent[1:]
        result = {"agent": agent, "month": when, "spent_usd": round(spent, 4), "budget_usd": None if last_month else budget_usd}
        if spent <= 0:
            return result, f"Nothing spent on {name} {when}."
        budget = f", of a {speak_usd(budget_usd)} budget" if budget_usd and not last_month else ""
        return result, f"{did} {speak_usd(spent)} on {name} {when}{budget}."
    result = {"month": when, "spent_usd": {name: round(spent, 4) for name, spent in totals.items()}}
    if not totals:
        return result, f"Nothing spent on agents {when}."
    parts = [f"{speak_usd(spent)} on {name[:1].upper() + name[1:]}" for name, spent in sorted(totals.items(), key=lambda item: -item[1])]
    return result, f"{did} {_join(parts)} {when}."
