from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    OutputTransportMessageFrame,
)
from starlette.websockets import WebSocketDisconnect

from victoria_voice_assistant.app import create_app
from victoria_voice_assistant.chat.llm import build_llm, build_router
from victoria_voice_assistant.chat.pinning import pin_models
from victoria_voice_assistant.chat.serializer import ChatJsonSerializer
from victoria_voice_assistant.chat.timing import RequestTimer
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.launchd import render_web_launch_agent
from victoria_voice_assistant.web.app import create_web_app
from victoria_voice_assistant.web.auth import SessionSigner

LOOPBACK = ("127.0.0.1", 50000)


def _fake_lms(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    calls = tmp_path / "lms-calls.log"
    script = tmp_path / "lms"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "ps" ]; then echo "[]"; exit 0; fi\n'
        f'echo "$@" >> "{calls}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, calls


def test_serializer_maps_user_message_to_llm_frame() -> None:
    serializer = ChatJsonSerializer()

    async def run() -> None:
        frame = await serializer.deserialize('{"type": "user_message", "text": "  hi  "}')
        assert isinstance(frame, LLMMessagesAppendFrame)
        assert frame.run_llm is True
        assert frame.messages == [{"role": "user", "content": "hi"}]
        assert await serializer.deserialize('{"type": "user_message", "text": "   "}') is None
        assert await serializer.deserialize("not json") is None
        assert await serializer.deserialize('{"type": "other"}') is None

    asyncio.run(run())


def test_serializer_maps_transport_messages_to_client_json() -> None:
    serializer = ChatJsonSerializer()

    async def run() -> None:
        assert await serializer.serialize(OutputTransportMessageFrame(message={"type": "token", "text": "Hel"})) == (
            '{"type": "token", "text": "Hel"}'
        )
        assert await serializer.serialize(LLMContextFrame(context=None)) is None  # type: ignore[arg-type]

    asyncio.run(run())


def test_protocol_processor_mirrors_reply_frames_to_transport_messages() -> None:
    from pipecat.tests.utils import run_test

    from victoria_voice_assistant.chat.serializer import ChatProtocolProcessor

    async def run() -> None:
        down, _ = await run_test(
            ChatProtocolProcessor(),
            frames_to_send=[LLMTextFrame(text="Hi"), LLMFullResponseEndFrame()],
        )
        assert [type(f) for f in down if not isinstance(f, EndFrame)] == [
            OutputTransportMessageFrame,
            LLMTextFrame,
            OutputTransportMessageFrame,
            LLMFullResponseEndFrame,
        ]
        assert [f.message for f in down if isinstance(f, OutputTransportMessageFrame)] == [
            {"type": "token", "text": "Hi"},
            {"type": "done"},
        ]

    asyncio.run(run())


def test_request_timer_logs_first_token_once_and_total(caplog: pytest.LogCaptureFixture) -> None:
    timer = RequestTimer()
    timer.model = "fast"
    with caplog.at_level(logging.INFO):
        timer.first_token()  # before start: ignored
        timer.start()
        time.sleep(0.01)
        timer.first_token()
        timer.first_token()
        timer.complete()
    events = [record.event for record in caplog.records if hasattr(record, "event")]
    assert events == ["llm_first_token", "llm_response_complete"]
    first = next(record for record in caplog.records if getattr(record, "event", "") == "llm_first_token")
    assert first.ttft_ms >= 10  # type: ignore[attr-defined]


def test_router_has_primary_and_fallback_from_config(example_config_dir: Path) -> None:
    models = load_config(example_config_dir).models
    router = build_router(models)
    assert len(router.llms) == 2  # type: ignore[attr-defined]


def test_unknown_model_params_are_sent_as_request_extras(example_config_dir: Path) -> None:
    models = load_config(example_config_dir).models
    models.models["fast"].params["reasoning_effort"] = "none"
    service = build_llm("fast", models)
    assert service._settings.extra == {"reasoning_effort": "none"}
    assert service._settings.temperature == models.models["fast"].params["temperature"]

def test_pin_models_loads_each_pinned_model_once(example_config_dir: Path, tmp_path: Path) -> None:
    lms, calls = _fake_lms(tmp_path)
    models = load_config(example_config_dir).models
    expected = sorted({config.model for config in models.models.values() if config.pin})
    assert pin_models(models, lms=str(lms)) == expected
    assert calls.read_text(encoding="utf-8").splitlines() == [f"load {model} -y" for model in expected]


