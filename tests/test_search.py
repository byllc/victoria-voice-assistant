from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from victoria_voice_assistant.chat.prompt import system_prompt
from victoria_voice_assistant.config.schema import AgentsConfig, SearchConfig, SearchReadConfig, ToolsConfig, WeatherConfig
from victoria_voice_assistant.launchd import render_searxng_launch_agent
from victoria_voice_assistant.search import searxng
from victoria_voice_assistant.search.service import (
    UNTRUSTED,
    PageReader,
    SearchService,
    _obviously_private,
    parse_bing,
)
from victoria_voice_assistant.tools.registry import Services, ToolContext, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

SETTINGS = SearchConfig.model_validate(
    {"url": "http://127.0.0.1:8888", "engines": ["brave", "wikipedia"], "max_results": 2, "snippet_chars": 40}
)
SEARXNG_REPLY = {
    "results": [
        {"title": "Celtics beat Knicks 112-104", "url": "https://www.espn.com/nba/game", "content": "Jayson Tatum scored 31 as the Celtics beat the Knicks on Thursday night at TD Garden.", "publishedDate": "2026-10-30T02:00:00"},
        {"title": "Box score", "url": "https://nba.com/game", "content": "Final score"},
        {"title": "Third", "url": "https://example.org", "content": "not shown"},
    ],
    "answers": [],
    "unresponsive_engines": [],
}


def run(coro):
    return asyncio.run(coro)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_search_returns_short_untrusted_snippets_and_caches() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SEARXNG_REPLY)

    async def scenario():
        async with _client(handler) as client:
            service = SearchService(SETTINGS, client=client)
            first = await service.search("celtics score")
            second = await service.search("  Celtics   score ")
            return first, second

    first, second = run(scenario())
    assert seen[0].url.params["format"] == "json" and seen[0].url.params["engines"] == "brave,wikipedia"
    assert len(seen) == 1  # the second question was answered from the cache
    assert first.result["note"] == UNTRUSTED
    assert [r["source"] for r in first.result["results"]] == ["espn.com", "nba.com"]
    snippet = first.result["results"][0]["snippet"]
    assert len(snippet) == 40 and snippet.startswith("Jayson Tatum scored 31")
    assert first.result["results"][0]["date"] == "2026-10-30"
    assert second.result == first.result and first.speak is None


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(502),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        lambda request: httpx.Response(200, json={"results": [], "unresponsive_engines": [["brave", "timeout"]]}),
    ],
)
def test_search_outage_is_spoken_not_guessed(handler) -> None:
    async def scenario():
        async with _client(handler) as client:
            return await SearchService(SETTINGS, client=client).search("celtics score")

    outcome = run(scenario())
    assert outcome.error and outcome.speak == "I couldn't search right now."


BING_PAGE = """<html><body><ol id="b_results">
<li class="b_algo"><div class="b_tpcn"><a class="tilk" href="https://www.bing.com/ck/a?!&amp;p=x&amp;u=a1aHR0cHM6Ly93d3cubWZhLm9yZy92aXNpdA&amp;ntb=1">mfa.org</a></div>
<h2><a href="https://www.bing.com/ck/a?!&amp;p=x&amp;u=a1aHR0cHM6Ly93d3cubWZhLm9yZy92aXNpdA&amp;ntb=1">Visit  Hours and Admission</a></h2>
<div class="b_caption"><p class="b_lineclamp2">Open 10 am to 5 pm. Closed Tuesdays.</p></div></li>
<li class="b_algo"><h2><a href="https://example.org/direct">Direct link</a></h2></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;u=zzbroken">Broken redirect</a></h2></li>
<li class="b_ad"><h2><a href="https://ads.example.com">An ad</a></h2></li>
</ol></body></html>"""
DOWN = lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))  # noqa: E731
FALLBACK_SETTINGS = SETTINGS.model_copy(update={"browser_fallback": "bing"})


def test_parse_bing_unwraps_links_and_skips_ads_and_broken_ones() -> None:
    assert parse_bing(BING_PAGE) == [
        {"title": "Visit Hours and Admission", "url": "https://www.mfa.org/visit", "content": "Open 10 am to 5 pm. Closed Tuesdays."},
        {"title": "Direct link", "url": "https://example.org/direct", "content": ""},
    ]
    assert parse_bing("<html><body>Please solve this challenge</body></html>") == []
    assert parse_bing("") == []


