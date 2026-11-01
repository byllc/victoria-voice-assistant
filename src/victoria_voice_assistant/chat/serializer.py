from __future__ import annotations

import json
import logging

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
    InputTransportMessageFrame,
    LLMTextFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
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

    Client -> server: ``{"type": "user_message", "text": "..."}``, ``{"type": "timer_stop"}``,
    ``{"type": "notification_ack", "ids": [...] | "all"}``, ``{"type": "agent_answer", "task_id": "...", "text": "..."}``,
    ``{"type": "task_action", "action": "cancel" | "retry" | "resend" | "open", "task_id": "...", "agent"?, "model"?}``,
    ``{"type": "hello", "client_id": "..."}`` (the browser's id), ``{"type": "music_speaker", "player_id": "..." | null, "can_play": bool}``
    (this browser's Sendspin speaker, and whether it has a player that can turn it on), ``{"type": "push_subscribe", "subscription": {...}}``
    and ``{"type": "push_unsubscribe", "endpoint": "..."}``.
    Server -> client: ``{"type": "token", "text": "..."}`` per streamed chunk,
    ``{"type": "done"}`` when a reply ends, ``{"type": "error", "message": "..."}``
    on failure, ``{"type": "notice", "title": "...", "text": "..."}`` for text shown
    in chat only (stub setup steps), ``{"type": "timers", "timers": [...]}`` when
    timers change, ``{"type": "timer_ring", "names": [...], "text": "..."}`` and
    ``{"type": "timer_ring_stop", "names": [...]}``, ``{"type": "notifications", "items": [...]}`` (the unread
    list, for the badge), ``{"type": "announce", "text": "...", "sound": bool, "spoken": bool, "items": [...]}`` and
    ``{"type": "dashboard", "tasks": [...], "summary": {...}}`` (the task dashboard, on every change) and
    ``{"type": "push_config", "public_key": "..." | null, "problem": "..." | null}`` (whether phone push is available) and
    ``{"type": "music_start", "title": "...", "artist": "..." | null, "kind": "..."}`` (music asked for here: turn this
    browser's speaker on if it's off, and show what's starting) and ``{"type": "music_error", "text": "..."}``.
    """

    async def serialize(self, frame: Frame) -> str | None:
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            return json.dumps(frame.message)
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        try:
            message = json.loads(data)
        except (TypeError, ValueError):
            logger.warning("Ignoring non-JSON chat message", extra={"event": "chat_message_invalid"})
            return None
        if isinstance(message, dict) and message.get("type") == "timer_stop":
            return InputTransportMessageFrame(message={"type": "timer_stop"})
        if isinstance(message, dict) and message.get("type") == "notification_ack":
            ids = message.get("ids")
            if ids == "all" or (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                return InputTransportMessageFrame(message={"type": "notification_ack", "ids": ids})
        if isinstance(message, dict) and message.get("type") == "hello" and isinstance(message.get("client_id"), str):
            return InputTransportMessageFrame(message={"type": "hello", "client_id": message["client_id"][:64]})
        if isinstance(message, dict) and message.get("type") == "music_speaker":
            player_id = message.get("player_id")
            if player_id is None or isinstance(player_id, str):
                speaker = {"type": "music_speaker", "player_id": (player_id or "")[:64] or None, "can_play": message.get("can_play") is True}
                return InputTransportMessageFrame(message=speaker)
        if isinstance(message, dict) and message.get("type") == "push_subscribe" and isinstance(message.get("subscription"), dict):
            return InputTransportMessageFrame(message={"type": "push_subscribe", "subscription": message["subscription"]})
        if isinstance(message, dict) and message.get("type") == "push_unsubscribe" and isinstance(message.get("endpoint"), str):
            return InputTransportMessageFrame(message={"type": "push_unsubscribe", "endpoint": message["endpoint"]})
        if isinstance(message, dict) and message.get("type") == "task_action":
            action, task_id = message.get("action"), message.get("task_id")
            if action in ("cancel", "retry", "resend", "open") and isinstance(task_id, str):
                extra = {k: message[k] for k in ("agent", "model") if isinstance(message.get(k), str)}
                return InputTransportMessageFrame(message={"type": "task_action", "action": action, "task_id": task_id, **extra})
        if isinstance(message, dict) and message.get("type") == "agent_answer":
            task_id, text = message.get("task_id"), message.get("text")
            if isinstance(task_id, str) and isinstance(text, str) and text.strip():
                return InputTransportMessageFrame(message={"type": "agent_answer", "task_id": task_id, "text": text.strip()})
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
