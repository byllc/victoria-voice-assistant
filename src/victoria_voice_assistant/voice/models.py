"""Speech models shared by every voice session, with one lock per model (architecture §4.2).

One Whisper and one Kokoro serve all devices. MLX models shouldn't be called from
several threads at once, so each model has a lock; a second room waits its turn
and the wait is logged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from collections.abc import AsyncGenerator, Callable, Iterable

from pipecat.frames.frames import Frame
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.whisper.stt import WhisperSTTServiceMLX
from pipecat.transcriptions.language import Language

from victoria_voice_assistant.config.schema import AgentsConfig, AppConfig, SpeechConfig

from victoria_voice_assistant.voice.pipecat_internals import (
    KOKORO_MODEL,
    KOKORO_VOICES,
    PromptedWhisperSTT,
    ensure_kokoro_files,
    shared_kokoro,
)

logger = logging.getLogger(__name__)

# Log a lock wait longer than this: it means two rooms wanted the same model at once.
LOCK_WAIT_LOG_MS = 50

class SpeechModels:
    """Loads the speech models once, before any voice session is accepted."""

    def __init__(self, config: SpeechConfig) -> None:
        self.config = config
        self.whisper_lock = asyncio.Lock()
        self.kokoro_lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.error: str | None = None

    async def load(self) -> None:
        """Load Whisper and Kokoro in parallel, off the event loop."""
        started = time.perf_counter()
        try:
            await asyncio.gather(
                asyncio.to_thread(self._load_whisper),
                asyncio.to_thread(self._load_kokoro),
            )
        except Exception as exc:  # noqa: BLE001 - reported through /health and the offer endpoint
            self.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Speech models failed to load", extra={"event": "speech_load_failed"})
            return
        self.ready.set()
        logger.info(
            "Speech models loaded",
            extra={
                "event": "speech_ready",
                "model": self.config.stt.model,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )

    def _load_whisper(self) -> None:
        import mlx.core as mx
        import numpy as np
        from mlx_whisper.transcribe import ModelHolder, transcribe

        ModelHolder.get_model(self.config.stt.model, mx.float16)
        # One short pass compiles the kernels so the first real request isn't slower.
        transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=self.config.stt.model, language="en")

    def _load_kokoro(self) -> None:
        ensure_kokoro_files()
        kokoro = shared_kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        if self.config.tts.voice not in kokoro.voices:
            raise ValueError(f"Kokoro voice {self.config.tts.voice!r} is not in {KOKORO_VOICES}")
        kokoro.create(".", voice=self.config.tts.voice, lang="en-us")

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """Speech for `text` outside any voice session (an announcement to several rooms, synthesized
        once): 16-bit mono audio and its sample rate, under the same lock the sessions use."""
        import numpy as np

        kokoro = shared_kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        await _acquire(self.kokoro_lock, "kokoro")
        try:
            samples, rate = await asyncio.to_thread(
                kokoro.create, text, voice=self.config.tts.voice, speed=self.config.tts.speed, lang="en-us"
            )
        finally:
            self.kokoro_lock.release()
        return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes(), rate

    def stt(self, prompt: Prompt = None) -> LockedWhisperSTT:
        return LockedWhisperSTT(
            self.whisper_lock,
            prompt=prompt,
            settings=WhisperSTTServiceMLX.Settings(
                model=self.config.stt.model,
                language=Language(self.config.stt.language),
            ),
        )

    def tts(self) -> LockedKokoroTTS:
        return LockedKokoroTTS(
            self.kokoro_lock,
            model_path=str(KOKORO_MODEL),
            voices_path=str(KOKORO_VOICES),
            settings=KokoroTTSService.Settings(voice=self.config.tts.voice, speed=self.config.tts.speed),
        )


async def _acquire(lock: asyncio.Lock, model: str) -> None:
    started = time.perf_counter()
    await lock.acquire()
    waited = (time.perf_counter() - started) * 1000
    if waited > LOCK_WAIT_LOG_MS:
        logger.info("Waited for a speech model", extra={"event": "model_lock_wait", "model": model, "duration_ms": round(waited, 1)})


def whisper_prompt(agents: AgentsConfig, words: Iterable[str] = ()) -> str | None:
    """Whisper's initial prompt: the agent names, so "ask Hermes" isn't heard as "ask Herman"
    (architecture §5), then `words` it should know, such as the cellar's grapes and producers.

    Only the agents' names: aliases are the mis-hearings, not what we want written.
    """
    names = [agent.name[:1].upper() + agent.name[1:] for agent in agents.agents]
    parts = [f"Victoria. Ask {', ask '.join(names)}."] if names else []
    words = [word for word in words if word]
    if words:
        parts.append(", ".join(words) + ".")
    return " ".join(parts) or None


def session_prompt(config: AppConfig, registry: Any, services: Any) -> Prompt:
    """Whisper's prompt for a voice session: the agents' names, plus the cellar's words while the
    CellarTracker tool is on (feature 20261114-162903), asked again before each transcription."""
    words = getattr(services, "wine_words", None)
    tool = next((tool for tool in config.tools.tools if tool.kind == "builtin" and tool.service == "cellartracker"), None)
    entry = registry.get(tool.name) if tool is not None else None
    if words is None or entry is None or entry.status != "ready":
        return whisper_prompt(config.agents)
    return lambda: whisper_prompt(config.agents, words())


def is_prompt_echo(text: str, prompt: str | None) -> bool:
    """Whether a transcript is only Whisper's own prompt said back.

    On near-silence Whisper can return the initial prompt, often repeated ("Ask Claude, ask Claude, …").
    That's the case when the text is nothing but the prompt's phrases, with one of them repeated or
    three or more in all. "Ask Claude" or "Victoria, ask Claude" alone is left: someone may have said it.
    """
    if not prompt:
        return False

    def phrases(value: str) -> list[tuple[str, ...]]:
        return [tuple(part.split()) for part in re.split(r"[^\w\s'-]+", value.lower()) if part.split()]

    known = sorted(set(phrases(prompt)), key=len, reverse=True)
    words = [word for phrase in phrases(text) for word in phrase]
    found: list[tuple[str, ...]] = []
    at = 0
    while at < len(words):
        match = next((phrase for phrase in known if tuple(words[at : at + len(phrase)]) == phrase), None)
        if match is None:
            return False
        at += len(match)
        found.append(match)
    return len(found) >= 3 or len(set(found)) < len(found)


Prompt = str | Callable[[], str | None] | None


class LockedWhisperSTT(PromptedWhisperSTT):
    """`prompt` may be a function, asked before each transcription, so a long-lived session (a
    satellite's) picks up new words, such as a refreshed wine list, without reconnecting."""

    def __init__(self, lock: asyncio.Lock, prompt: Prompt = None, **kwargs) -> None:
        self._prompt_source = prompt if callable(prompt) else None
        super().__init__(prompt=None if callable(prompt) else prompt, drop=is_prompt_echo, **kwargs)
        self._model_lock = lock

    def _refresh_prompt(self) -> None:
        if self._prompt_source is None:
            return
        try:
            self.prompt = self._prompt_source()
        except Exception:  # noqa: BLE001 - a broken word list mustn't stop speech recognition
            logger.warning("Couldn't build Whisper's prompt", exc_info=True, extra={"event": "stt_prompt_failed"})

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        await _acquire(self._model_lock, "whisper")
        try:
            self._refresh_prompt()
            stt = self.run_stt_with_prompt(audio) if self.prompt else WhisperSTTServiceMLX.run_stt(self, audio)
            frames = [frame async for frame in stt]
        finally:
            self._model_lock.release()
        for frame in frames:
            yield frame


class LockedKokoroTTS(KokoroTTSService):
    def __init__(self, lock: asyncio.Lock, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model_lock = lock

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        # Held per sentence, not per reply, so two rooms speaking at once interleave. The
        # sentence is synthesized under the lock and yielded after, so a session cancelled
        # mid-reply can't leave the lock held. Kokoro renders a short sentence in one
        # batch anyway, so this doesn't delay the first audio.
        await _acquire(self._model_lock, "kokoro")
        try:
            frames = [frame async for frame in super().run_tts(text, context_id)]
        finally:
            self._model_lock.release()
        for frame in frames:
            yield frame