def test_pin_models_skips_models_already_loaded_without_ttl(example_config_dir: Path, tmp_path: Path) -> None:
    models = load_config(example_config_dir).models
    target = sorted({config.model for config in models.models.values() if config.pin})[0]
    calls = tmp_path / "lms-calls.log"
    script = tmp_path / "lms"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "ps" ]; then\n'
        f'  echo \'[{{"modelKey": "{target}", "ttlMs": null}}]\'\n'
        "  exit 0\n"
        "fi\n"
        f'echo "$@" >> "{calls}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    pinned = pin_models(models, lms=str(script))
    assert target in pinned
    loaded = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    assert f"load {target} -y" not in loaded


def test_pin_models_logs_failure_and_continues(example_config_dir: Path, tmp_path: Path) -> None:
    lms, _ = _fake_lms(tmp_path, exit_code=1)
    models = load_config(example_config_dir).models
    assert pin_models(models, lms=str(lms)) == []


def test_session_signer_rejects_tampered_and_expired_tokens() -> None:
    signer = SessionSigner("household")
    token = signer.issue(now=1000)
    assert signer.verify(token, now=1001)
    assert not signer.verify(token, now=10**12)
    expires, signature = token.split(".", 1)
    assert not signer.verify(f"{int(expires) + 1}.{signature}", now=1001)
    assert not signer.verify(None)
    assert not signer.verify("garbage")
    assert signer.check_password("household")
    assert not signer.check_password("Household")


def test_web_login_session_and_logout() -> None:
    client = TestClient(create_web_app(password="household", static_dir=None))
    assert client.get("/api/session").json()["authenticated"] is False
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "household"}).json() == {"authenticated": True}
    assert client.get("/api/session").json()["authenticated"] is True
    client.post("/api/logout")
    assert client.get("/api/session").json()["authenticated"] is False


def test_web_websocket_requires_login() -> None:
    client = TestClient(create_web_app(password="household", static_dir=None))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_web_reports_unreachable_service_when_authenticated() -> None:
    client = TestClient(
        create_web_app(
            password="household",
            service_url="ws://127.0.0.1:1/ws",
            static_dir=None,
        )
    )
    client.post("/api/login", json={"password": "household"})
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
    assert exc.value.code == 1011


def test_web_without_password_runs_open_and_flags_insecure() -> None:
    client = TestClient(create_web_app(password="", static_dir=None))
    assert client.get("/api/session").json() == {"authenticated": True, "insecure": True}
    assert client.post("/api/login", json={"password": "x"}).status_code == 400


def test_web_with_password_is_not_insecure() -> None:
    client = TestClient(create_web_app(password="secret", static_dir=None))
    assert client.get("/api/session").json() == {"authenticated": False, "insecure": False}


def test_assistant_loopback_guard(example_config_dir: Path, tmp_path: Path) -> None:
    app = create_app(example_config_dir, tmp_path / "assistant.sqlite3")

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            remote = httpx.ASGITransport(app=app, client=("192.168.1.20", 5555))
            async with httpx.AsyncClient(transport=remote, base_url="http://test") as client:
                assert (await client.get("/health")).status_code == 403
            local = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
            async with httpx.AsyncClient(transport=local, base_url="http://test") as client:
                assert (await client.get("/health")).status_code == 200

    asyncio.run(exercise())


def test_assistant_websocket_rejects_non_loopback(example_config_dir: Path, tmp_path: Path) -> None:
    app = create_app(example_config_dir, tmp_path / "assistant.sqlite3")
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app, client=("192.168.1.20", 5555)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_text()


@pytest.mark.skipif(
    os.environ.get("ASSISTANT_LIVE_TESTS") != "1",
    reason="needs LM Studio running with the configured fast model; set ASSISTANT_LIVE_TESTS=1",
)
def test_live_text_chat_streams_tokens(example_config_dir: Path, tmp_path: Path) -> None:
    app = create_app(example_config_dir, tmp_path / "assistant.sqlite3")
    with TestClient(app, client=LOOPBACK) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text('{"type": "user_message", "text": "Say hello in one word."}')
            kinds: list[str] = []
            while True:
                message = ws.receive_json()
                kinds.append(message["type"])
                if message["type"] in ("done", "error"):
                    break
    assert kinds[-1] == "done"
    assert "token" in kinds


def test_web_launch_agent_template_renders_a_valid_plist(tmp_path: Path) -> None:
    document = render_web_launch_agent(
        Path("/usr/bin/python3"),
        tmp_path,
        tmp_path / "config",
        tmp_path / "logs",
        web_port=8766,
    )
    assert document["Label"] == "com.victoria.voice-assistant-web"
    assert document["ProgramArguments"][-1] == "web"  # type: ignore[index]
    assert document["EnvironmentVariables"]["ASSISTANT_WEB_PORT"] == "8766"  # type: ignore[index]
    assert "ASSISTANT_WEB_PASSWORD" not in str(document)