@pytest.mark.parametrize(
    "handler",
    [DOWN, lambda request: httpx.Response(200, json={"results": [], "unresponsive_engines": [["brave", "CAPTCHA"]]})],
)
def test_browser_fallback_answers_when_searxng_fails(handler) -> None:
    calls: list[tuple[str, str]] = []

    async def browser_search(site: str, query: str):
        calls.append((site, query))
        return parse_bing(BING_PAGE)

    async def scenario():
        async with _client(handler) as client:
            service = SearchService(FALLBACK_SETTINGS, client=client, browser_search=browser_search)
            return await service.search("mfa hours"), await service.search("MFA hours")

    first, second = run(scenario())
    assert calls == [("bing", "mfa hours")]  # the second answer came from the cache
    assert not first.error and first.result["note"] == UNTRUSTED
    assert [r["source"] for r in first.result["results"]] == ["mfa.org", "example.org"]
    assert second.result == first.result


def test_failed_browser_fallback_is_still_spoken_not_guessed() -> None:
    async def browser_search(site: str, query: str):
        return None

    async def scenario():
        async with _client(DOWN) as client:
            return await SearchService(FALLBACK_SETTINGS, client=client, browser_search=browser_search).search("x")

    outcome = run(scenario())
    assert outcome.error and outcome.speak == "I couldn't search right now."


def test_browser_fallback_is_not_used_when_searxng_works_or_it_is_off() -> None:
    async def browser_search(site: str, query: str):
        raise AssertionError("the browser must not be used")

    async def scenario():
        replies = [SEARXNG_REPLY, {"results": [], "unresponsive_engines": []}]
        outcomes = []
        for reply in replies:
            async with _client(lambda request, reply=reply: httpx.Response(200, json=reply)) as client:
                outcomes.append(await SearchService(FALLBACK_SETTINGS, client=client, browser_search=browser_search).search("q"))
        async with _client(DOWN) as client:  # SearXNG down, but no browser_fallback in the settings
            outcomes.append(await SearchService(SETTINGS, client=client, browser_search=browser_search).search("q"))
        return outcomes

    found, empty, off = run(scenario())
    assert len(found.result["results"]) == 2 and empty.result["results"] == [] and not empty.error
    assert off.error and off.speak == "I couldn't search right now."


def test_search_with_no_results_lets_the_model_say_so() -> None:
    async def scenario():
        async with _client(lambda request: httpx.Response(200, json={"results": [], "unresponsive_engines": []})) as client:
            return await SearchService(SETTINGS, client=client).search("zzzz qqqq")

    outcome = run(scenario())
    assert not outcome.error and outcome.result["results"] == []


ARTICLE = (
    "<html><body><nav>Menu Home Contact</nav><article><h1>Store hours</h1>"
    + "".join(f"<p>Paragraph {n}: the store is open from 6 am to 10 pm, including holidays.</p>" for n in range(12))
    + "</article></body></html>"
)


def test_read_extracts_text_trims_and_marks_untrusted() -> None:
    async def scenario():
        async with _client(lambda request: httpx.Response(200, html=ARTICLE)) as client:
            reader = PageReader(SearchReadConfig(fetcher="http", max_chars=120), client=client, host_check=lambda host: True)
            return await reader.read("https://store.example.com/hours")

    outcome = run(scenario())
    assert outcome.result["note"] == UNTRUSTED
    text = outcome.result["text"]
    assert "Paragraph 0: the store is open from 6 am to 10 pm" in text and "Menu Home Contact" not in text
    assert len(text) == 120  # trimmed to max_chars


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765/health", "ftp://example.com/x", "not a url"])
def test_read_refuses_private_or_odd_addresses(url: str) -> None:
    def must_not_fetch(request: httpx.Request) -> httpx.Response:
        raise AssertionError("fetched")

    async def scenario():
        async with _client(must_not_fetch) as client:
            return await PageReader(SearchReadConfig(fetcher="http"), client=client).read(url)

    outcome = run(scenario())
    assert outcome.result["instead"] == "answer from the search snippets"
    assert outcome.speak is None  # the model carries on with the snippets


