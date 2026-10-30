from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from victoria_voice_assistant.config.loader import default_config_dir
from victoria_voice_assistant.logging_config import configure_logging
from victoria_voice_assistant.web.auth import COOKIE_NAME, SESSION_TTL_S, SessionSigner

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_URL = "ws://127.0.0.1:8765/ws"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATIC_DIR = REPO_ROOT / "web" / "client" / "dist"


class LoginRequest(BaseModel):
    password: str


def create_web_app(
    *,
    password: str,
    service_url: str = DEFAULT_SERVICE_URL,
    static_dir: Path | None = DEFAULT_STATIC_DIR,
) -> FastAPI:
    if not password:
        raise ValueError("ASSISTANT_WEB_PASSWORD must be set")
    signer = SessionSigner(password)
    app = FastAPI(title="Victoria Voice Assistant Web")
    app.state.signer = signer

    def authenticated(token: str | None) -> bool:
        return signer.verify(token)

    @app.get("/api/session")
    async def session(request: Request) -> dict[str, bool]:
        return {"authenticated": authenticated(request.cookies.get(COOKIE_NAME))}

    @app.post("/api/login")
    async def login(body: LoginRequest, response: Response) -> dict[str, bool]:
        if not signer.check_password(body.password):
            logger.warning("Web login rejected", extra={"event": "web_login_rejected"})
            raise HTTPException(status_code=401, detail="invalid password")
        response.set_cookie(
            COOKIE_NAME,
            signer.issue(),
            max_age=SESSION_TTL_S,
            httponly=True,
            samesite="strict",
            path="/",
        )
        logger.info("Web login accepted", extra={"event": "web_login_accepted"})
        return {"authenticated": True}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME, path="/", samesite="strict")
        return {"authenticated": False}

    @app.websocket("/ws")
    async def relay(websocket: WebSocket) -> None:
        if not authenticated(websocket.cookies.get(COOKIE_NAME)):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            async with connect(service_url) as upstream:
                await _relay_messages(websocket, upstream)
        except (OSError, WebSocketException):
            logger.error("Assistant service unreachable", extra={"event": "service_unreachable"})
            await websocket.close(code=1011)
        except WebSocketDisconnect:
            pass

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="client")
    else:
        logger.warning(
            "Client build not found; run npm run build in web/client",
            extra={"event": "client_build_missing", "static_dir": str(static_dir)},
        )
    return app


async def _relay_messages(websocket: WebSocket, upstream) -> None:
    async def browser_to_service() -> None:
        while True:
            await upstream.send(await websocket.receive_text())

    async def service_to_browser() -> None:
        async for message in upstream:
            await websocket.send_text(message if isinstance(message, str) else message.decode())

    tasks = [
        asyncio.create_task(browser_to_service()),
        asyncio.create_task(service_to_browser()),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()


def create_default_web_app() -> FastAPI:
    """Factory for uvicorn: reads secrets from config/.env, never from the plist."""
    configure_logging()
    load_dotenv(default_config_dir() / ".env", override=False)
    return create_web_app(
        password=os.environ.get("ASSISTANT_WEB_PASSWORD", ""),
        service_url=os.environ.get("ASSISTANT_SERVICE_URL", DEFAULT_SERVICE_URL),
    )
