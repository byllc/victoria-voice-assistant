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
