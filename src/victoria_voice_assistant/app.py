from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, WebSocket
from starlette.types import ASGIApp, Receive, Scope, Send

from victoria_voice_assistant.chat.pinning import pin_models
from victoria_voice_assistant.chat.session import ChatHub, run_chat_session
from victoria_voice_assistant.config.loader import default_config_dir
from victoria_voice_assistant.config.manager import ConfigManager
from victoria_voice_assistant.logging_config import configure_logging
from victoria_voice_assistant.config.schema import AppConfig
from victoria_voice_assistant.storage.db import default_database_path, migrate
from victoria_voice_assistant.tools.registry import Services, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

logger = logging.getLogger(__name__)


class LoopbackOnlyMiddleware:
    """Reject HTTP and WebSocket connections that do not originate from loopback.

    The assistant service is never meant to be reached from the network; the web
    process is the only intended client.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        if client and _is_loopback(client[0]):
            await self.app(scope, receive, send)
            return
        logger.warning(
            "Rejected non-loopback connection",
            extra={"event": "non_loopback_rejected", "client": client[0] if client else None},
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": b"forbidden"})


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _log_tool_status(config: AppConfig, services: Services) -> None:
    """Build the registry once (startup, reload) so automatic stubs are logged once, not per chat."""
    registry = build_registry(config.tools, config.agents, services, log_stubs=True)
    logger.info(
        "Tools loaded",
        extra={"event": "tools_loaded", "count": len(registry.active)},
    )


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
        services = Services(
            timers=TimerService(manager.current.tools.timers, database),
            weather=WeatherService(manager.current.tools.weather),
        )
        await services.timers.start()
        hub = ChatHub()
        app.state.services = services
        app.state.chat_hub = hub

        def on_reload(config: AppConfig) -> None:
            services.timers.update_settings(config.tools.timers)
            services.weather.update_settings(config.tools.weather)
            _log_tool_status(config, services)
            hub.reload(config, services)

        manager.add_listener(on_reload)
        _log_tool_status(manager.current, services)
        watcher = asyncio.create_task(manager.watch(), name="config-watcher")
        pinning: asyncio.Task | None = None
        if os.environ.get("ASSISTANT_PIN_MODELS", "1") != "0":
            # Pinning shells out to `lms` and can take minutes; keep startup responsive.
            pinning = asyncio.create_task(
                asyncio.to_thread(pin_models, manager.current.models),
                name="model-pinning",
            )
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
            if pinning is not None:
                pinning.cancel()
            await services.timers.close()
            logger.info("Assistant service stopped", extra={"event": "service_stopped"})

    app = FastAPI(title="Victoria Voice Assistant", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str | int]:
        manager: ConfigManager = request.app.state.config_manager
        return {
            "status": "ok",
            "config_revision": manager.revision,
        }

    @app.websocket("/ws")
    async def chat(websocket: WebSocket) -> None:
        await websocket.accept()
        manager: ConfigManager = websocket.app.state.config_manager
        try:
            await run_chat_session(
                websocket,
                manager.current,
                websocket.app.state.services,
                websocket.app.state.chat_hub,
            )
        except Exception:
            logger.exception("Chat session failed", extra={"event": "chat_session_failed"})
            raise

    app.add_middleware(LoopbackOnlyMiddleware)
    return app


app = create_app()
