from __future__ import annotations

import logging
import re
import time
from typing import Literal

from collections import deque

from pipecat.frames.frames import (
    Frame,
    FunctionCallsStartedFrame,
    InputTransportMessageFrame,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
    OutputTransportMessageFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from victoria_voice_assistant.chat.tools import SessionState, SpokenReplyEndFrame, ToolRuntime
from victoria_voice_assistant.tools.timers import TimerService

logger = logging.getLogger(__name__)

YES = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "yes please", "do it", "go ahead",
    "sure thing", "of course", "please do", "yes do it", "ok do it", "okay do it", "sounds good", "absolutely",
}
NO = {
    "no", "nope", "nah", "no thanks", "no thank you", "don't", "do not", "never mind", "nevermind",
    "cancel", "forget it", "not now", "no need", "stop",
}
STOP = {
    "stop", "stop it", "stop the timer", "stop timer", "stop the alarm", "stop ringing", "ok stop",
    "okay stop", "dismiss", "quiet", "silence", "enough", "shut up", "turn it off", "thanks",
}


def _normalize(text: str) -> str:
    text = re.sub(r"[^\w\s']", " ", text.lower())
    text = re.sub(r"^(victoria|hey victoria)\s+", "", " ".join(text.split()))
    return text.removesuffix(" please").strip() if text not in YES else text


def match_yes_no(text: str) -> Literal["yes", "no"] | None:
    """Match a short yes/no reply without a model. Anything longer goes to the model."""
    normalized = _normalize(text)
    if normalized in YES or normalized.removesuffix(" thanks") in YES:
        return "yes"
    if normalized in NO or normalized.removesuffix(" thanks") in NO:
        return "no"
    return None


def is_stop(text: str) -> bool:
    return _normalize(text) in STOP


class TurnPreprocessor(FrameProcessor):
    """Resolves a turn before the model when it can: stopping a ringing timer and yes/no answers.

    Every other turn passes through to the model, with the pending clarify question
    (if any) noted so the request counts as its answer. Requests run one at a time:
    a message typed while the previous request is still working waits, so the model
    never answers it from a context that lacks the previous tool results.
    """

    def __init__(self, state: SessionState, runtime: ToolRuntime, timers: TimerService, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.runtime = runtime
        self.timers = timers
        self._waiting: deque[LLMMessagesAppendFrame] = deque()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, InputTransportMessageFrame):
            if isinstance(frame.message, dict) and frame.message.get("type") == "timer_stop":
                await self.timers.stop_ringing(self.state.device)
            return
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMMessagesAppendFrame) and frame.run_llm:
            text = _user_text(frame)
            if text is not None:
                if not self.state.idle():
                    self._waiting.append(frame)
                    logger.info("Message waits for the current request", extra={"event": "request_queued"})
                    return
                await self._start(frame, text)
                return
        await self.push_frame(frame, direction)

    async def release_next(self) -> None:
        """Run the next waiting message once the current request has settled."""
        while self._waiting and self.state.idle():
            frame = self._waiting.popleft()
            await self._start(frame, _user_text(frame) or "")

    async def _start(self, frame: LLMMessagesAppendFrame, text: str) -> None:
        if not await self._handled(text):
            await self.push_frame(frame)

    async def _handled(self, text: str) -> bool:
        started = time.perf_counter()
        self.state.begin_request()
        handled = await self._resolve(text, started)
        if handled:
            self.state.awaiting_llm = False
        return handled

    async def _resolve(self, text: str, started: float) -> bool:
        if self.timers.ringing(self.state.device) and is_stop(text):
            await self.timers.stop_ringing(self.state.device)
            await self._reply(text, "Okay.", "timer_stop", started)
            return True
        pending = self.state.take_pending()
        if pending is None:
            return False
        if pending.kind == "clarify":
            self.state.answering_clarify = True
            return False
        answer = match_yes_no(text)
        if answer is None:
            return False  # not a yes/no: the model handles it as a new request
        if answer == "no":
            await self._reply(text, "Okay.", "pending_no", started)
            return True
        reply = "Okay."
        if pending.on_yes is not None:
            name, arguments = pending.on_yes
            entry = self.runtime.registry.get(name)
            if entry is not None:
                outcome, direct = await self.runtime.run(entry, dict(arguments))
                reply = direct or outcome.speak or reply
        await self._reply(text, reply, "pending_yes", started)
        return True

    async def _reply(self, user_text: str, reply: str, handled_by: str, started: float) -> None:
        """Record the exchange in the conversation and send the reply, with no model call."""
        await self.push_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}],
                run_llm=False,
            )
        )
        await self.push_frame(OutputTransportMessageFrame(message={"type": "token", "text": reply}))
        await self.push_frame(OutputTransportMessageFrame(message={"type": "done"}))
        logger.info(
            "Turn handled without a model",
            extra={
                "event": "direct_reply",
                "handled_by": handled_by,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )


def _user_text(frame: LLMMessagesAppendFrame) -> str | None:
    if not frame.messages:
        return None
    message = frame.messages[-1]
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


class TurnTracker(FrameProcessor):
    """Placed right after the LLM: counts the tool calls and model replies a request is waiting on.

    When the last one finishes, the next waiting message is released.
    """

    def __init__(self, state: SessionState, preprocessor: TurnPreprocessor, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.preprocessor = preprocessor

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, FunctionCallsStartedFrame):
            self.state.tools_busy += len(frame.function_calls)
        elif isinstance(frame, LLMFullResponseEndFrame) and not isinstance(frame, SpokenReplyEndFrame):
            self.state.llm_busy -= 1
            await self.preprocessor.release_next()
