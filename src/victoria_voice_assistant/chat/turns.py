from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from collections import deque

from pipecat.frames.frames import (
    Frame,
    FunctionCallsStartedFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    OutputTransportMessageFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from victoria_voice_assistant.chat.tools import SessionState, SpokenReplyEndFrame, ToolRuntime, forget_stale
from victoria_voice_assistant.agents.runner import AgentRunner, match_agent_address
from victoria_voice_assistant.notifications.service import NotificationService
from victoria_voice_assistant.tools.music import MusicService
from victoria_voice_assistant.tools.timers import TimerService

logger = logging.getLogger(__name__)

EMPTY_REPLY = "Sorry, I didn't get that. Could you say it another way?"
# Control tokens the local model sometimes leaks into its text, like "<|channel>::" (gemma-4-12b).
CONTROL_TOKENS = re.compile(r"<\|?/?[a-z_\"]+\|?>:*")  # also its quote token, <|"|>
ROLE_NOISE = re.compile(r"[\s:]*(?:(?:user|model|assistant)[\s:]*)*")
WRITTEN_CALL = re.compile(r"call:(\w+)\{(.*)\}", re.DOTALL)
MIN_LETTERS = 8  # a reply's opening is held until it has this many letters of real text

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

    def __init__(
        self,
        state: SessionState,
        runtime: ToolRuntime,
        timers: TimerService,
        agents: AgentRunner | None = None,
        notifications: NotificationService | None = None,
        music: MusicService | None = None,
        context: LLMContext | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.context = context
        self.state = state
        self.runtime = runtime
        self.timers = timers
        self.agents = agents
        self.notifications = notifications
        self.music = music
        self._waiting: deque[LLMMessagesAppendFrame] = deque()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, InputTransportMessageFrame):
            message = frame.message if isinstance(frame.message, dict) else {}
            if message.get("type") == "timer_stop":
                await self.timers.stop_ringing(self.state.device)
            elif message.get("type") == "notification_ack" and self.notifications is not None:
                # Opened or dismissed on the screen.
                unread = self.notifications.unread(self.state.device)
                wanted = message.get("ids")
                self.notifications.acknowledge(unread if wanted == "all" else [i for i in unread if i.id in (wanted or [])])
            elif message.get("type") == "hello":
                self.state.client_id = message["client_id"]
            elif message.get("type") == "music_speaker" and self.music is not None and self.state.client_id:
                # A page with the music player can be asked to turn its speaker on ("music_start").
                send = self._send_message if message.get("can_play") else None
                self.music.set_speaker(self.state.client_id, message["player_id"], send)
            elif message.get("type") == "push_subscribe" and self.notifications is not None and self.notifications.push:
                saved = self.state.client_id is not None and self.notifications.push.subscribe(
                    self.state.client_id, self.state.device, message["subscription"]
                )
                if not saved:
                    await self.push_frame(OutputTransportMessageFrame(message={"type": "push_config", "public_key": None, "problem": "subscription rejected"}))
            elif message.get("type") == "push_unsubscribe" and self.notifications is not None and self.notifications.push:
                self.notifications.push.unsubscribe(message["endpoint"])
            elif message.get("type") == "task_action" and self.agents is not None:
                await self._task_action(message)
            elif message.get("type") == "agent_answer" and self.agents is not None:
                # Answered from the notification on the screen.
                outcome = self.agents.answer(str(message.get("task_id") or ""), str(message.get("text") or ""))
                await self._deliver_note(outcome.speak or "Okay.")
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

    async def _send_message(self, message: dict) -> None:
        await self.push_frame(OutputTransportMessageFrame(message=message))

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
        if self.runtime is not None and self.runtime.history is not None:
            self.runtime.history.begin(text)
        open_question = self.state.pending is not None and not self.state.pending.expired()
        if self.context is not None and self.runtime is not None and self.runtime.forget and not open_question:
            # Before the model sees its old time or weather. Not while a tool's question is open: the answer
            # ("the laundry one") means nothing without "Which timer: pasta or laundry?" (2026-11-11).
            forget_stale(self.context, self.runtime.forget)
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
            stray = match_yes_no(text)
            if stray is not None and not self._last_reply_asked():
                # Nothing was asked: a yes or no on its own (often heard by mistake) gets a short answer.
                reply = "Okay." if stray == "no" else "There's nothing waiting for a yes. What would you like?"
                await self._reply(text, reply, "stray_yes_no", started)
                return True
            return await self._agent_address(text, started)
        if pending.kind == "clarify":
            self.state.answering_clarify = True
            return await self._agent_address(text, started)
        if pending.kind == "answer" and self.agents is not None and pending.task_id:
            outcome = self.agents.answer(pending.task_id, text)
            if outcome.result and "answered" in outcome.result:
                await self._reply(text, outcome.speak or "Okay.", "agent_answer", started)
                return True
            return await self._agent_address(text, started)  # the question closed: an ordinary request
        answer = match_yes_no(text)
        if answer is None:
            # not a yes/no: a new request, for an agent by name or for the model
            return await self._agent_address(text, started)
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

    async def _agent_address(self, text: str, started: float) -> bool:
        """ "Ask Helper to ..." goes straight to that agent, with no model call (architecture §5 step 4)."""
        entry = self.runtime.registry.get("ask_agent")
        if self.agents is None or entry is None:
            return False
        match = match_agent_address(text, self.agents.config.agents.agents)
        if match is None:
            return False
        agent, request = match
        outcome, direct = await self.runtime.run(entry, {"agent": agent.name, "task": request})
        await self._reply(text, direct or outcome.speak or "Okay.", "agent_address", started)
        return True

    async def _deliver(self, user_text: str, reply: str) -> None:
        """Record the exchange in the conversation and send the reply to the chat client."""
        await self.push_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}],
                run_llm=False,
            )
        )
        await self.push_frame(OutputTransportMessageFrame(message={"type": "token", "text": reply}))
        await self.push_frame(OutputTransportMessageFrame(message={"type": "done"}))

    async def _task_action(self, message: dict) -> None:
        """The dashboard's buttons: cancel, retry, re-send (another agent or a stronger model), open."""
        assert self.agents is not None
        task_id, action = message["task_id"], message["action"]
        task = self.agents.tasks.get(task_id)
        if task is None:
            return
        if action == "cancel":
            stopped = self.agents.tasks.cancel(task_id)
            await self._deliver_note(f"Cancelled {task.agent.title()}'s task." if stopped else "That task isn't running.")
        elif action in ("retry", "resend"):
            outcome = await self.agents.retry(task_id, agent=message.get("agent"), model=message.get("model"))
            await self._deliver_note(outcome.speak or "Okay.")
            if outcome.chat and self.runtime is not None:  # setup steps for an agent that isn't set up
                await self.runtime.notify({"type": "notice", "title": outcome.chat_title or "Note", "text": outcome.chat})
        elif action == "open" and self.notifications is not None:
            # The full result is on the screen now: its notification counts as read.
            self.notifications.acknowledge([i for i in self.notifications.unread() if i.task_id == task_id])

    async def _deliver_note(self, reply: str) -> None:
        """A reply to something done on the screen (no user turn to pair it with)."""
        await self.push_frame(OutputTransportMessageFrame(message={"type": "token", "text": reply}))
        await self.push_frame(OutputTransportMessageFrame(message={"type": "done"}))

    def _last_reply_asked(self) -> bool:
        """Whether the assistant's last reply asked something (a yes there may answer it in plain text)."""
        if self.context is None:
            return True  # can't tell: let the model decide
        for message in reversed(self.context.get_messages()):
            if isinstance(message, dict) and message.get("role") == "assistant":
                content = message.get("content")
                return isinstance(content, str) and content.rstrip().endswith("?")
        return False

    async def _reply(self, user_text: str, reply: str, handled_by: str, started: float) -> None:
        """Answer with no model call."""
        if self.runtime is not None and self.runtime.history is not None:
            self.runtime.history.reply(reply)
        await self._deliver(user_text, reply)
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

    When the last one finishes, the next waiting message is released. It also guards what the local
    model says (Stage 11): the start of each reply is held until it has a few real words, with leaked
    control tokens ("<|channel>::", "::user<channel|>user") removed, and a reply of nothing but those
    (or nothing at all) is asked for once more with `retry` (the local model sometimes pads its
    whole reply with blank lines: Bill, 2026-11-02), and only then becomes EMPTY_REPLY rather than
    silence or garbage.
    """

    def __init__(
        self, state: SessionState, preprocessor: TurnPreprocessor, retry: Callable[[], Awaitable[None]] | None = None, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.preprocessor = preprocessor
        self.retry = retry  # runs the model again on the same conversation
        self._held: list[str] | None = None  # the reply's opening text, until it has real words
        self._said_something = True
        self._retried = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseStartFrame):
            self._held, self._said_something = [], False
        elif isinstance(frame, LLMTextFrame):
            if self._held is not None:
                self._held.append(frame.text)
                opening = clean_reply("".join(self._held))
                # A tool call written out as text ("call:clarify{...}") is held whole, to be read at the end.
                if not opening.startswith("call:") and len(re.findall(r"[A-Za-z]", opening)) >= MIN_LETTERS:
                    await self._release(frame)
                return
            if "<" in frame.text:
                frame.text = CONTROL_TOKENS.sub("", frame.text)
        elif isinstance(frame, InterruptionFrame):
            self._held, self._said_something = None, True  # cut off by the user, not empty
            self._retried = False
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._held, self._said_something = None, True  # a tool call: whatever came before it was noise
            self.state.tools_busy += len(frame.function_calls)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._held is not None:
                await self._release(None)
            if not self._said_something and self.retry is not None and not self._retried:
                logger.warning("Model gave an empty reply; asking again", extra={"event": "empty_reply_retry"})
                self._retried = self._said_something = True
                await self.push_frame(frame, direction)
                if not isinstance(frame, SpokenReplyEndFrame):
                    # The retry goes through the preprocessor as a new request, so it must find the session
                    # idle (or it would queue behind itself). Queued commands still wait: nothing releases
                    # them until the retry's own reply ends.
                    self.state.llm_busy -= 1
                await self.retry()
                return
            if not self._said_something:
                logger.warning("Model gave an empty reply", extra={"event": "empty_reply"})
                await self.push_frame(LLMTextFrame(EMPTY_REPLY))
                self._said_something = True
            self._retried = False
            await self.push_frame(frame, direction)
            if not isinstance(frame, SpokenReplyEndFrame):
                self.state.llm_busy -= 1
                await self.preprocessor.release_next()
            return
        await self.push_frame(frame, direction)

    async def _release(self, frame: LLMTextFrame | None) -> None:
        text = clean_reply("".join(self._held or []))
        self._held = None
        if text:
            self._said_something = True
            if frame is not None:
                frame.text = text
                await self.push_frame(frame)
            else:
                await self.push_frame(LLMTextFrame(text))


def clean_reply(text: str) -> str:
    """The reply without leaked control tokens, stray colons, or bare role names ("user")."""
    text = CONTROL_TOKENS.sub("", text)
    if ROLE_NOISE.fullmatch(text):
        return ""
    text = text.lstrip(": ")
    written_call = WRITTEN_CALL.fullmatch(text.strip())
    if written_call:
        # The model wrote its tool call as text instead of making it. A clarifying question can still be
        # asked; anything else can't be run from here, so it counts as no reply.
        name, body = written_call.groups()
        question = re.search(r"question\s*:\s*(.+?)\s*$", body)
        return question.group(1).strip().strip('"') if name == "clarify" and question else ""
    return text

class HistoryTap(FrameProcessor):
    """Placed after the turn tracker: saves each reply the model (or a tool) gives to the command history.

    Replies answered without a model are saved by the preprocessor, which sends them another way.
    """

    def __init__(self, runtime: ToolRuntime, **kwargs) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        self._text: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMTextFrame):
                self._text.append(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                text, self._text = "".join(self._text).strip(), []
                if text and self.runtime.history is not None:
                    self.runtime.history.reply(text)
        await self.push_frame(frame, direction)
