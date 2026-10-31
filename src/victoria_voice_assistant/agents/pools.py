"""Picking a pool member per request (architecture §7.1, job 1): the cheapest paid model that will do.

Order: a spoken override ("ask Claude Opus to ...") picks the member; otherwise the decider
picks from each member's `good_for`, and a pick below `min_confidence` (or no pick) falls
back to the pool's `default`. Budget rules then apply to every choice: past `downgrade_at`
nothing above the default, past `stop_at` no paid call at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from victoria_voice_assistant.agents.spend import SpendLog
from victoria_voice_assistant.config.schema import ModelsConfig, PoolConfig
from victoria_voice_assistant.decider.engine import DecisionModel

logger = logging.getLogger(__name__)

DECIDER_QUESTION = "Which is the cheapest model likely to handle this request well?"


@dataclass
class PoolChoice:
    model: str | None  # None: the budget is used up, make no paid call
    reason: str  # override, decider, low_confidence, no_decider, budget_downgrade, budget_stop
    confidence: float | None = None
    decider_choice: str | None = None


def split_override(pool: PoolConfig, request: str) -> tuple[str | None, str]:
    """ "Opus to plan the trip" -> ("claude-opus", "plan the trip") when "opus" is a spoken override."""
    for alias in sorted(pool.spoken_overrides, key=len, reverse=True):
        match = re.match(rf"^\W*{re.escape(alias)}\b[\s,:]*(?:to\s+)?(.+)$", request, flags=re.IGNORECASE | re.DOTALL)
        if match and match.group(1).strip():
            return pool.spoken_overrides[alias], match.group(1).strip()
    return None, request


class PoolChooser:
    def __init__(self, models: ModelsConfig, decider: DecisionModel | None, spend: SpendLog) -> None:
        self.models = models
        self.decider = decider
        self.spend = spend

    def budget_used(self, pool_name: str) -> float | None:
        """Fraction of this month's budget spent, or None when the pool has no budget."""
        budget = self.models.pools[pool_name].budget
        if budget is None:
            return None
        return self.spend.month_total(pool_name) / budget.monthly_usd

    def over_budget(self, pool_name: str) -> bool:
        pool = self.models.pools[pool_name]
        used = self.budget_used(pool_name)
        return used is not None and pool.budget is not None and used >= pool.budget.stop_at

    async def choose(self, pool_name: str, request: str, override: str | None = None) -> PoolChoice:
        pool = self.models.pools[pool_name]
        members = [member.model for member in pool.members]
        if override is not None:
            choice = PoolChoice(override, "override")
        elif self.decider is None or len(members) == 1:
            choice = PoolChoice(pool.default if len(members) > 1 else members[0], "no_decider")
        else:
            decision = await self.decider.decide(
                f"Spoken request: {request!r}", DECIDER_QUESTION, {m.model: m.good_for for m in pool.members}
            )
            if decision.choice is not None and decision.confidence >= pool.min_confidence:
                choice = PoolChoice(decision.choice, "decider", decision.confidence, decision.choice)
            else:
                choice = PoolChoice(pool.default, "low_confidence", decision.confidence, decision.choice)
        choice = self._apply_budget(pool_name, choice)
        logger.info(
            "Pool choice",
            extra={
                "event": "pool_choice",
                "agent": pool_name,
                "model": choice.model,
                "reason": choice.reason,
                "choice": choice.decider_choice,
                "confidence": None if choice.confidence is None else round(choice.confidence, 3),
            },
        )
        return choice

    def escalate(self, pool_name: str, model: str) -> str | None:
        """The next member up from `model`, if there is one and the budget allows it."""
        pool = self.models.pools[pool_name]
        members = [member.model for member in pool.members]
        if model not in members or members.index(model) + 1 >= len(members):
            return None
        step_up = members[members.index(model) + 1]
        allowed = self._apply_budget(pool_name, PoolChoice(step_up, "escalation"))
        return allowed.model if allowed.model == step_up else None

    def _apply_budget(self, pool_name: str, choice: PoolChoice) -> PoolChoice:
        pool = self.models.pools[pool_name]
        used = self.budget_used(pool_name)
        if used is None or pool.budget is None or choice.model is None:
            return choice
        if used >= pool.budget.stop_at:
            return PoolChoice(None, "budget_stop", choice.confidence, choice.decider_choice)
        members = [member.model for member in pool.members]
        if used >= pool.budget.downgrade_at and members.index(choice.model) > members.index(pool.default):
            return PoolChoice(pool.default, "budget_downgrade", choice.confidence, choice.decider_choice)
        return choice
