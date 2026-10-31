"""`assistant voice bench`: speak recorded commands into a real WebRTC voice session and time the replies.

Each command is rendered with macOS `say` (a different voice from Kokoro), streamed to
the assistant service as a browser would, and timed from the end of the spoken audio to
the first audio of the reply. The service's per-stage breakdown (Pipecat's
UserBotLatencyObserver, logged as `voice_latency`) is joined from its log when given.
"""

from __future__ import annotations

import asyncio
import fractions
import json
import statistics
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np

SAMPLE_RATE = 16000
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
SPEECH_RMS = 300  # received int16 audio above this is the assistant speaking
REPLY_TIMEOUT_S = 25.0
REPLY_END_SILENCE_S = 1.2
TARGET_MS = 1500

COMMANDS = [
    "What time is it?",
    "Set a pasta timer for ten minutes.",
    "How long is left on the pasta timer?",
    "Set a laundry timer for thirty minutes.",
    "What timers do I have?",
    "Cancel the laundry timer.",
    "Add five minutes to the pasta timer.",
    "What's the weather tomorrow?",
    "Do I need an umbrella today?",
    "Turn off the kitchen lights.",
    "Play some jazz.",
    "How many ounces are in a cup?",
    "Stop.",
    "Pause.",
    "Cancel the pasta timer.",
    "What's the weather in Boston this weekend?",
    "Set a timer for two minutes.",
    "Is the heat on upstairs?",
    "Skip.",
    "What day is it?",
]
SHORT_COMMANDS = {"Stop.", "Pause.", "Skip."}


def render(text: str, directory: Path, voice: str = "Samantha") -> np.ndarray:
    """Speak ``text`` to 16 kHz mono int16 samples with macOS `say`."""
    path = directory / f"{abs(hash(text))}.wav"
    if not path.exists():
        subprocess.run(
            ["say", "-v", voice, "-o", str(path), "--file-format=WAVE", f"--data-format=LEI16@{SAMPLE_RATE}", text],
            check=True,
        )
    with wave.open(str(path)) as audio:
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)


@dataclass
class TurnResult:
    command: str
    reply_ms: float | None
    reply_s: float = 0.0
    gaps: int = 0  # 20 ms frames arriving over 100 ms apart during the reply (stutter)
    server: dict[str, Any] = field(default_factory=dict)
    transcript: str | None = None


class _Microphone:
    """An aiortc audio track that plays queued speech, then silence, in real time."""

    def __init__(self) -> None:
        from aiortc import MediaStreamTrack

        outer = self

        class Track(MediaStreamTrack):
            kind = "audio"

            async def recv(self):  # type: ignore[override]
                return await outer._next_frame()

        self.track = Track()
        self._queue: list[np.ndarray] = []
        self._pts = 0
        self._start: float | None = None
        self.speech_ended: asyncio.Event = asyncio.Event()
        self.ended_at: float | None = None

    def say(self, samples: np.ndarray) -> None:
        self.speech_ended.clear()
        padded = np.concatenate([samples, np.zeros((-len(samples)) % SAMPLES_PER_FRAME, dtype=np.int16)])
        self._queue.extend(padded.reshape(-1, SAMPLES_PER_FRAME))
        self.ended_at = None

    async def _next_frame(self):
        import av

        if self._start is None:
            self._start = time.perf_counter()
        target = self._start + self._pts / SAMPLE_RATE
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._queue:
            chunk = self._queue.pop(0)
            if not self._queue:
                self.ended_at = time.perf_counter() + FRAME_MS / 1000
                self.speech_ended.set()
        else:
            chunk = np.zeros(SAMPLES_PER_FRAME, dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._pts += SAMPLES_PER_FRAME
        return frame


class _Speaker:
    """Watches the assistant's audio for the start and end of each reply."""

    def __init__(self) -> None:
        self.speech_times: list[float] = []
        self.arrivals: list[float] = []

    async def listen(self, track) -> None:
        while True:
            try:
                frame = await track.recv()
            except Exception:  # noqa: BLE001 - track ended
                return
            self.arrivals.append(time.perf_counter())
            samples = frame.to_ndarray().astype(np.float32)
            if samples.size and float(np.sqrt(np.mean(samples**2))) > SPEECH_RMS:
                self.speech_times.append(time.perf_counter())


async def _connect(base_url: str):
    from aiortc import RTCPeerConnection, RTCSessionDescription

    pc = RTCPeerConnection()  # no ICE servers: host candidates only, as on the LAN
    mic = _Microphone()
    speaker = _Speaker()
    pc.addTrack(mic.track)
    channel = pc.createDataChannel("chat")
    listeners: list[asyncio.Task] = []

    @pc.on("track")
    def on_track(track) -> None:
        if track.kind == "audio":
            listeners.append(asyncio.ensure_future(speaker.listen(track)))

    @channel.on("open")
    def on_open() -> None:
        channel.send(json.dumps({"label": "rtvi-ai", "type": "client-ready", "id": "bench", "data": {"version": "1.0.0"}}))

    await pc.setLocalDescription(await pc.createOffer())
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base_url}/api/offer", json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        )
        response.raise_for_status()
        answer = response.json()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    return pc, mic, speaker, listeners


