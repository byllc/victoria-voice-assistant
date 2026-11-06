"""The web interface's document panel: show_page, the page view, and the web route (feature web-document-panel)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from victoria_voice_assistant.config.schema import SearchReadConfig
from victoria_voice_assistant.search.service import PageReader, frameable
from victoria_voice_assistant.tools.pages import NO_SCREEN, page_address, show_page
from victoria_voice_assistant.web.app import create_web_app

ARTICLE = "<html><head><title>Lentil soup &amp; bread</title></head><body><article>" + "<p>Soften the onions slowly.</p>" * 40 + "</article></body></html>"


@dataclass
class _State:
    client_id: str | None


@dataclass
class _Session:
    client_id: str | None
    sent: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.state = _State(self.client_id)

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("bbc.co.uk/weather", "https://bbc.co.uk/weather"),
        ("http://example.org", "http://example.org"),
        ("javascript:alert(1)", None),
        ("file:///etc/passwd", None),
        ("ftp://example.com", None),
        ("lentil soup", None),
        ("localhost", None),
        ("", None),
    ],
)
def test_page_address_accepts_only_web_addresses(given: str, expected: str | None) -> None:
    assert page_address(given) == expected


def test_show_page_goes_only_to_the_browser_that_asked() -> None:
    mine, also_mine, other = _Session("browser-1"), _Session("browser-1"), _Session("browser-2")
    outcome = asyncio.run(show_page({"url": "example.com/recipe", "title": "Soup"}, "browser-1", [mine, also_mine, other]))
    assert not outcome.error
    assert outcome.result["shown"] == "https://example.com/recipe"
    message = {"type": "show_page", "url": "https://example.com/recipe", "title": "Soup"}
    assert mine.sent == [message] and also_mine.sent == [message]  # chat and voice sessions of one browser
    assert other.sent == []


def test_show_page_without_a_screen_says_so() -> None:
    satellite = _Session(None)
    outcome = asyncio.run(show_page({"url": "https://example.com"}, None, [satellite]))
    assert outcome.error and outcome.speak == NO_SCREEN
    assert satellite.sent == []


def test_show_page_refuses_other_schemes() -> None:
    browser = _Session("browser-1")
    outcome = asyncio.run(show_page({"url": "javascript:alert(1)"}, "browser-1", [browser]))
    assert outcome.error and browser.sent == []


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, True),
        ({"X-Frame-Options": "DENY"}, False),
        ({"X-Frame-Options": "SAMEORIGIN"}, False),
        ({"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"}, False),
        ({"Content-Security-Policy": "frame-ancestors 'self' https://partner.example"}, False),
        ({"Content-Security-Policy": "frame-ancestors *"}, True),
        ({"Content-Security-Policy": "frame-ancestors https:"}, True),
        ({"Content-Security-Policy": "default-src 'self'"}, True),
    ],
)
def test_frameable_reads_framing_headers(headers: dict[str, str], expected: bool) -> None:
    assert frameable(httpx.Headers(headers)) is expected


def _reader(handler, host_check=lambda host: True) -> PageReader:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PageReader(SearchReadConfig(fetcher="http"), client=client, host_check=host_check)


def test_page_view_gives_title_text_and_framing() -> None:
    reader = _reader(lambda request: httpx.Response(200, html=ARTICLE, headers={"X-Frame-Options": "DENY"}))
    view = asyncio.run(reader.page_view("https://example.com/soup"))
    assert view["frameable"] is False
    assert view["title"] == "Lentil soup & bread"
    assert "Soften the onions" in view["text"]
    assert view["error"] is None


def test_page_view_allows_framing_when_the_site_does() -> None:
    reader = _reader(lambda request: httpx.Response(200, html=ARTICLE))
    assert asyncio.run(reader.page_view("https://example.com/soup"))["frameable"] is True


def test_page_view_refuses_private_addresses() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, html=ARTICLE)

    reader = _reader(handler, host_check=lambda host: False)
    view = asyncio.run(reader.page_view("http://192.168.1.10/admin"))
    assert view["error"] == "address is not on the public internet" and not view["frameable"]
    assert calls == []  # never fetched
    assert asyncio.run(reader.page_view("file:///etc/passwd"))["error"] == "not a web address"


def test_web_page_route_requires_login() -> None:
    client = TestClient(create_web_app(password="household", static_dir=None))
    assert client.get("/api/page", params={"url": "https://example.com"}).status_code == 401


def test_web_page_route_reports_unreachable_service() -> None:
    client = TestClient(create_web_app(password="", service_url="ws://127.0.0.1:9/ws", static_dir=None))
    assert client.get("/api/page", params={"url": "https://example.com"}).status_code == 502
