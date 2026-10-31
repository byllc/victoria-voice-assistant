from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, WebSocket
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
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
from victoria_voice_assistant.agents.runner import AgentRunner
from victoria_voice_assistant.agents.spend import SpendLog
from victoria_voice_assistant.agents.tasks import TaskService
from victoria_voice_assistant.notifications.service import NotificationService
from victoria_voice_assistant.search.service import PageReader, SearchService
from victoria_voice_assistant.voice.monitor import monitor_loop_lag

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
        search_settings = manager.current.tools.search
        reader = PageReader(search_settings.read if search_settings else None)
        browser_enabled = os.environ.get("ASSISTANT_BROWSER", "1") != "0"
        services = Services(
            timers=TimerService(manager.current.tools.timers, database),
            weather=WeatherService(manager.current.tools.weather),
            search=SearchService(search_settings, browser_search=reader.search_page if browser_enabled else None),
            reader=reader,
        )
        await services.timers.start()
        services.notifications = NotificationService(manager.current.tools.notifications, database)
        services.notifications.start()
        tasks = TaskService(database)
        tasks.start()
        services.agents = AgentRunner(manager.current, services, tasks, SpendLog(database))
        hub = ChatHub()
        app.state.services = services
        app.state.chat_hub = hub

        def on_reload(config: AppConfig) -> None:
            services.timers.update_settings(config.tools.timers)
            services.weather.update_settings(config.tools.weather)
            services.search.update_settings(config.tools.search)
            services.reader.update_settings(config.tools.search.read if config.tools.search else None)
            services.agents.update_config(config)
            services.notifications.update_settings(config.tools.notifications)
            _log_tool_status(config, services)
            hub.reload(config, services)

        manager.add_listener(on_reload)
        _log_tool_status(manager.current, services)

        background = [asyncio.create_task(monitor_loop_lag(), name="loop-lag")]
        if search_settings is not None and browser_enabled:
            background.append(asyncio.create_task(services.reader.warm(), name="browser-warm"))
        app.state.speech = None
        app.state.voice_tasks = set()
        app.state.webrtc = SmallWebRTCRequestHandler()  # no ICE servers: LAN only, nothing leaves the house
        if os.environ.get("ASSISTANT_SPEECH", "1") != "0":
            # Imported here so text-only runs (and tests) don't load MLX and ONNX.
            from victoria_voice_assistant.voice.models import SpeechModels

            speech = SpeechModels(manager.current.models.speech)
            app.state.speech = speech
            background.append(asyncio.create_task(speech.load(), name="speech-load"))
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
            for task in [*background, *app.state.voice_tasks]:
                task.cancel()
            await app.state.webrtc.close()
            await services.timers.close()
            await services.notifications.close()
            await services.reader.close()
            logger.info("Assistant service stopped", extra={"event": "service_stopped"})

    app = FastAPI(title="Victoria Voice Assistant", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str | int | None]:
        manager: ConfigManager = request.app.state.config_manager
        speech = request.app.state.speech
        return {
            "status": "ok",
            "config_revision": manager.revision,
            "speech": "off" if speech is None else "ready" if speech.ready.is_set() else speech.error or "loading",
        }

    @app.post("/api/offer")
    async def offer(request: Request) -> dict[str, str]:
        """WebRTC signaling for browser voice (proxied by the web interface; audio goes direct)."""
        speech = request.app.state.speech
        if speech is None or not speech.ready.is_set():
            raise HTTPException(status_code=503, detail=speech.error if speech and speech.error else "voice is not ready")
        manager: ConfigManager = request.app.state.config_manager
        body = await request.json()
        body.pop("request_data", None)
        body.pop("requestData", None)
        sdp = body.get("sdp")
        is_renegotiation = bool(body.get("pc_id"))
        if not isinstance(sdp, str) or not sdp.startswith("v=0") or ("m=audio" not in sdp and not is_renegotiation):
            raise HTTPException(status_code=400, detail="expected an SDP offer with an audio track")

        async def start_session(connection) -> None:
            from victoria_voice_assistant.voice.session import run_voice_session

            task = asyncio.create_task(
                run_voice_session(connection, manager.current, request.app.state.services, speech, request.app.state.chat_hub),
                name=f"voice-{connection.pc_id}",
            )
            request.app.state.voice_tasks.add(task)
            task.add_done_callback(request.app.state.voice_tasks.discard)

        answer = await request.app.state.webrtc.handle_web_request(SmallWebRTCRequest.from_dict(body), start_session)
        if answer is None:
            raise HTTPException(status_code=500, detail="no SDP answer")
        return answer

    @app.patch("/api/offer")
    async def offer_candidates(request: Request) -> dict[str, str]:
        body = await request.json()
        await request.app.state.webrtc.handle_patch_request(
            SmallWebRTCPatchRequest(
                pc_id=body["pc_id"],
                candidates=[IceCandidate(**candidate) for candidate in body.get("candidates", [])],
            )
        )
        return {"status": "ok"}

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
