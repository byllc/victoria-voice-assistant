from __future__ import annotations

import json
import logging

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    OutputTransportMessageFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.base_serializer import FrameSerializer

logger = logging.getLogger(__name__)


class ChatProtocolProcessor(FrameProcessor):
    """Mirror reply frames as transport messages.

    The websocket output transport only serializes transport-message frames, so text
    and end-of-reply frames are converted here. The original frames keep flowing so the
    assistant aggregator still records the reply in the conversation context.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            message = None
            if isinstance(frame, LLMTextFrame):
                message = {"type": "token", "text": frame.text}
            elif isinstance(frame, LLMFullResponseEndFrame):
                message = {"type": "done"}
            elif isinstance(frame, ErrorFrame):
                message = {"type": "error", "message": frame.error}
            if message is not None:
                await self.push_frame(OutputTransportMessageFrame(message=message), direction)
        await self.push_frame(frame, direction)


class ChatJsonSerializer(FrameSerializer):
    """Text chat protocol.

    Client -> server: ``{"type": "user_message", "text": "..."}``
    Server -> client: ``{"type": "token", "text": "..."}`` per streamed chunk,
    ``{"type": "done"}`` when a reply ends, ``{"type": "error", "message": "..."}``
    on failure.
    """

    async def serialize(self, frame: Frame) -> str | None:
        if isinstance(frame, OutputTransportMessageFrame):
            return json.dumps(frame.message)
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        try:
            message = json.loads(data)
        except (TypeError, ValueError):
            logger.warning("Ignoring non-JSON chat message", extra={"event": "chat_message_invalid"})
            return None
        if not isinstance(message, dict) or message.get("type") != "user_message":
            logger.warning("Ignoring unknown chat message", extra={"event": "chat_message_invalid"})
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return LLMMessagesAppendFrame(
            messages=[{"role": "user", "content": text.strip()}],
            run_llm=True,
        )
