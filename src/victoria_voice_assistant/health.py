"""The dashboard's health strip (architecture §4.1): are the services tasks depend on answering?

Checked every `INTERVAL_S` in the background: each OpenAI-compatible server in `models.yaml`
(LM Studio, Hermes's gateway, the decider) by listing its models, SearXNG by `/healthz`, and the
music server by any answer. Cloud servers aren't called; they show whether a key is set.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

import httpx

from victoria_voice_assistant.config.schema import AppConfig

logger = logging.getLogger(__name__)

INTERVAL_S = 30.0
TIMEOUT_S = 3.0


@dataclass
class Health:
    name: str
    ok: bool | None  # None: not checked (a cloud server)
    detail: str
    checked_at: float


HealthListener = Callable[[list[Health]], Awaitable[None]]


class HealthMonitor:
    def __init__(self, config: AppConfig, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self._clock = clock
        self.results: list[Health] = []
        self._listeners: list[HealthListener] = []
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
