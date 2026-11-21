"""CellarTracker export cache and MCP server (features/20261114-162903_cellartracker-wine-tool.md).

A fake CellarTracker (httpx.MockTransport) and a made-up export; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import logging
import stat
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from victoria_voice_assistant.cellartracker.export import (
    MAX_AGE_S,
    NOT_LOGGED_IN,
    CellarTrackerError,
    CellarTrackerExport,
    CredentialsMissing,
    LoginFailed,
)
from victoria_voice_assistant.cellartracker.server import build_server

PASSWORD = "s3cret-pass"

LIST_CSV = (
    "iWine,Vintage,Wine,Producer,Color,Type,Varietal,Country,Region,Quantity,Location,Bin,Size,BeginConsume,EndConsume,CT,MY,Price\r\n"
    "101,2019,Ridge Lytton Springs,Ridge,Red,Red,Zinfandel Blend,USA,California,2,Cellar,A-3,750ml,2022,2035,92.4,,45\r\n"
    "101,2019,Ridge Lytton Springs,Ridge,Red,Red,Zinfandel Blend,USA,California,1,Kitchen Fridge,2,750ml,2022,2035,92.4,,45\r\n"
    "202,2020,Kistler Chardonnay Les Noisetiers,Kistler,White,White,Chardonnay,USA,California,3,Cellar,B-1,750ml,2022,2028,93.1,95,70\r\n"
    "303,1001,Krug Grande Cuvée,Krug,White,White - Sparkling,Champagne Blend,France,Champagne,1,Cellar,C-2,750ml,,,94,,200\r\n"
)


class FakeCellarTracker:
    """Answers the export's requests; records each one."""

    def __init__(self, body: str = LIST_CSV, status: int = 200, content_type: str = "text/csv; charset=utf-8") -> None:
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.content_type = content_type
        self.requests: list[httpx.Request] = []
        self.fail_with: Exception | None = None
        self.statuses: list[int] = []  # answered in turn before `status`

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        status = self.statuses.pop(0) if self.statuses else self.status
        return httpx.Response(status, content=self.body, headers={"content-type": self.content_type})


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def no_sleep(_seconds: float) -> None:
    return None


def make_export(tmp_path: Path, fake: FakeCellarTracker, clock: Clock | None = None, login=("bill", PASSWORD)) -> CellarTrackerExport:
    return CellarTrackerExport(
        tmp_path / "cellartracker",
        lambda: login,
        transport=httpx.MockTransport(fake),
        clock=clock or Clock(),
        sleep=no_sleep,
    )


