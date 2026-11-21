from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from watchfiles import Change, awatch

from victoria_voice_assistant.config.loader import ConfigError, load_config
from victoria_voice_assistant.config.schema import AppConfig

logger = logging.getLogger(__name__)


class ConfigManager:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.current = load_config(config_dir)
        self.revision = 1
        self._listeners: list[Callable[[AppConfig], None]] = []

    def add_listener(self, listener: Callable[[AppConfig], None]) -> None:
        """Called with the new configuration after each accepted reload."""
        self._listeners.append(listener)

    def reload(self) -> bool:
        try:
            candidate = load_config(self.config_dir)
        except ConfigError as exc:
            logger.error(
                "Configuration reload rejected",
                extra={
                    "event": "config_reload_rejected",
                    "error": str(exc),
                    "config_revision": self.revision,
                },
            )
            return False
        self.current = candidate
        self.revision += 1
        logger.info(
            "Configuration reloaded",
            extra={"event": "config_reloaded", "config_revision": self.revision},
        )
        self.notify()
        return True

    def notify(self) -> None:
        """Tell every listener to rebuild from the current configuration (a reload, or something the
        tools depend on changed, like Reminders access being granted)."""
        for listener in self._listeners:
            try:
                listener(self.current)
            except Exception:
                logger.exception("Config reload listener failed", extra={"event": "config_listener_failed"})

    async def watch(self) -> None:
        config_files = {"models.yaml", "tools.yaml", "agents.yaml"}

        def watch_filter(_change: Change, path: str) -> bool:
            return Path(path).name in config_files

        async for _changes in awatch(self.config_dir, watch_filter=watch_filter):
            self.reload()
