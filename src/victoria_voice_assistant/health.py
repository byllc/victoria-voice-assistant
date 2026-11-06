"""The dashboard's health strip (architecture §4.1): are the services tasks depend on answering?

Checked every `INTERVAL_S` in the background: each OpenAI-compatible server in `models.yaml`
(LM Studio, Hermes's gateway, the decider) by listing its models, SearXNG by `/healthz`, and the
music server by any answer. Cloud servers aren't called; they show whether a key is set.
Then this machine (Stage 11): the assistant's memory and the Mac's free memory, whether the web
interface answers, and how old the newest backup is; each turns red past its limit.
Then each satellite (Stage 13): green while connected; one offline for `SATELLITE_NOTICE_CHECKS`
checks in a row also posts a low-priority notice to the web client, once per time it goes offline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

import httpx

from victoria_voice_assistant.config.schema import AppConfig

logger = logging.getLogger(__name__)

INTERVAL_S = 30.0
TIMEOUT_S = 3.0
# The first check waits for startup to finish: building the HTTP client briefly holds the event loop,
# which delayed the config watcher's start enough to miss an edit made right after startup.
FIRST_CHECK_AFTER_S = 2.0
MEMORY_LIMIT_MB = 3000  # the assistant itself; models run in LM Studio, not here
MIN_FREE_PERCENT = 10  # the Mac's free-memory level (kern.memorystatus_level) below which it's under pressure
BACKUP_MAX_AGE_H = 36  # a daily backup that's missed one run
SATELLITE_NOTICE_CHECKS = 2  # offline this many checks in a row (about a minute) before a notice: not a brief reconnect


@dataclass
class Health:
    name: str
    ok: bool | None  # None: not checked (a cloud server)
    detail: str
    checked_at: float


HealthListener = Callable[[list[Health]], Awaitable[None]]


class HealthMonitor:
    def __init__(
        self,
        config: AppConfig,
        clock: Callable[[], float] = time.time,
        backup_dir: Path | None = None,
        web_url: str | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self.backup_dir = backup_dir
        self.web_url = web_url
        self.results: list[Health] = []
        self._listeners: list[HealthListener] = []
        self.satellites = None  # the satellite board, once satellites run (set by the app)
        self.notifications = None  # for the offline-satellite notice (set by the app)
        self._offline: dict[str, int] = {}  # satellite -> offline checks in a row
        self._task: asyncio.Task | None = None

    def update_config(self, config: AppConfig) -> None:
        self.config = config

    def add_listener(self, listener: HealthListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: HealthListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="health")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def snapshot(self) -> list[dict[str, object]]:
        return [asdict(result) for result in self.results]

    async def _run(self) -> None:
        await asyncio.sleep(FIRST_CHECK_AFTER_S)
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001 - the strip must never take the service down
                logger.exception("Health check failed", extra={"event": "health_failed"})
            await asyncio.sleep(INTERVAL_S)

    def targets(self) -> list[tuple[str, str | None, dict[str, str], str]]:
        """(name, url or None, headers, cloud note) for each service to check."""
        targets: list[tuple[str, str | None, dict[str, str], str]] = []
        for name, server in self.config.models.servers.items():
            if server.type != "openai" or not server.base_url:
                targets.append((name, None, {}, "key set" if server.api_key else "no key (not set up)"))
                continue
            headers = {"Authorization": f"Bearer {server.api_key}"} if server.api_key else {}
            targets.append((name, server.base_url.rstrip("/") + "/models", headers, ""))
        search = self.config.tools.search
        if search is not None:
            targets.append(("searxng", str(search.url).rstrip("/") + "/healthz", {}, ""))
        music = self.config.tools.music
        if music is not None and music.server is not None:
            targets.append(("music", str(music.server), {}, ""))
        return targets

    async def check(self) -> list[Health]:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            results = await asyncio.gather(*(self._check_one(client, *target) for target in self.targets()))
            results = [*results, await self.memory(), *([await self.web()] if self.web_url else [])]
        if self.backup_dir is not None:
            results.append(self.backup())
        results.extend(self.satellite_health())
        changed = [(r.name, r.ok) for r in results] != [(r.name, r.ok) for r in self.results]
        self.results = list(results)
        if changed:
            logger.info(
                "Service health changed",
                extra={"event": "health_changed", "count": sum(1 for r in results if r.ok is False)},
            )
        for listener in list(self._listeners):
            await listener(self.results)
        return self.results

    def satellite_health(self) -> list[Health]:
        """Each satellite: connected or not. Offline for SATELLITE_NOTICE_CHECKS checks: one notice."""
        if self.satellites is None:
            return []
        now = self._clock()
        results = []
        for status in self.satellites.snapshot():
            name = status["name"]
            results.append(Health(f"satellite {name}", status["online"], "connected" if status["online"] else "offline", now))
            if status["online"]:
                self._offline.pop(name, None)
                continue
            self._offline[name] = self._offline.get(name, 0) + 1
            if self._offline[name] == SATELLITE_NOTICE_CHECKS and self.notifications is not None:
                self.notifications.post(
                    source="satellites",
                    kind="notice",
                    headline=f"The {name} satellite is offline.",
                    summary="It hasn't been connected for about a minute. Check its power and Wi-Fi.",
                    full=None,
                    target="web",
                    priority="low",
                )
        for name in list(self._offline):
            if name not in {s["name"] for s in self.satellites.snapshot()}:
                del self._offline[name]
        return results

    async def _check_one(
        self, client: httpx.AsyncClient, name: str, url: str | None, headers: dict[str, str], note: str
    ) -> Health:
        now = self._clock()
        if url is None:
            return Health(name, None, note, now)
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return Health(name, False, type(exc).__name__, now)
        if response.status_code >= 500:
            return Health(name, False, f"HTTP {response.status_code}", now)
        if response.status_code in (401, 403):
            return Health(name, False, "rejected the key", now)
        return Health(name, True, "ok", now)

    # This machine -------------------------------------------------------------------------

    async def memory(self) -> Health:
        from victoria_voice_assistant.voice.monitor import resident_mb

        own = await resident_mb()
        free = await asyncio.to_thread(free_memory_percent)
        problems = []
        if own is not None and own > MEMORY_LIMIT_MB:
            problems.append(f"assistant over {MEMORY_LIMIT_MB} MB")
        if free is not None and free < MIN_FREE_PERCENT:
            problems.append("the Mac is low on memory")
        parts = [f"assistant {own:.0f} MB" if own is not None else None, f"Mac {free}% free" if free is not None else None]
        detail = "; ".join(problems) or " · ".join(p for p in parts if p) or "unknown"
        if problems:
            detail += f" ({' · '.join(p for p in parts if p)})"
        return Health("memory", None if own is None and free is None else not problems, detail, self._clock())

    async def web(self, transport: httpx.AsyncBaseTransport | None = None) -> Health:
        """The web interface, on this machine (its certificate is our own, so it isn't verified here)."""
        now = self._clock()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, verify=False, transport=transport) as client:
                response = await client.get(f"{self.web_url}/api/session")
        except httpx.HTTPError as exc:
            return Health("web", False, type(exc).__name__, now)
        return Health("web", response.status_code < 500, "ok" if response.status_code < 500 else f"HTTP {response.status_code}", now)

    def backup(self) -> Health:
        from victoria_voice_assistant.storage.backup import list_backups

        now = self._clock()
        found = list_backups(self.backup_dir) if self.backup_dir else []
        if not found:
            return Health("backup", False, "no backup yet", now)
        age_h = (now - found[0].stat().st_mtime) / 3600
        when = f"{age_h:.0f} h ago" if age_h >= 1 else "under an hour ago"
        return Health("backup", age_h <= BACKUP_MAX_AGE_H, when if age_h <= BACKUP_MAX_AGE_H else f"last one {when}", now)


def free_memory_percent() -> int | None:
    """macOS's free-memory level (0-100); None elsewhere or if unreadable."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True, timeout=2, check=True)
        return int(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def local_web_url() -> str:
    """The web interface as this machine reaches it: HTTPS when it has a certificate."""
    from victoria_voice_assistant.config.loader import default_config_dir
    from victoria_voice_assistant.web.tls import find_tls_files

    port = os.environ.get("ASSISTANT_WEB_PORT", "8766")
    try:
        secure = find_tls_files(default_config_dir()) is not None
    except Exception:  # noqa: BLE001
        secure = False
    return f"{'https' if secure else 'http'}://127.0.0.1:{port}"
