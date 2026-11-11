"""Which satellite answers when several hear the same words (architecture §4, Stage 13).

When a satellite wakes, the arbiter opens a window (`satellite_arbitration_s`, about 0.5 s) for
others waking too. Each turn's audio is held meanwhile, not transcribed. One turn then goes ahead:
a satellite with an open question or a ringing timer, else the one that hears the speech loudest
(plus its `arbitration_offset_db`, for microphones that read loud or quiet), with near-ties going
to the first to wake. The others end quietly. Each satellite is scored on the same amount of audio,
its own first window after it woke, so the decision comes one window after the last of them woke
(Bill, 2026-11-11: a late waker used to be scored on a sliver). The speaker is usually still
talking then, so it rarely adds a wait to the reply. With one satellite configured nothing is held.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)

TIE_DB = 1.5  # scores this close count as a tie: the first to wake wins
FRAME_SAMPLES = 480  # 30 ms at 16 kHz


class Candidate(Protocol):
    name: str

    def arbitration_score(self) -> tuple[bool, float]:
        """(has priority: an open question or a ringing timer, how loud its speech is in dB)."""
        ...


def frame_levels(audio: bytes) -> tuple[float, float, float] | None:
    """The loud and quiet 30 ms frame levels of `audio` (90th and 10th percentile, raw 16-bit RMS)
    and how many seconds it holds. None when there is too little to tell."""
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    frames = samples.size // FRAME_SAMPLES
    if frames < 2:
        return None
    rms = np.sqrt(np.mean(samples[: frames * FRAME_SAMPLES].reshape(frames, FRAME_SAMPLES) ** 2, axis=1))
    return float(np.percentile(rms, 90)), float(np.percentile(rms, 10)), frames * FRAME_SAMPLES / 16000


def _dbfs(level: float) -> float:
    return float(20 * np.log10((level + 1.0) / 32768.0))


def speech_level_db(audio: bytes) -> float:
    """How loud the speech in `audio` is, in dB below full scale: the 90th percentile of its 30 ms
    frame levels. Distance lowers it. (It was speech against the quiet frames until 2026-11-11: a
    board with a low noise floor then won from across the room and couldn't make out the words.)"""
    levels = frame_levels(audio)
    return _dbfs(0.0) if levels is None else _dbfs(levels[0])


def describe_levels(audio: bytes, offset_db: float = 0.0) -> str:
    """For the decision log, what a score was made of: the loud and quiet levels (dB below full
    scale), how much audio was scored, and the satellite's offset. Used to tune `arbitration_offset_db`."""
    levels = frame_levels(audio)
    if levels is None:
        return "no audio"
    loud, quiet, seconds = levels
    return f"loud {_dbfs(loud):.1f}, quiet {_dbfs(quiet):.1f} dBFS, {seconds:.2f} s, offset {offset_db:+.1f}"


@dataclass
class _Group:
    started: float
    entries: list[tuple[Candidate, float, asyncio.Future[bool]]] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None  # the decision: one window after the last to wake


class Arbiter:
    def __init__(self, window_s: float = 0.5, clock: Callable[[], float] = time.monotonic) -> None:
        self.window_s = window_s
        self.satellites = 1  # how many are configured; with one, every turn goes ahead at once
        self._clock = clock
        self._group: _Group | None = None

    async def join(self, candidate: Candidate) -> bool:
        """Wait for the decision on a turn that just started. True: it goes ahead."""
        if self.satellites <= 1 or self.window_s <= 0:
            return True
        loop = asyncio.get_running_loop()
        now = self._clock()
        if self._group is None or now - self._group.started > self.window_s:
            self._group = _Group(started=now)
        group = self._group
        if group.timer is not None:
            group.timer.cancel()
        group.timer = loop.call_later(self.window_s, self._decide, group)  # this one gets its full window too
        future: asyncio.Future[bool] = loop.create_future()
        group.entries.append((candidate, now, future))
        return await future

    def _decide(self, group: _Group) -> None:
        if self._group is group:
            self._group = None
        scored = [(candidate, started, future, *candidate.arbitration_score()) for candidate, started, future in group.entries]
        if not scored:
            return
        pool = [entry for entry in scored if entry[3]] or scored  # priority first
        best = max(entry[4] for entry in pool)
        winner = min((entry for entry in pool if entry[4] >= best - TIE_DB), key=lambda entry: entry[1])
        for candidate, _started, future, _priority, _score in scored:
            if not future.done():
                future.set_result(candidate is winner[0])
        if len(scored) > 1:
            first = min(started for _c, started, _f, _p, _score in scored)
            summary = ", ".join(
                f"{c.name} {score:.1f} dB{' (priority)' if priority else ''}{' (answers)' if c is winner[0] else ''}"
                f" [woke +{started - first:.2f} s{_levels(c)}]"
                for c, started, _f, priority, score in scored
            )
            logger.info("Satellite chosen", extra={"event": "satellite_arbitration", "device": winner[0].name, "text": summary})


def _levels(candidate: Candidate) -> str:
    describe = getattr(candidate, "arbitration_levels", None)
    return f"; {describe()}" if describe is not None else ""