def test_first_request_fetches_and_saves_privately(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    export = make_export(tmp_path, fake)

    table = asyncio.run(export.table("List"))

    assert table.fetched_now and table.problem is None
    assert [row["Wine"] for row in table.rows][:1] == ["Ridge Lytton Springs"]
    assert len(table.rows) == 4
    params = fake.requests[0].url.params
    assert params["Table"] == "List" and params["Location"] == "1" and params["Format"] == "csv"
    assert params["User"] == "bill" and params["Password"] == PASSWORD
    cache = tmp_path / "cellartracker"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    for name in ("List.csv", "List.json"):
        assert stat.S_IMODE((cache / name).stat().st_mode) == 0o600


def test_saved_copy_is_used_for_24_hours_then_fetched_again(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    clock = Clock()
    export = make_export(tmp_path, fake, clock)
    asyncio.run(export.table("List"))

    clock.now += MAX_AGE_S - 60
    table = asyncio.run(export.table("List"))
    assert not table.fetched_now and len(fake.requests) == 1
    assert table.rows[2]["Wine"] == "Kistler Chardonnay Les Noisetiers"

    clock.now += 120  # now just over 24 hours since the fetch
    table = asyncio.run(export.table("List"))
    assert table.fetched_now and len(fake.requests) == 2


def test_saved_copy_survives_a_restart(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    clock = Clock()
    asyncio.run(make_export(tmp_path, fake, clock).table("List"))
    clock.now += 3600

    table = asyncio.run(make_export(tmp_path, fake, clock).table("List"))

    assert not table.fetched_now and len(fake.requests) == 1
    assert table.age_s(clock.now) == pytest.approx(3600)


def test_update_now_fetches_whatever_the_age(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    export = make_export(tmp_path, fake)
    asyncio.run(export.table("List"))

    table = asyncio.run(export.table("List", force=True))

    assert table.fetched_now and len(fake.requests) == 2


def test_two_requests_at_once_fetch_once(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    export = make_export(tmp_path, fake)

    async def both():
        return await asyncio.gather(export.table("List"), export.table("List"))

    first, second = asyncio.run(both())
    assert len(fake.requests) == 1
    assert len(first.rows) == len(second.rows) == 4


def test_failed_fetch_falls_back_to_the_saved_copy(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    clock = Clock()
    export = make_export(tmp_path, fake, clock)
    asyncio.run(export.table("List"))
    clock.now += MAX_AGE_S + 1
    fake.status = 503

    table = asyncio.run(export.table("List"))

    assert not table.fetched_now
    assert table.problem and "Couldn't reach CellarTracker" in table.problem and "HTTP 503" in table.problem
    assert len(table.rows) == 4


def test_failed_fetch_without_a_saved_copy_raises(tmp_path: Path) -> None:
    fake = FakeCellarTracker(status=503)
    with pytest.raises(CellarTrackerError, match="Couldn't reach CellarTracker"):
        asyncio.run(make_export(tmp_path, fake).table("List"))


def test_server_errors_are_retried(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    fake.statuses = [502]

    table = asyncio.run(make_export(tmp_path, fake).table("List"))

    assert table.fetched_now and len(fake.requests) == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, f"<html><body>{NOT_LOGGED_IN}</body></html>"), (401, ""), (403, "")],
)
def test_refused_login(tmp_path: Path, status: int, body: str) -> None:
    fake = FakeCellarTracker(body=body, status=status, content_type="text/html")
    with pytest.raises(LoginFailed):
        asyncio.run(make_export(tmp_path, fake).table("List"))
    assert len(fake.requests) == 1  # never retried


def test_other_web_page_is_not_a_login_failure(tmp_path: Path) -> None:
    fake = FakeCellarTracker(body="<html>Down for maintenance</html>", content_type="text/html")
    with pytest.raises(CellarTrackerError, match="may be down") as caught:
        asyncio.run(make_export(tmp_path, fake).table("List"))
    assert not isinstance(caught.value, LoginFailed)


def test_missing_credentials_fetch_nothing(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    with pytest.raises(CredentialsMissing, match="assistant secrets set CT_USERNAME"):
        asyncio.run(make_export(tmp_path, fake, login=None).table("List"))
    assert fake.requests == []


def test_errors_and_logs_never_hold_the_password(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeCellarTracker()
    fake.fail_with = httpx.ConnectError(f"failed for https://www.cellartracker.com/xlquery.asp?Password={PASSWORD}")

    with pytest.raises(CellarTrackerError) as caught:
        asyncio.run(make_export(tmp_path, fake).table("List"))

    assert "ConnectError" in str(caught.value)
    assert PASSWORD not in str(caught.value) and caught.value.__cause__ is None
    fake.fail_with = None
    asyncio.run(make_export(tmp_path, fake).table("List"))  # a successful fetch: httpx logs its request
    assert PASSWORD not in caplog.text


def test_windows_1252_export_is_decoded(tmp_path: Path) -> None:
    body = "iWine,Vintage,Wine,Quantity\r\n1,2018,Château Musar,1\r\n".encode("windows-1252")
    fake = FakeCellarTracker(body=body, content_type="text/csv")  # no charset: CellarTracker's default
    table = asyncio.run(make_export(tmp_path, fake).table("List"))
    assert table.rows[0]["Wine"] == "Château Musar"


def test_status_fetches_nothing(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    clock = Clock()
    export = make_export(tmp_path, fake, clock)
    assert export.status() == {}
    asyncio.run(export.table("List"))
    clock.now += 7200

    status = export.status()["List"]

    assert status["rows"] == 4 and status["fresh"] and status["age_s"] == pytest.approx(7200)
    assert len(fake.requests) == 1


# The MCP server ---------------------------------------------------------------------------------


def call(server, name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(server.call_tool(name, arguments or {}))
    assert not result.is_error, result
    return result.structured_content


def test_mcp_tools_are_listed(tmp_path: Path) -> None:
    server = build_server(make_export(tmp_path, FakeCellarTracker()))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"get_wine_list", "update_wine_list", "wine_list_status"}


def test_mcp_get_wine_list(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    server = build_server(make_export(tmp_path, fake))

    first = call(server, "get_wine_list")
    second = call(server, "get_wine_list")

    assert first["fetched_now"] and not second["fetched_now"] and len(fake.requests) == 1
    assert first["wines"] == 4 and first["bottles"] == 7
    lytton = first["list"][0]
    assert lytton["Location"] == "Cellar" and lytton["Bin"] == "A-3" and lytton["CT"] == "92.4"
    assert "MY" not in lytton  # empty columns are left out
    assert first["list"][3]["Vintage"] == "NV"


def test_mcp_update_wine_list_fetches_now(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    server = build_server(make_export(tmp_path, fake))
    call(server, "get_wine_list")

    updated = call(server, "update_wine_list")

    assert updated["updated"] and updated["bottles"] == 7 and len(fake.requests) == 2
    assert "list" not in updated


def test_mcp_update_that_fails_says_so(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    server = build_server(make_export(tmp_path, fake))
    call(server, "get_wine_list")
    fake.status = 503

    updated = call(server, "update_wine_list")

    assert not updated["updated"] and "This is the saved copy." in updated["problem"]


def test_mcp_status(tmp_path: Path) -> None:
    server = build_server(make_export(tmp_path, FakeCellarTracker()))
    assert call(server, "wine_list_status") == {"saved": False}
    call(server, "get_wine_list")
    status = call(server, "wine_list_status")
    assert status["saved"] and status["fresh"] and status["wines"] == 4


def test_mcp_error_reaches_the_model_without_the_password(tmp_path: Path) -> None:
    server = build_server(make_export(tmp_path, FakeCellarTracker(), login=None))
    # Called directly the server raises; over a transport it's an is_error result carrying this text.
    with pytest.raises(ToolError, match="assistant secrets set CT_USERNAME") as caught:
        asyncio.run(server.call_tool("get_wine_list", {}))
    assert PASSWORD not in str(caught.value)


# The built-in `wine` tool -----------------------------------------------------------------------

from victoria_voice_assistant.config.schema import AgentsConfig, TimersConfig, ToolsConfig, WeatherConfig  # noqa: E402
from victoria_voice_assistant.tools.registry import Services, ToolContext, build_registry  # noqa: E402
from victoria_voice_assistant.tools.timers import TimerService  # noqa: E402
from victoria_voice_assistant.tools.weather import WeatherService  # noqa: E402
from victoria_voice_assistant.tools.wine import WineTool  # noqa: E402

AGENTS = AgentsConfig.model_validate({"default": "helper", "agents": [{"name": "helper", "kind": "model", "model": "fast"}]})


def read(tool: WineTool, **arguments) -> dict:
    outcome = asyncio.run(tool.handle({"action": "read", **arguments}))
    assert not outcome.error, outcome
    return outcome.result


def test_wine_read_groups_places_and_puts_the_best_first(tmp_path: Path) -> None:
    result = read(WineTool(make_export(tmp_path, FakeCellarTracker())))

    assert result["matched"] == 3 and result["bottles"] == 7
    assert [wine["wine"] for wine in result["wines"]] == [
        "Krug Grande Cuvée",  # CT 94
        "Kistler Chardonnay Les Noisetiers",  # 93.1
        "Ridge Lytton Springs",  # 92.4
    ]
    ridge = result["wines"][2]
    assert ridge["bottles"] == 3
    assert ridge["where"] == [
        {"location": "Cellar", "bin": "A-3", "bottles": 2},
        {"location": "Kitchen Fridge", "bin": "2", "bottles": 1},
    ]
    assert ridge["drink"] == "2022-2035" and ridge["community_score"] == 92.4
    assert result["wines"][0]["vintage"] == "NV"
    assert result["wines"][1]["our_score"] == 95


def test_wine_highest_rated_whites(tmp_path: Path) -> None:
    result = read(WineTool(make_export(tmp_path, FakeCellarTracker())), color="white")
    assert [wine["wine"] for wine in result["wines"]] == ["Krug Grande Cuvée", "Kistler Chardonnay Les Noisetiers"]


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        ("2019 Lytton Springs", ["Ridge Lytton Springs"]),
        ("lytton springs wine", ["Ridge Lytton Springs"]),  # "wine" doesn't narrow
        ("champagne", ["Krug Grande Cuvée"]),
        ("cuvee", ["Krug Grande Cuvée"]),  # accents don't matter
        ("kitchen fridge", ["Ridge Lytton Springs"]),
        ("B-1", ["Kistler Chardonnay Les Noisetiers"]),
        ("barolo", []),
    ],
)
def test_wine_search(tmp_path: Path, search: str, expected: list[str]) -> None:
    result = read(WineTool(make_export(tmp_path, FakeCellarTracker())), search=search)
    assert [wine["wine"] for wine in result["wines"]] == expected


def test_wine_read_uses_the_saved_list(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    tool = WineTool(make_export(tmp_path, fake))
    read(tool)
    read(tool, color="red")
    assert len(fake.requests) == 1


def test_wine_says_how_old_a_fallback_list_is(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    clock = Clock()
    tool = WineTool(make_export(tmp_path, fake, clock))
    read(tool)
    clock.now += MAX_AGE_S + 3600
    fake.status = 503

    result = read(tool)

    assert result["matched"] == 3 and "saved list from 25 hours ago" in result["problem"]


def test_wine_update_fetches_now_and_says_so(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    tool = WineTool(make_export(tmp_path, fake))
    read(tool)

    outcome = asyncio.run(tool.handle({"action": "update"}))

    assert len(fake.requests) == 2 and outcome.direct and not outcome.error
    assert outcome.speak == "I've updated the wine list: 7 bottles of 3 wines."


def test_wine_update_that_fails_keeps_the_saved_list(tmp_path: Path) -> None:
    fake = FakeCellarTracker()
    tool = WineTool(make_export(tmp_path, fake))
    read(tool)
    fake.status = 503

    outcome = asyncio.run(tool.handle({"action": "update"}))

    assert outcome.error and outcome.speak.startswith("I couldn't update the wine list, so I'm still using the one from less than an hour ago.")


def test_wine_without_a_login_says_how_to_set_it_up(tmp_path: Path) -> None:
    outcome = asyncio.run(WineTool(make_export(tmp_path, FakeCellarTracker(), login=None)).handle({"action": "read"}))
    assert outcome.error and "assistant secrets set CT_USERNAME" in outcome.speak


def test_wine_tool_from_tools_yaml(tmp_path: Path) -> None:
    tools = ToolsConfig.model_validate(
        {"tools": [{"name": "wine", "description": "Our wine cellar.", "kind": "builtin", "service": "cellartracker",
                    "params": {"action": {"type": "string", "enum": ["read", "update"], "required": True}}}]}
    )  # fmt: skip
    services = Services(
        timers=TimerService(TimersConfig()),
        weather=WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})),
        cellar=make_export(tmp_path, FakeCellarTracker()),
    )
    entry = build_registry(tools, AGENTS, services).get("wine")

    assert entry.status == "ready"
    outcome = asyncio.run(entry.handler({"action": "read", "color": "red"}, ToolContext(device="kitchen")))
    assert [wine["wine"] for wine in outcome.result["wines"]] == ["Ridge Lytton Springs"]


def test_wine_search_by_grape(tmp_path: Path) -> None:
    result = read(WineTool(make_export(tmp_path, FakeCellarTracker())), search="zinfandel")
    assert [wine["wine"] for wine in result["wines"]] == ["Ridge Lytton Springs"]
    assert "no_match" not in result and "no_exact_match" not in result


@pytest.mark.parametrize("search", ["chardonay", "shar donnay", "zinfandle"])
def test_wine_search_takes_close_spellings(tmp_path: Path, search: str) -> None:
    result = read(WineTool(make_export(tmp_path, FakeCellarTracker())), search=search)
    assert len(result["wines"]) == 1 and "close spellings" in result["no_exact_match"]


def test_wine_search_with_no_match_lists_what_the_cellar_holds(tmp_path: Path) -> None:
    # Bill, 2026-11-14: "Any Tempranillo?" heard as "Any tempera Nioh", and the model searched "Nioh".
    result = read(WineTool(make_export(tmp_path, FakeCellarTracker())), search="Nioh")
    assert result["wines"] == [] and "misheard" in result["no_match"]
    assert "Chardonnay" in result["in_cellar"]["grapes"] and "Zinfandel Blend" in result["in_cellar"]["grapes"]
    assert result["in_cellar"]["producers"] == ["Kistler", "Krug", "Ridge"]


def test_wine_no_match_vocabulary_keeps_to_the_colour(tmp_path: Path) -> None:
    result = read(WineTool(make_export(tmp_path, FakeCellarTracker())), color="red", search="Nioh")
    assert result["in_cellar"]["producers"] == ["Ridge"]


# Corrections (config/corrections.yaml) ----------------------------------------------------------

from victoria_voice_assistant.corrections import Corrections  # noqa: E402

TEMPRANILLO_CSV = (
    "iWine,Vintage,Wine,Producer,Color,Varietal,MasterVarietal,Country,Region,Quantity,Location,Bin,CT\r\n"
    "127542,1968,Bodegas Vega-Sicilia Ribera del Duero Único,Bodegas Vega-Sicilia,Red,Tempranillo Blend,Tempranillo,Spain,Castilla y León,1,Cellar,A1,\r\n"
)


def corrections_tool(tmp_path: Path, body: str = TEMPRANILLO_CSV) -> tuple[WineTool, Corrections]:
    corrections = Corrections(tmp_path / "corrections.yaml")
    return WineTool(make_export(tmp_path, FakeCellarTracker(body=body)), corrections), corrections


def say(tool: WineTool, **arguments) -> str:
    outcome = asyncio.run(tool.handle(arguments))
    assert outcome.direct
    return outcome.speak


def test_corrections_file_round_trip(tmp_path: Path) -> None:
    corrections = Corrections(tmp_path / "corrections.yaml")
    assert corrections.learn("wine", "Tempra  Nioh", "Tempranillo")
    assert not corrections.learn("wine", "tempranillo", "Tempranillo")  # the same words
    text = (tmp_path / "corrections.yaml").read_text()
    assert text.startswith("# Words speech recognition mishears") and "tempra nioh: Tempranillo" in text
    assert corrections.apply("wine", "any Tempra nioh?") == ("any Tempranillo", [("tempra nioh", "Tempranillo")])
    assert corrections.apply("wine", "contemptra niohs") == ("contemptra niohs", [])  # whole words only
    assert corrections.forget("wine", "TEMPRA NIOH") == "Tempranillo" and corrections.entries("wine") == {}


def test_corrections_file_edited_by_hand_applies_at_once(tmp_path: Path) -> None:
    tool, corrections = corrections_tool(tmp_path)
    corrections.path.write_text("wine:\n  Nioh: Tempranillo\n  broken: \n", encoding="utf-8")
    result = read(tool, search="Nioh")
    assert result["corrected"] == ["“nioh” → Tempranillo"] and result["matched"] == 1


def test_corrections_file_unreadable_is_ignored(tmp_path: Path) -> None:
    tool, corrections = corrections_tool(tmp_path)
    corrections.path.write_text("wine: [not, a, mapping\n", encoding="utf-8")
    assert read(tool, search="Tempranillo")["matched"] == 1


def test_told_a_correction_then_it_is_used(tmp_path: Path) -> None:
    tool, corrections = corrections_tool(tmp_path)
    assert say(tool, action="correct", heard="tempra nioh", meant="Tempranillo") == "Got it: when I hear “tempra nioh”, you mean Tempranillo."

    result = read(tool, search="any tempra nioh")

    assert result["matched"] == 1 and "no_match" not in result and "no_exact_match" not in result
    assert result["corrected"] == ["“tempra nioh” → Tempranillo"]


def test_a_close_spelling_is_learned(tmp_path: Path) -> None:
    tool, corrections = corrections_tool(tmp_path)

    result = read(tool, search="tempera nioh")

    assert result["matched"] == 1 and result["learned"] == ["“tempera nioh” means Tempranillo"]
    assert corrections.entries("wine") == {"tempera nioh": "Tempranillo"}
    again = read(tool, search="tempera nioh")
    assert "no_exact_match" not in again and again["corrected"] == ["“tempera nioh” → Tempranillo"]


def test_a_close_spelling_keeps_the_lists_accents(tmp_path: Path) -> None:
    tool, corrections = corrections_tool(tmp_path)
    read(tool, search="unicko")
    assert corrections.entries("wine") == {"unicko": "Único"}


def test_an_ambiguous_close_spelling_is_not_learned(tmp_path: Path) -> None:
    body = TEMPRANILLO_CSV + "9,2015,Tempranilla Test,Someone,Red,Tempranilla,Tempranilla,Spain,Rioja,1,Cellar,A2,\r\n"
    tool, corrections = corrections_tool(tmp_path, body)
    result = read(tool, search="tempranillia")
    assert result["matched"] == 2 and "learned" not in result and corrections.entries("wine") == {}


def test_searching_again_after_no_match_learns_what_was_misheard(tmp_path: Path) -> None:
    tool, corrections = corrections_tool(tmp_path)
    assert "no_match" in read(tool, search="Nioh")

    result = read(tool, search="Tempranillo", heard="Nioh")

    assert result["learned"] == ["“Nioh” means Tempranillo"]
    assert corrections.entries("wine") == {"nioh": "Tempranillo"}


def test_heard_without_a_match_learns_nothing(tmp_path: Path) -> None:
    tool, corrections = corrections_tool(tmp_path)
    read(tool, search="Barolo", heard="Nioh")
    assert corrections.entries("wine") == {}


def test_list_and_forget_corrections(tmp_path: Path) -> None:
    tool, corrections = corrections_tool(tmp_path)
    assert say(tool, action="corrections") == "I don't have any wine corrections yet."
    corrections.learn("wine", "tempra nioh", "Tempranillo")
    corrections.learn("wine", "vega sicily", "Vega-Sicilia")
    assert say(tool, action="corrections") == "I have 2 wine corrections: “tempra nioh” means Tempranillo; “vega sicily” means Vega-Sicilia."
    assert say(tool, action="forget", heard="vega sicily") == "I've forgotten that “vega sicily” means Vega-Sicilia."
    assert say(tool, action="forget", heard="nothing") == "I don't have a wine correction for “nothing”."
    assert corrections.entries("wine") == {"tempra nioh": "Tempranillo"}


# Whisper's prompt ---------------------------------------------------------------------------------

from victoria_voice_assistant.tools.wine import PromptWords, prompt_words  # noqa: E402


def test_prompt_words_put_what_is_held_most_first_and_stop_at_the_limit() -> None:
    from victoria_voice_assistant.cellartracker.export import parse_csv

    _, rows = parse_csv(LIST_CSV)
    words = prompt_words(rows, meant=["Tempranillo"])
    # Corrections first, then grapes, regions and producers by bottles held; "Blend" and colours dropped.
    assert words == ["Tempranillo", "Chardonnay", "Zinfandel", "Champagne", "California", "Kistler", "Ridge", "Krug"]
    assert prompt_words(rows, limit=22) == ["Chardonnay", "Zinfandel"]  # a tie goes alphabetically
    assert prompt_words([]) == []


def test_prompt_words_follow_the_saved_list_and_the_corrections(tmp_path: Path) -> None:
    import os

    tool, corrections = corrections_tool(tmp_path)
    words = PromptWords(tool.export, corrections)
    assert words() == []  # nothing saved yet, and it never fetches
    assert not FakeCellarTracker().requests

    asyncio.run(tool.export.table("List"))
    assert words() == ["Tempranillo", "Castilla y León", "Bodegas Vega-Sicilia"]

    corrections.learn("wine", "grenash", "Garnacha")
    os.utime(corrections.path, (1, 1))  # a different modification time, however fast the test runs
    assert words()[0] == "Garnacha"


def test_voice_sessions_add_the_cellar_to_whisper_prompt_while_the_wine_tool_is_on(tmp_path: Path) -> None:
    from victoria_voice_assistant.config.schema import AgentsConfig, AppConfig, ModelsConfig
    from victoria_voice_assistant.voice.models import session_prompt

    tools = ToolsConfig.model_validate(
        {"tools": [{"name": "wine", "description": "Our wine cellar.", "kind": "builtin", "service": "cellartracker",
                    "params": {"action": {"type": "string", "enum": ["read", "update"], "required": True}}}]}
    )  # fmt: skip
    export = make_export(tmp_path, FakeCellarTracker(body=TEMPRANILLO_CSV))
    asyncio.run(export.table("List"))
    services = Services(
        timers=TimerService(TimersConfig()),
        weather=WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})),
        cellar=export,
        wine_words=PromptWords(export, Corrections(tmp_path / "corrections.yaml")),
    )
    config = AppConfig.model_construct(models=ModelsConfig.model_construct(), tools=tools, agents=AgentsConfig())
    prompt = session_prompt(config, build_registry(tools, AgentsConfig(), services), services)
    assert callable(prompt) and prompt() == "Tempranillo, Castilla y León, Bodegas Vega-Sicilia."

    off = ToolsConfig.model_validate({"tools": []})
    assert session_prompt(AppConfig.model_construct(tools=off, agents=AgentsConfig()), build_registry(off, AgentsConfig(), services), services) is None
