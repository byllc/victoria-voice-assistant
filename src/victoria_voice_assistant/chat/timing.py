from __future__ import annotations

import logging
import time

from pipecat.frames.frames import Frame, LLMContextFrame, LLMFullResponseEndFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


class RequestTimer:
    """Per-connection latency clock shared by the start and end processors.

    Time to first token is measured from the moment the user message reaches the
    LLM context to the first streamed text chunk. Text-only chat has no speech
    end, so this is the closest equivalent to the voice path's end-of-speech mark.
    """

    def __init__(self) -> None:
        self.model = "unknown"
        self._started: float | None = None
        self._first_token_logged = False

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
                "total_ms": round((time.perf_counter() - self._started) * 1000, 1),
            },
        )
        self._started = None


class TimingStartProcessor(FrameProcessor):
    """Placed before the LLM switcher: marks when a request enters the LLM."""

    def __init__(self, timer: RequestTimer, **kwargs) -> None:
        super().__init__(**kwargs)
        self._timer = timer

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMContextFrame):
            self._timer.start()
        await self.push_frame(frame, direction)


class TimingEndProcessor(FrameProcessor):
    """Placed after the LLM switcher: records first token and response end."""

    def __init__(self, timer: RequestTimer, **kwargs) -> None:
        super().__init__(**kwargs)
        self._timer = timer

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMTextFrame):
                self._timer.first_token()
            elif isinstance(frame, LLMFullResponseEndFrame):
                self._timer.complete()
        await self.push_frame(frame, direction)