def test_failed_read_falls_back_to_snippets() -> None:
    async def scenario():
        async with _client(lambda request: httpx.Response(403)) as client:
            reader = PageReader(SearchReadConfig(fetcher="http"), client=client, host_check=lambda host: True)
            return await reader.read("https://paywalled.example.com/story")

    outcome = run(scenario())
    assert outcome.result["error"] == "page could not be read"


def test_browser_subrequests_to_private_hosts_are_blocked() -> None:
    assert _obviously_private("127.0.0.1") and _obviously_private("192.168.1.20") and _obviously_private("nas.local")
    assert _obviously_private("[::1]") and not _obviously_private("espn.com") and not _obviously_private("8.8.8.8")


def _registry(tools_config: ToolsConfig, search: SearchService | None = None):
    services = Services(
        timers=TimerService(None),
        weather=WeatherService(WeatherConfig()),
        search=search,
        reader=PageReader(None),
    )
    return build_registry(tools_config, AgentsConfig(), services)


def test_search_tools_need_the_search_section() -> None:
    tools = [
        {"name": "web_search", "description": "search", "kind": "builtin", "service": "search", "params": {"query": {"type": "string", "required": True}}},
        {"name": "web_read", "description": "read", "kind": "builtin", "service": "search.read", "params": {"url": {"type": "string"}}},
    ]
    without = _registry(ToolsConfig.model_validate({"tools": tools}))
    assert without.get("web_search").status == "auto_stub"  # type: ignore[union-attr]

    with_search = _registry(ToolsConfig.model_validate({"tools": tools, "search": {"url": "http://127.0.0.1:8888"}}))
    assert with_search.get("web_search").status == "ready"  # type: ignore[union-attr]
    # The untrusted-content warning travels with every result, not in the system prompt
    # (extra prompt lines made the test model skip other tools; decisions log).
    assert "web_search" not in system_prompt(with_search)


def test_web_search_tool_runs_the_service() -> None:
    async def scenario():
        async with _client(lambda request: httpx.Response(200, json=SEARXNG_REPLY)) as client:
            config = ToolsConfig.model_validate(
                {
                    "tools": [{"name": "web_search", "description": "s", "kind": "builtin", "service": "search"}],
                    "search": SETTINGS.model_dump(mode="json"),
                }
            )
            registry = _registry(config, SearchService(SETTINGS, client=client))
            return await registry.get("web_search").handler({"query": "celtics"}, ToolContext())  # type: ignore[union-attr]

    assert run(scenario()).result["results"][0]["source"] == "espn.com"


def test_searxng_run_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "searxng"
    (source / "searx").mkdir(parents=True)
    config = tmp_path / "config"
    monkeypatch.setenv("ASSISTANT_SEARXNG_DIR", str(source))
    monkeypatch.delenv("SEARXNG_SECRET", raising=False)
    with pytest.raises(FileNotFoundError, match="settings.yml"):
        searxng.run_command(config)
    (config / "searxng").mkdir(parents=True)
    (config / "searxng" / "settings.yml").write_text("server: {}\n", encoding="utf-8")
    if not (searxng.RUNTIME_DIR / ".venv" / "bin" / "granian").is_file():
        pytest.skip("SearXNG runtime not installed (assistant searxng install)")
    command, env, cwd = searxng.run_command(config)
    assert command[-1] == "searx.webapp:app" and "127.0.0.1" in command
    assert env["SEARXNG_SETTINGS_PATH"] == str(config / "searxng" / "settings.yml")
    assert len(env["SEARXNG_SECRET"]) == 64 and env["PYTHONPATH"] == str(source) and cwd == source


def test_searxng_launch_agent_renders(tmp_path: Path) -> None:
    document = render_searxng_launch_agent(Path("/usr/bin/python3"), tmp_path, tmp_path / "config", tmp_path / "logs", tmp_path / "searxng")
    assert document["Label"] == "com.victoria.searxng"
    assert document["ProgramArguments"][-2:] == ["searxng", "run"]  # type: ignore[index]
    assert document["EnvironmentVariables"]["ASSISTANT_SEARXNG_DIR"] == str(tmp_path / "searxng")  # type: ignore[index]


def test_example_searxng_settings_are_localhost_and_json() -> None:
    import yaml

    settings = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/examples/searxng/settings.yml").read_text())
    assert settings["server"]["bind_address"] == "127.0.0.1"
    assert "json" in settings["search"]["formats"]
    assert settings["outgoing"]["request_timeout"] <= 2.0
