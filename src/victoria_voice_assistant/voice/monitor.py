from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

TICK_S = 0.05
# A stall this long on the event loop is audible as stutter on every voice stream.
STALL_MS = 20.0


async def resident_mb() -> float | None:
    """This process's resident memory, read with `ps` (no extra dependency)."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ps", "-o", "rss=", "-p", str(os.getpid()), stdout=asyncio.subprocess.PIPE
        )
        output, _ = await process.communicate()
        return round(int(output.strip()) / 1024, 1)
    except (OSError, ValueError):
        return None


async def monitor_loop_lag(tick_s: float = TICK_S, stall_ms: float = STALL_MS) -> None:
    """Log every event-loop stall over ``stall_ms``, plus each minute the worst lag and memory.

    Architecture §4.2: one blocked loop stutters every stream; a leak in one model
    grows the whole process."""
    worst = 0.0
    window_started = time.perf_counter()
    while True:
        expected = time.perf_counter() + tick_s
        await asyncio.sleep(tick_s)
        lag_ms = max(0.0, (time.perf_counter() - expected) * 1000)
        worst = max(worst, lag_ms)
        if lag_ms > stall_ms:
            logger.warning("Event loop stalled", extra={"event": "loop_stall", "duration_ms": round(lag_ms, 1)})
        if time.perf_counter() - window_started >= 60:
            logger.info(
                "Event loop lag and memory",
                extra={"event": "loop_lag", "duration_ms": round(worst, 1), "rss_mb": await resident_mb()},
            )
            worst = 0.0
            window_started = time.perf_counter()
