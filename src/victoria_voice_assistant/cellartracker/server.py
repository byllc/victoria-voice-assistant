"""Our CellarTracker MCP server (features/20261114-162903_cellartracker-wine-tool.md).

Step 1: the wine list, fetched from CellarTracker's export when the saved copy is over 24 hours old,
or now on "update the wine list". Read only. Run with `assistant cellartracker mcp` (stdio) or
`assistant cellartracker mcp --http` (streamable HTTP on 127.0.0.1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from victoria_voice_assistant.cellartracker.export import CellarTrackerError, CellarTrackerExport, Table
from victoria_voice_assistant.secret_store import get_secret
from victoria_voice_assistant.storage.db import default_database_path

# The wine list's columns worth giving a model; `full=True` gives all of them (about 70).
WINE_COLUMNS = (
    "iWine", "Wine", "Vintage", "Producer", "Color", "Type", "Category", "Varietal", "MasterVarietal",
    "Country", "Region", "SubRegion", "Appellation", "Designation", "Vineyard",
    "Quantity", "Pending", "Location", "Bin", "Size",
    "BeginConsume", "EndConsume", "CT", "MY", "Price", "Valuation",
)  # fmt: skip

NON_VINTAGE = "1001"  # CellarTracker's vintage for a non-vintage wine


def default_cache_dir() -> Path:
    return default_database_path().parent / "cellartracker"


def credentials_from_secrets() -> tuple[str, str] | None:
    user, password = get_secret("CT_USERNAME"), get_secret("CT_PASSWORD")
    return (user, password) if user and password else None


def default_export() -> CellarTrackerExport:
    return CellarTrackerExport(default_cache_dir(), credentials_from_secrets)


def count_bottles(rows: list[dict[str, str]]) -> int:
    total = 0
    for row in rows:
        try:
            total += int(float(row.get("Quantity") or 0))
        except ValueError:
            continue
    return total


def _wine(row: dict[str, str], full: bool) -> dict[str, str]:
    keys = row.keys() if full else WINE_COLUMNS
    wine = {key: row[key] for key in keys if row.get(key)}
    if wine.get("Vintage") == NON_VINTAGE:
        wine["Vintage"] = "NV"
    return wine


def _when(table: Table) -> dict[str, Any]:
    return {
        "fetched_at": datetime.fromtimestamp(table.fetched_at, timezone.utc).isoformat(timespec="seconds"),
        "age_hours": round(table.age_s() / 3600, 1),
        "fetched_now": table.fetched_now,
        **({"problem": f"{table.problem} This is the saved copy."} if table.problem else {}),
    }


async def _wine_list(export: CellarTrackerExport, *, force: bool = False) -> Table:
    """The wine list; a failed fetch with nothing saved reaches the model as our message (never a URL)."""
    try:
        return await export.table("List", force=force)
    except CellarTrackerError as exc:
        raise ToolError(str(exc)) from None


def build_server(export: CellarTrackerExport | None = None) -> MCPServer:
    export = export or default_export()
    server = MCPServer(
        name="cellartracker",
        title="CellarTracker",
        instructions=(
            "Our wine cellar from CellarTracker. get_wine_list returns what's in the cellar (one row per "
            "wine, location and bin; Quantity is bottles). It is fetched again when the saved copy is "
            "more than 24 hours old. update_wine_list fetches it now."
        ),
    )

    @server.tool(
        description=(
            "The wines in our cellar, one row per wine, location and bin: name, vintage, colour, region, "
            "varietal, bottles (Quantity), Location, Bin, drinking window, community score (CT) and our "
            "score (MY). Uses the saved list unless it is over 24 hours old. full=true gives every column."
        ),
    )
    async def get_wine_list(full: bool = False) -> dict[str, Any]:
        table = await _wine_list(export)
        return {
            **_when(table),
            "wines": len(table.rows),
            "bottles": count_bottles(table.rows),
            "list": [_wine(row, full) for row in table.rows],
        }

    @server.tool(description="Update the wine list now: fetch it from CellarTracker whatever its age.")
    async def update_wine_list() -> dict[str, Any]:
        table = await _wine_list(export, force=True)
        return {**_when(table), "updated": table.fetched_now, "wines": len(table.rows), "bottles": count_bottles(table.rows)}

    @server.tool(description="When the wine list was last fetched, and whether it's still fresh. Fetches nothing.")
    async def wine_list_status() -> dict[str, Any]:
        status = export.status().get("List")
        if status is None:
            return {"saved": False}
        return {
            "saved": True,
            "fetched_at": datetime.fromtimestamp(status["fetched_at"], timezone.utc).isoformat(timespec="seconds"),
            "age_hours": round(status["age_s"] / 3600, 1),
            "fresh": status["fresh"],
            "wines": status["rows"],
        }

    return server


def run(http: bool = False, host: str = "127.0.0.1", port: int = 8931) -> None:
    server = build_server()
    if http:
        server.run("streamable-http", host=host, port=port)
    else:
        server.run("stdio")
