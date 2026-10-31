from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

# Long enough to answer by voice; text chat uses a longer window (see decisions log).
VOICE_PENDING_S = 10.0


@dataclass
class PendingQuestion:
    """A question the assistant just asked, resolved by the next user turn.

    ``yes_no`` questions are matched without a model call; ``clarify`` answers go to
    the model with the question already in the conversation; ``answer`` questions are an
    agent's, and the next turn goes to that agent as its answer, with no model call.
    """

    kind: Literal["clarify", "yes_no", "answer"]
    question: str
    options: list[str] = field(default_factory=list)
    # For yes/no: the follow-up to run on "yes" (tool name and arguments).
    on_yes: tuple[str, dict[str, Any]] | None = None
    # For answer: the agent task waiting for it; the next turn is passed to the agent as it is.
    task_id: str | None = None
    expires_at: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at


@dataclass
class ToolOutcome:
    """What a tool handler produced.

    ``result`` goes into the conversation as the tool result. ``speak`` is said
    directly with no second model call. ``chat`` is extra text for the chat client
    only (never spoken). ``pending`` asks a question the next turn resolves.
    """

    result: Any = None
    speak: str | None = None
    chat: str | None = None
    chat_title: str | None = None
    pending: PendingQuestion | None = None
    error: bool = False
