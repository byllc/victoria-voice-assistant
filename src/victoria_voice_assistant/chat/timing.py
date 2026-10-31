from __future__ import annotations

import logging
import time
from typing import Callable

from pipecat.frames.frames import Frame, LLMContextFrame, LLMFullResponseEndFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


class RequestTimer:
    """Per-connection latency clock shared by the start and end processors.

    Time to first token is measured from the moment the user message reaches the
    LLM context to the first streamed text chunk. Text-only chat has no speech
    end, so this is the closest equivalent to the voice path's end-of-speech mark.
    """

    def __init__(self, on_llm_call: Callable[[], int] | None = None) -> None:
        self.model = "unknown"
        # Index of the model call within the current request (2 = the reply after a tool result).
        self.llm_call = 0
        self._on_llm_call = on_llm_call
        self._started: float | None = None
        self._first_token_logged = False

    def begin_call(self) -> None:
        """A context frame is about to reach the model."""
        if self._on_llm_call is not None:
            self.llm_call = self._on_llm_call()
        self.start()

    def start(self) -> None:
        self._started = time.perf_counter()
        self._first_token_logged = False

    def first_token(self) -> None:
        if self._started is None or self._first_token_logged:
            return
        self._first_token_logged = True
        logger.info(
            "LLM first token",
            extra={
                "event": "llm_first_token",
                "model": self.model,
                "llm_call": self.llm_call,
                "ttft_ms": round((time.perf_counter() - self._started) * 1000, 1),
            },
        )

    def complete(self) -> None:
        if self._started is None:
            return
        logger.info(
            "LLM response complete",
            extra={
                "event": "llm_response_complete",
                "model": self.model,
                "llm_call": self.llm_call,
                "total_ms": round((time.perf_counter() - self._started) * 1000, 1),
            },
        )
        self._started = None


class TimingStartProcessor(FrameProcessor):
    """Placed before the LLM switcher: marks when a user request enters the LLM."""

    def __init__(self, timer: RequestTimer, **kwargs) -> None:
        super().__init__(**kwargs)
        self._timer = timer

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMContextFrame):
            self._timer.begin_call()
        await self.push_frame(frame, direction)


class TimingEndProcessor(FrameProcessor):
    """Placed after the LLM switcher: records first token and response end.

    The model call after a tool result is triggered by a context frame the assistant
    aggregator pushes upstream, which passes here (not the start processor) on its
    way to the model.
    """

    def __init__(self, timer: RequestTimer, **kwargs) -> None:
        super().__init__(**kwargs)
        self._timer = timer

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.UPSTREAM and isinstance(frame, LLMContextFrame):
            self._timer.begin_call()
        elif direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMTextFrame):
                self._timer.first_token()
            elif isinstance(frame, LLMFullResponseEndFrame):
                self._timer.complete()
        await self.push_frame(frame, direction)
