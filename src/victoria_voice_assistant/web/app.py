from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from urllib.parse import urlsplit
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from victoria_voice_assistant.logging_config import configure_logging
from victoria_voice_assistant.web.auth import COOKIE_NAME, SESSION_TTL_S, SessionSigner

logger = logging.getLogger(__name__)

# Browsers want the web app manifest (home-screen install) served with its own type.
mimetypes.add_type("application/manifest+json", ".webmanifest")

DEFAULT_SERVICE_URL = "ws://127.0.0.1:8765/ws"
SIGNALING_TIMEOUT_S = 30.0
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATIC_DIR = REPO_ROOT / "web" / "client" / "dist"


class LoginRequest(BaseModel):
    password: str


def create_web_app(
    *,
    password: str = "",
    service_url: str = DEFAULT_SERVICE_URL,
    static_dir: Path | None = DEFAULT_STATIC_DIR,
    secure_cookies: bool = False,
    sendspin_url: str | None = None,
) -> FastAPI:
    # Without a password the app runs open (insecure) and the client shows a warning.
    insecure = not password
    signer = SessionSigner(password) if password else None
    app = FastAPI(title="Victoria Voice Assistant Web")
    app.state.signer = signer
    if insecure:
        logger.warning(
            "ASSISTANT_WEB_PASSWORD is not set; web interface is open without login",
            extra={"event": "web_insecure_mode"},
        )

    def authenticated(token: str | None) -> bool:
        return insecure or signer.verify(token)

    @app.get("/api/session")
    async def session(request: Request) -> dict[str, bool]:
        return {
            "authenticated": authenticated(request.cookies.get(COOKIE_NAME)),
            "insecure": insecure,
        }

    @app.post("/api/login")
    async def login(body: LoginRequest, response: Response) -> dict[str, bool]:
        if signer is None:
            raise HTTPException(status_code=400, detail="no password configured")
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
            secure=secure_cookies,
        )
        logger.info("Web login accepted", extra={"event": "web_login_accepted"})
        return {"authenticated": True}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME, path="/", samesite="strict")
        return {"authenticated": False}

    # WebRTC signaling is proxied; the audio itself goes straight to the assistant service.
    offer_url = _http_url(service_url) + "/api/offer"

    async def forward_offer(request: Request) -> Response:
        if not authenticated(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(status_code=401, detail="not signed in")
        try:
            async with httpx.AsyncClient(timeout=SIGNALING_TIMEOUT_S) as client:
                upstream = await client.request(
                    request.method,
                    offer_url,
                    content=await request.body(),
                    headers={"content-type": "application/json"},
                )
        except httpx.HTTPError:
            logger.error("Assistant service unreachable", extra={"event": "service_unreachable"})
            raise HTTPException(status_code=502, detail="assistant service unreachable") from None
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    app.add_api_route("/api/offer", forward_offer, methods=["POST", "PATCH"])

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

    @app.get("/api/music")
    async def music_info(request: Request) -> dict[str, bool]:
        """Whether this browser can be a music speaker (Sendspin through /sendspin)."""
        return {"speaker": bool(sendspin_url) and authenticated(request.cookies.get(COOKIE_NAME))}

    music_host = urlsplit(sendspin_url).hostname if sendspin_url else None

    @app.get("/api/music/artwork")
    async def music_artwork(request: Request, url: str) -> Response:
        """Album art for the player widget. Music Assistant serves it over plain HTTP, which an HTTPS page
        won't load, so it comes through here: only images, only from the Music Assistant host."""
        if not authenticated(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(status_code=401, detail="not signed in")
        target = urlsplit(url)
        if not music_host or target.scheme not in ("http", "https") or target.hostname != music_host:
            raise HTTPException(status_code=400, detail="not the music server")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                upstream = await client.get(url)
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="music server unreachable") from None
        content_type = upstream.headers.get("content-type", "")
        if upstream.status_code != 200 or not content_type.startswith("image/"):
            raise HTTPException(status_code=404, detail="no image")
        return Response(content=upstream.content, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.websocket("/sendspin")
    async def sendspin(websocket: WebSocket) -> None:
        """The browser as a Music Assistant speaker: Sendspin relayed to Music Assistant (Stage 10).

        Relayed rather than direct so it works from the HTTPS page (a browser won't open plain ws:// there)
        and stays behind the household login. Goes to Music Assistant's Sendspin listener (port 8927 by
        default), not its API port's /sendspin, which wants Music Assistant's own login first. Audio is binary.
        """
        if not sendspin_url or not authenticated(websocket.cookies.get(COOKIE_NAME)):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            async with connect(sendspin_url, max_size=None) as upstream:
                try:
                    await _relay_frames(websocket, upstream)
                finally:
                    logger.info(
                        "Speaker relay closed",
                        extra={"event": "sendspin_closed", "status": upstream.close_code, "reason": upstream.close_reason or ""},
                    )
        except OSError as exc:
            logger.error("Music server unreachable", extra={"event": "sendspin_unreachable", "reason": str(exc)})
            await websocket.close(code=1011)
        except WebSocketException as exc:
            logger.warning("Speaker relay ended", extra={"event": "sendspin_closed", "reason": repr(exc)})
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


async def _relay_frames(websocket: WebSocket, upstream) -> None:
    """Both directions, text and binary."""

    async def browser_to_server() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    async def server_to_browser() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    tasks = [asyncio.create_task(browser_to_server()), asyncio.create_task(server_to_browser())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()


def _http_url(service_url: str) -> str:
    """http://host:port of the assistant service, from its websocket URL."""
    base = service_url.removesuffix("/ws")
    return "https://" + base.removeprefix("wss://") if base.startswith("wss://") else "http://" + base.removeprefix("ws://")


def create_default_web_app() -> FastAPI:
    """Factory for uvicorn: the password comes from the Keychain (or the process environment), never a file."""
    from victoria_voice_assistant.secret_store import get_secret

    configure_logging()
    return create_web_app(
        password=get_secret("ASSISTANT_WEB_PASSWORD") or "",
        service_url=os.environ.get("ASSISTANT_SERVICE_URL", DEFAULT_SERVICE_URL),
        secure_cookies=os.environ.get("ASSISTANT_WEB_TLS") == "1",
        sendspin_url=os.environ.get("ASSISTANT_SENDSPIN_URL") or None,
    )