async def run_session(base_url: str, commands: list[str], audio_dir: Path, label: str = "") -> list[TurnResult]:
    pc, mic, speaker, listeners = await _connect(base_url)
    results: list[TurnResult] = []
    try:
        await asyncio.sleep(2.0)  # let ICE settle and the pipeline start
        for command in commands:
            heard_before = len(speaker.speech_times)
            mic.say(render(command, audio_dir))
            await mic.speech_ended.wait()
            ended = mic.ended_at or time.perf_counter()
            deadline = ended + REPLY_TIMEOUT_S
            while time.perf_counter() < deadline:
                await asyncio.sleep(0.05)
                new = speaker.speech_times[heard_before:]
                if new and time.perf_counter() - new[-1] > REPLY_END_SILENCE_S:
                    break
            reply = [t for t in speaker.speech_times[heard_before:] if t >= ended]
            window = [t for t in speaker.arrivals if reply and reply[0] <= t <= reply[-1]]
            gaps = sum(1 for a, b in zip(window, window[1:]) if b - a > 0.1)
            results.append(
                TurnResult(
                    command=command,
                    reply_ms=(reply[0] - ended) * 1000 if reply else None,
                    reply_s=(reply[-1] - reply[0]) if reply else 0.0,
                    gaps=gaps,
                )
            )
            print(f"{label}{command:<45} -> {results[-1].reply_ms or 0:7.0f} ms", flush=True)
            await asyncio.sleep(0.8)
    finally:
        for task in listeners:
            task.cancel()
        await pc.close()
    return results


def join_server_log(results: list[TurnResult], log_path: Path, since: float) -> None:
    """Attach each turn's `voice_latency` breakdown and transcript, in order."""
    latencies, transcripts = [], []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        stamp = entry.get("timestamp", "")
        try:
            from datetime import datetime

            if datetime.fromisoformat(stamp).timestamp() < since:
                continue
        except ValueError:
            continue
        if entry.get("event") == "voice_latency" and entry.get("measured_from") == "user_silence":
            latencies.append(entry)
        elif entry.get("event") == "voice_transcript":
            transcripts.append(entry.get("text"))
    for result, latency in zip(results, latencies):
        result.server = latency
    for result, text in zip(results, transcripts):
        result.transcript = text


def report(results: list[TurnResult]) -> str:
    lines = [f"{'command':<45} {'reply':>7}  server stages (ms)"]
    for result in results:
        reply = f"{result.reply_ms:7.0f}" if result.reply_ms is not None else "   none"
        parts = result.server.get("parts", {})
        stages = ", ".join(f"{key} {value:.0f}" for key, value in parts.items())
        flag = "  (stutter)" if result.gaps else ""
        lines.append(f"{result.command:<45} {reply}  {stages}{flag}")
        if result.transcript and result.transcript.strip().lower().rstrip(".?!") != result.command.lower().rstrip(".?!"):
            lines.append(f"{'':<45}          heard: {result.transcript!r}")
    timed = [r.reply_ms for r in results if r.reply_ms is not None]
    if timed:
        within = sum(1 for value in timed if value <= TARGET_MS)
        lines.append("")
        lines.append(
            f"replies {len(timed)}/{len(results)}; median {statistics.median(timed):.0f} ms, "
            f"p90 {sorted(timed)[int(0.9 * (len(timed) - 1))]:.0f} ms; {within} within the {TARGET_MS} ms target"
        )
        keyed: dict[str, list[float]] = {}
        for result in results:
            for key, value in result.server.get("parts", {}).items():
                keyed.setdefault(key, []).append(value)
        if keyed:
            lines.append("median by stage: " + ", ".join(f"{k} {statistics.median(v):.0f}" for k, v in keyed.items()))
        short = [r for r in results if r.command in SHORT_COMMANDS and r.reply_ms is not None]
        if short:
            lines.append("short commands: " + ", ".join(f"{r.command} {r.reply_ms:.0f} ms" for r in short))
    return "\n".join(lines)


async def bench(base_url: str, sessions: int = 1, log_path: Path | None = None) -> str:
    since = time.time()
    with tempfile.TemporaryDirectory() as directory:
        audio_dir = Path(directory)
        for command in COMMANDS:
            render(command, audio_dir)
        runs = await asyncio.gather(
            *(run_session(base_url, COMMANDS, audio_dir, label=f"[{index}] " if sessions > 1 else "") for index in range(sessions))
        )
    output = []
    for index, results in enumerate(runs):
        if log_path is not None and sessions == 1:
            join_server_log(results, log_path, since)
        if sessions > 1:
            output.append(f"Session {index + 1}")
        output.append(report(results))
        stutter = sum(result.gaps for result in results)
        output.append(f"audio frames arriving over 100 ms late during replies: {stutter}")
    return "\n\n".join(output)
