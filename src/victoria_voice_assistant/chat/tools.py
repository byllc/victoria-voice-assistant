from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pipecat.adapters.llm.schemas.direct_function import tool_options
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.services.llm_service import FunctionCallParams

from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome
from victoria_voice_assistant.tools.registry import ToolContext, ToolEntry, ToolRegistry, format_template

logger = logging.getLogger(__name__)

# Tool rounds (model calls that produce tool calls) allowed per request before offering an agent.
MAX_TOOL_ROUNDS = 3
# Typing an answer takes longer than saying one (voice uses VOICE_PENDING_S).
TEXT_PENDING_S = 60.0
# A request that hasn't settled after this long no longer holds back the next one.
BUSY_LIMIT_S = 60.0

Notify = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class SessionState:
    """Per-connection state: the pending question and per-request counters."""

    device: str = "web"
    pending_window_s: float = TEXT_PENDING_S
    pending: PendingQuestion | None = None
    llm_calls: int = 0
    clarified: bool = False
    answering_clarify: bool = False
    # Work in flight for the current request: model calls, tool calls, a model call still to come.
    llm_busy: int = 0
    tools_busy: int = 0
    awaiting_llm: bool = False
    busy_since: float | None = None
    requests: int = 0

    def begin_request(self) -> None:
        self.requests += 1
        self.llm_calls = 0
        self.clarified = False
        self.answering_clarify = False
        self.awaiting_llm = True
        self.busy_since = time.monotonic()

    def idle(self) -> bool:
        if self.llm_busy <= 0 and self.tools_busy <= 0 and not self.awaiting_llm:
            return True
        if self.busy_since is not None and time.monotonic() - self.busy_since > BUSY_LIMIT_S:
            logger.warning("Request never settled; releasing the next one", extra={"event": "request_stuck"})
            self.llm_busy = self.tools_busy = 0
            self.awaiting_llm = False
            return True
        return False

    def set_pending(self, question: PendingQuestion) -> None:
        question.expires_at = time.monotonic() + self.pending_window_s
        self.pending = question

    def take_pending(self) -> PendingQuestion | None:
        """Return the live pending question (if any) and clear it."""
        pending, self.pending = self.pending, None
        if pending is None or pending.expired():
            return None
        return pending


class SpokenReplyEndFrame(LLMFullResponseEndFrame):
    """Ends a reply spoken from a tool, so it isn't mistaken for the end of a model call."""


async def speak_now(llm: Any, text: str) -> None:
    """Say ``text`` as an assistant reply with no model call.

    Pushed as a complete LLM response so the chat client shows it and the assistant
    aggregator records it in the conversation.
    """
    await llm.push_frame(LLMFullResponseStartFrame())
    await llm.push_frame(LLMTextFrame(text))
    await llm.push_frame(SpokenReplyEndFrame())


class ToolRuntime:
    """Wraps registry handlers as Pipecat function-call handlers for one session."""

    def __init__(
        self,
        registry: ToolRegistry,
        state: SessionState,
        notify: Notify,
        on_settled: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.registry = registry
        self.state = state
        self.notify = notify
        # Called when a tool call finishes, so a queued message can run.
        self.on_settled = on_settled

    def tools_schema(self) -> ToolsSchema:
        return ToolsSchema(standard_tools=[entry.schema(self._handler(entry)) for entry in self.registry.active])

    def _offer_default(self, summary: str) -> Awaitable[ToolOutcome]:
        offer = self.registry.get("offer_agent")
        assert offer is not None
        return offer.handler({"agent": self.registry.default_agent or "", "summary": summary}, self._context())

    def _context(self) -> ToolContext:
        return ToolContext(device=self.state.device)

    async def run(self, entry: ToolEntry, arguments: dict[str, Any]) -> tuple[ToolOutcome, str | None]:
        """Run a tool with the session's guards. Returns the outcome and the text to speak directly, if any."""
        # A guard's reply is always spoken directly: handing it back to the model would
        # start another round, which is what the guard is there to stop.
        guarded = False
        if self.state.llm_calls > MAX_TOOL_ROUNDS:
            logger.warning("Tool loop guard tripped", extra={"event": "tool_loop_guard", "tool": entry.name})
            outcome = await self._offer_default(f"request needed more than {MAX_TOOL_ROUNDS} tool rounds")
            guarded = True
        elif entry.name == "clarify" and (self.state.clarified or self.state.answering_clarify):
            # At most one clarifying question per request; then offer the default agent.
            outcome = await self._offer_default(str(arguments.get("question") or ""))
            guarded = True
        else:
            outcome = await entry.handler(arguments, self._context())

        if outcome.pending is not None:
            self.state.set_pending(outcome.pending)
            if outcome.pending.kind == "clarify":
                self.state.clarified = True

        direct: str | None = None
        must_speak = guarded or entry.speaks_directly or outcome.pending is not None or outcome.error
        if outcome.speak and must_speak:
            direct = outcome.speak
        elif entry.reply == "template":
            direct = format_template(entry.template, arguments, outcome.result) if entry.template else outcome.speak
        return outcome, direct

    def _handler(self, entry: ToolEntry):
        @tool_options(timeout_secs=entry.timeout_s)
        async def handler(params: FunctionCallParams) -> None:
            started = time.perf_counter()
            arguments = dict(params.arguments)
            outcome, direct = await self.run(entry, arguments)
            if outcome.chat:
                await self.notify({"type": "notice", "title": outcome.chat_title or entry.name, "text": outcome.chat})
            second_call = direct is None and entry.reply != "silent"
            logger.info(
                "Tool call",
                extra={
                    "event": "tool_call",
                    "tool": entry.name,
                    "kind": entry.kind,
                    "reply": entry.reply,
                    "spoken_directly": direct is not None,
                    "second_llm_call": second_call,
                    "llm_call": self.state.llm_calls,
                    "arguments": arguments,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            if second_call:
                self.state.awaiting_llm = True
                self.state.tools_busy -= 1
                await params.result_callback(outcome.result)
                return

            llm = params.llm

            async def speak() -> None:
                if direct:
                    await speak_now(llm, direct)
                self.state.tools_busy -= 1
                if self.on_settled is not None:
                    await self.on_settled()

            payload = {"result": outcome.result, "spoken": direct} if direct else outcome.result
            await params.result_callback(
                payload if payload is not None else {"ok": True},
                properties=FunctionCallResultProperties(run_llm=False, on_context_updated=speak),
            )

        return handler
