"""Turning an agent's written reply into something to say (architecture §7 roles: `summarizer`)."""

from __future__ import annotations

import logging
import re
import time

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import LLMService

logger = logging.getLogger(__name__)

# Replies longer than this (after Markdown is removed) are shortened before they're spoken.
SPOKEN_MAX_CHARS = 280
SUMMARIZE_PROMPT = (
    "Rewrite this answer to be spoken aloud by a home voice assistant: at most two short sentences, "
    "plain words, no lists, no formatting, no preamble. Keep the most useful facts."
)


def plain_text(text: str) -> str:
    """Remove Markdown so TTS doesn't read out asterisks and hashes; list items become sentences."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # links keep their words
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)  # headings
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", line)  # list markers
        line = re.sub(r"^\s*>\s?", "", line)  # quotes
        line = re.sub(r"(?<![\w*~])(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1(?![\w*~])", r"\2", line)  # emphasis
        line = line.strip()
        if not line or re.fullmatch(r"[-*_=|: ]{3,}", line):
            continue
        if line[-1] not in ".!?:;,":
            line += "."
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def first_sentences(text: str, count: int = 2, max_chars: int = SPOKEN_MAX_CHARS) -> str:
    """Up to `count` sentences that fit in `max_chars` (always at least the first)."""
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if kept and (len(kept) >= count or len(" ".join([*kept, sentence])) > max_chars):
            break
        kept.append(sentence)
    return " ".join(kept).strip()


async def spoken_version(text: str, summarizer: LLMService | None) -> str:
    """Plain text, shortened by the summarizer model when it's too long to say; the first sentences if that fails."""
    plain = plain_text(text)
    if len(plain) <= SPOKEN_MAX_CHARS:
        return plain
    started = time.perf_counter()
    short = None
    if summarizer is not None:
        try:
            context = LLMContext(messages=[{"role": "user", "content": plain}])
            short = await summarizer.run_inference(context, system_instruction=SUMMARIZE_PROMPT, max_tokens=120)
        except Exception as exc:  # noqa: BLE001 - fall back to the first sentences
            logger.warning("Summarizer failed", extra={"event": "summarize_failed", "reason": str(exc)})
    spoken = plain_text(short) if short else first_sentences(plain)
    logger.info(
        "Agent reply shortened for speech",
        extra={
            "event": "agent_reply_shortened",
            "kind": "summarizer" if short else "first_sentences",
            "count": len(plain),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return spoken
