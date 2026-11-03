"""Which satellite answers when several hear the same words (architecture §4, Stage 13).

When a satellite wakes, the arbiter opens a window (`satellite_arbitration_s`, about 0.5 s) for
others waking too. Each turn's audio is held meanwhile, not transcribed. At the window's end one turn
goes ahead: a satellite with an open question or a ringing timer, else the one whose audio after the
wake word stands highest above its own background noise (plus its `arbitration_offset_db`), with
near-ties going to the first to wake. The others end quietly. The window passes while the user is
still speaking, so it adds no wait to the reply. With one satellite configured nothing is held.
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
        """(has priority: an open question or a ringing timer, speech level above noise in dB)."""
        ...


def speech_over_noise_db(audio: bytes) -> float:
    """How far the loud parts of `audio` stand above its quiet parts, in dB: 90th against 10th
    percentile of 30 ms frame levels. Distance lowers it; a satellite's own background sets the floor."""
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    frames = samples.size // FRAME_SAMPLES
    if frames < 2:
        return 0.0
    rms = np.sqrt(np.mean(samples[: frames * FRAME_SAMPLES].reshape(frames, FRAME_SAMPLES) ** 2, axis=1))
    loud, quiet = np.percentile(rms, 90), np.percentile(rms, 10)
    return float(20 * np.log10((loud + 1.0) / (quiet + 1.0)))


@dataclass
class _Group:
    started: float
    entries: list[tuple[Candidate, float, asyncio.Future[bool]]] = field(default_factory=list)


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
            group = _Group(started=now)
            self._group = group
            loop.call_later(self.window_s, self._decide, group)
        future: asyncio.Future[bool] = loop.create_future()
        self._group.entries.append((candidate, now, future))
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
            summary = ", ".join(
                f"{c.name} {score:.1f} dB{' (priority)' if priority else ''}{' (answers)' if c is winner[0] else ''}"
                for c, _s, _f, priority, score in scored
            )
            logger.info("Satellite chosen", extra={"event": "satellite_arbitration", "device": winner[0].name, "text": summary})
