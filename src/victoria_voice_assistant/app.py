from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request

from victoria_voice_assistant.config.loader import default_config_dir
from victoria_voice_assistant.config.manager import ConfigManager
from victoria_voice_assistant.logging_config import configure_logging
from victoria_voice_assistant.storage.db import default_database_path, migrate

logger = logging.getLogger(__name__)


def create_app(
    config_dir: Path | None = None,
    database_path: Path | None = None,
) -> FastAPI:
    selected_config_dir = (config_dir or default_config_dir()).expanduser().resolve()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        manager = ConfigManager(selected_config_dir)
        database = migrate(database_path or default_database_path())
        app.state.config_manager = manager
        app.state.database_path = database
        watcher = asyncio.create_task(manager.watch(), name="config-watcher")
        logger.info(
            "Assistant service started",
            extra={
                "event": "service_started",
                "config_dir": str(selected_config_dir),
                "database_path": str(database),
            },
        )
        try:
            yield
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            logger.info("Assistant service stopped", extra={"event": "service_stopped"})

    app = FastAPI(title="Victoria Voice Assistant", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str | int]:
        manager: ConfigManager = request.app.state.config_manager
        return {
            "status": "ok",
            "config_revision": manager.revision,
        }

    return app


app = create_app()
