from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from aioesphomeapi.model import VoiceAssistantEventType as E

from victoria_voice_assistant.satellite.arbiter import Arbiter, describe_levels, speech_level_db


@dataclass
class Fake:
    name: str
    score: float
    priority: bool = False

    def arbitration_score(self) -> tuple[bool, float]:
        return self.priority, self.score


def _voice(level: float, noise: float, seconds: float = 0.5, seed: int = 1) -> bytes:
    """A burst of 'speech' (a tone at `level`) in the middle of background noise at `noise`."""
    rng = np.random.default_rng(seed)
    n = int(seconds * 16000)
    samples = rng.normal(0, noise, n)
    t = np.arange(n // 2) / 16000
    samples[n // 4 : n // 4 + n // 2] += level * np.sin(2 * np.pi * 220 * t)
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def _decide(candidates: list[Fake], gaps: list[float] | None = None, window: float = 0.05, satellites: int = 3) -> list[bool]:
    async def scenario() -> list[bool]:
        arbiter = Arbiter(window)
        arbiter.satellites = satellites
        joins = []
        for candidate, gap in zip(candidates, gaps or [0.0] * len(candidates), strict=True):
            await asyncio.sleep(gap)
            joins.append(asyncio.create_task(arbiter.join(candidate)))
        return list(await asyncio.gather(*joins))

    return asyncio.run(scenario())


def test_the_clearest_satellite_answers() -> None:
    assert _decide([Fake("kitchen", 8.0), Fake("office", 21.0), Fake("hall", 3.0)]) == [False, True, False]


def test_a_near_tie_goes_to_the_first_to_wake() -> None:
    assert _decide([Fake("kitchen", 20.0), Fake("office", 21.0)], gaps=[0.0, 0.01]) == [True, False]


def test_an_open_question_or_ringing_timer_wins_over_a_clearer_satellite() -> None:
    assert _decide([Fake("kitchen", 25.0), Fake("office", 6.0, priority=True)]) == [False, True]


def test_one_satellite_never_waits_and_separate_windows_both_answer() -> None:
    assert _decide([Fake("office", 1.0)], satellites=1) == [True]
    assert _decide([Fake("kitchen", 30.0), Fake("office", 1.0)], gaps=[0.0, 0.12]) == [True, True]


def test_a_near_voice_scores_above_the_same_voice_far_away() -> None:
    near = speech_level_db(_voice(level=3000, noise=60))
    far = speech_level_db(_voice(level=400, noise=60))
    silence = speech_level_db(_voice(level=0, noise=60))
    assert near > far + 10 and far > silence


def test_a_quiet_noise_floor_does_not_beat_louder_speech() -> None:
    # 2026-11-11, across the room: the board with the lower noise floor won and then heard no speech.
    quiet_board = speech_level_db(_voice(level=250, noise=5))
    noisy_board = speech_level_db(_voice(level=400, noise=60))
    assert noisy_board > quiet_board


def test_a_late_waker_gets_its_own_full_window_before_the_decision() -> None:
    async def scenario() -> tuple[float, list[bool]]:
        arbiter = Arbiter(0.1)
        arbiter.satellites = 2
        started = time.monotonic()
        first = asyncio.create_task(arbiter.join(Fake("waveshare", -50.0)))
        await asyncio.sleep(0.08)
        second = asyncio.create_task(arbiter.join(Fake("office", -40.0)))
        results = list(await asyncio.gather(first, second))
        return time.monotonic() - started, results

    elapsed, results = asyncio.run(scenario())
    assert results == [False, True]
    assert 0.17 <= elapsed < 0.4  # one window after the second woke, not after the first


def test_a_satellite_is_scored_on_its_first_window_only(example_config_dir: Path) -> None:
    from test_satellite import _session

    async def scenario() -> tuple[int, float, float]:
        arbiter = Arbiter(0.1)
        arbiter.satellites = 2
        session, _client = _session(example_config_dir)
        session.arbiter = arbiter
        await session.handle_start("c1", 0, None, "Victoria")
        await session.handle_audio(_voice(300, 60, seconds=0.1))
        before = session.arbitration_score()[1]
        await session.handle_audio(_voice(8000, 60, seconds=0.3))  # louder speech after its window
        return len(session._scored_audio()), before, session.arbitration_score()[1]

    scored, before, after = asyncio.run(scenario())
    assert scored == 3200 and before == after


def test_the_decision_log_says_what_each_score_was_made_of(caplog: Any) -> None:
    class Detailed(Fake):
        def arbitration_levels(self) -> str:
            return describe_levels(_voice(level=3000, noise=60), 2.0)

    with caplog.at_level("INFO", logger="victoria_voice_assistant.satellite.arbiter"):
        _decide([Detailed("kitchen", 20.0), Fake("office", 5.0)], gaps=[0.0, 0.02])
    text = next(record.text for record in caplog.records if getattr(record, "event", "") == "satellite_arbitration")
    assert "kitchen 20.0 dB (answers) [woke +0.00 s; loud " in text
    assert "dBFS, 0.48 s, offset +2.0]" in text
    assert "office 5.0 dB [woke +0.0" in text and "office 5.0 dB [woke +0.00 s; loud" not in text
    assert describe_levels(b"") == "no audio"


def test_two_satellites_hearing_the_same_words_answer_once(example_config_dir: Path) -> None:
    from test_satellite import FakeClient, _session

    async def scenario() -> tuple[list[Any], list[Any], FakeClient, FakeClient, Any, Any]:
        arbiter = Arbiter(0.05)
        arbiter.satellites = 2
        near, near_client = _session(example_config_dir)
        far, far_client = _session(example_config_dir)
        far.name = "kitchen"
        queued: dict[str, list[Any]] = {"near": [], "far": []}
        for session, key in ((near, "near"), (far, "far")):
            session.arbiter = arbiter

            async def queue_frame(frame: Any, key: str = key) -> None:
                queued[key].append(frame)

            session.task.queue_frame = queue_frame  # type: ignore[method-assign]
        await near.handle_start("c1", 0, None, "Victoria")
        await far.handle_start("c2", 0, None, "Victoria")
        for chunk in range(5):  # 0.5 s of the same words, loud near one satellite and faint at the other
            await near.handle_audio(_voice(3000, 60, seconds=0.1, seed=chunk))
            await far.handle_audio(_voice(300, 60, seconds=0.1, seed=chunk))
        assert queued == {"near": [], "far": []}  # held, not transcribed, while deciding
        await asyncio.sleep(0.15)
        return queued["near"], queued["far"], near_client, far_client, near.run, far.run

    near_frames, far_frames, near_client, far_client, near_run, far_run = asyncio.run(scenario())
    assert len(near_frames) == 5 and far_frames == []  # the held audio went on, for the nearer one only
    assert near_run is not None and not near_run.arbitrating  # its turn carries on
    assert far_run is None and far_client.kinds()[-1] == E.VOICE_ASSISTANT_RUN_END  # the other went back to idle
    assert E.VOICE_ASSISTANT_RUN_END not in near_client.kinds()
