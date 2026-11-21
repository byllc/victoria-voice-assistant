"""CellarTracker's CSV export, fetched and kept on disk (features/20261114-162903_cellartracker-wine-tool.md).

CellarTracker has no API. Its export (`xlquery.asp`) takes the account's username and password in the
URL, over HTTPS, and returns a table as CSV. The password is in the URL, so nothing here ever logs,
returns or raises a URL: errors carry a status code or an exception's class name only.

A table is fetched when its saved copy is older than `max_age_s` (24 hours), or when asked to
(`force`, "update the wine list"). When a fetch fails and a saved copy exists, the saved copy is
returned with the reason, so a CellarTracker outage doesn't empty the cellar.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

EXPORT_URL = "https://www.cellartracker.com/xlquery.asp"
MAX_AGE_S = 24 * 3600

# The export's tables and the parameters each needs. `Location=1` splits the wine list by location and
# bin, so "what bin is it in" can be answered from it.
TABLES: dict[str, dict[str, str]] = {
    "List": {"Table": "List", "Location": "1"},
    "Bottles": {"Table": "Bottles"},
    "Availability": {"Table": "Availability"},
    "Notes": {"Table": "Notes"},
    "Purchase": {"Table": "Purchase"},
    "Consumed": {"Table": "Consumed"},
    "Pending": {"Table": "Pending"},
    "Tag": {"Table": "Tag"},
}

# The page CellarTracker returns (HTTP 200) when the login fails.
NOT_LOGGED_IN = "You are currently not logged into CellarTracker."

FETCH_DEADLINE_S = 50.0  # one budget for all attempts; large accounts take 20 s+ for one table
MAX_ATTEMPTS = 3
MIN_ATTEMPT_S = 5.0  # no retry unless at least this much of the budget is left after the wait


class CellarTrackerError(Exception):
    """A fetch that failed. The message is safe to show and log: it never holds the URL."""


class CredentialsMissing(CellarTrackerError):
    def __init__(self) -> None:
        super().__init__(
            "CellarTracker isn't set up: store the login with `uv run assistant secrets set CT_USERNAME` "
            "and `uv run assistant secrets set CT_PASSWORD`."
        )


class LoginFailed(CellarTrackerError):
    def __init__(self) -> None:
        super().__init__("CellarTracker refused the login. Check CT_USERNAME and CT_PASSWORD.")


class _Retryable(Exception):
    pass


class _DropExportUrls(logging.Filter):
    """httpx logs each request's URL at INFO; for the export that URL holds the password."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "xlquery.asp" not in record.getMessage()


logging.getLogger("httpx").addFilter(_DropExportUrls())


@dataclass
class Table:
    name: str
    rows: list[dict[str, str]]
    fetched_at: float  # Unix time of the fetch these rows came from
    fetched_now: bool  # True when this request fetched it; False when it's the saved copy
    problem: str | None = None  # why a fetch that was needed failed, when the saved copy was used instead
    columns: list[str] = field(default_factory=list)
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else self.clock()) - self.fetched_at)


Credentials = Callable[[], tuple[str, str] | None]
Sleep = Callable[[float], Awaitable[None]]


def charset(content_type: str | None) -> str:
    match = re.search(r"charset=([^\s;]+)", content_type or "", re.IGNORECASE)
    return match.group(1).strip("\"'") if match else "windows-1252"


def parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows = [{key: (value or "").strip() for key, value in row.items() if key is not None} for row in reader]
    return list(reader.fieldnames or []), rows


class CellarTrackerExport:
    """The export's tables, each fetched at most once per `max_age_s` and saved under `cache_dir`."""

    def __init__(
        self,
        cache_dir: Path,
        credentials: Credentials,
        *,
        max_age_s: float = MAX_AGE_S,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
        deadline_s: float = FETCH_DEADLINE_S,
    ) -> None:
        self.cache_dir = Path(cache_dir).expanduser()
        self._credentials = credentials
        self.max_age_s = max_age_s
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._deadline_s = deadline_s
        self._locks: dict[str, asyncio.Lock] = {}

    # Reading -----------------------------------------------------------------------------------

    async def table(self, name: str, *, force: bool = False) -> Table:
        """The table's rows: the saved copy while it's fresh, otherwise fetched (and saved)."""
        if name not in TABLES:
            raise ValueError(f"unknown CellarTracker table {name!r}; known: {', '.join(TABLES)}")
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:  # two requests at once fetch once
            saved = self._load(name)
            if saved is not None and not force and saved.age_s(self._clock()) < self.max_age_s:
                return saved
            try:
                text = await self._fetch(name)
            except CellarTrackerError as exc:
                if saved is None:
                    raise
                logger.warning(
                    "CellarTracker fetch failed; using the saved copy",
                    extra={"event": "cellartracker_fetch_failed", "table": name, "reason": str(exc)},
                )
                saved.problem = str(exc)
                return saved
            fetched_at = self._clock()
            columns, rows = parse_csv(text)
            self._save(name, text, fetched_at, len(rows))
            logger.info(
                "CellarTracker table fetched",
                extra={"event": "cellartracker_fetched", "table": name, "rows": len(rows), "forced": force},
            )
            return Table(name, rows, fetched_at, fetched_now=True, columns=columns, clock=self._clock)

    def saved(self, name: str) -> Table | None:
        """The saved copy of a table, however old, without fetching; None when there isn't one."""
        return self._load(name)

    def status(self) -> dict[str, dict[str, Any]]:
        """Each saved table's fetch time, age and row count, without fetching anything."""
        now = self._clock()
        result: dict[str, dict[str, Any]] = {}
        for name in TABLES:
            meta = self._meta(name)
            if meta is None:
                continue
            fetched_at = float(meta["fetched_at"])
            result[name] = {
                "fetched_at": fetched_at,
                "age_s": max(0.0, now - fetched_at),
                "rows": meta.get("rows"),
                "fresh": now - fetched_at < self.max_age_s,
            }
        return result

    # Fetching ----------------------------------------------------------------------------------

    async def _fetch(self, name: str) -> str:
        login = self._credentials()
        if not login or not login[0] or not login[1]:
            raise CredentialsMissing()
        user, password = login
        params = {"User": user, "Password": password, "Format": "csv", **TABLES[name]}
        start = time.monotonic()
        deadline = start + self._deadline_s
        async with httpx.AsyncClient(transport=self._transport, follow_redirects=False) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    return await self._fetch_once(client, params, max(0.1, deadline - time.monotonic()))
                except _Retryable as exc:
                    wait = 1.0 * 3 ** (attempt - 1) * (0.85 + random.random() * 0.3)
                    if attempt < MAX_ATTEMPTS and deadline - time.monotonic() - wait >= MIN_ATTEMPT_S:
                        await self._sleep(wait)
                        continue
                    attempts = "1 attempt" if attempt == 1 else f"{attempt} attempts"
                    raise CellarTrackerError(
                        f"Couldn't reach CellarTracker for the {name} table after {attempts} ({exc}, "
                        f"{time.monotonic() - start:.0f} s)."
                    ) from None
        raise AssertionError("unreachable")

    async def _fetch_once(self, client: httpx.AsyncClient, params: dict[str, str], timeout_s: float) -> str:
        try:
            response = await client.get(EXPORT_URL, params=params, timeout=timeout_s)
            body = response.content
        except httpx.HTTPError as exc:
            # The class name only: an httpx error's message can hold the URL.
            raise _Retryable(type(exc).__name__) from None
        if response.status_code in (401, 403):
            raise LoginFailed()
        if response.status_code >= 500:
            raise _Retryable(f"HTTP {response.status_code}")
        if response.status_code >= 300:
            raise CellarTrackerError(f"CellarTracker answered HTTP {response.status_code}.")
        try:
            text = body.decode(charset(response.headers.get("content-type")))
        except (LookupError, UnicodeDecodeError):
            text = body.decode("windows-1252", errors="replace")
        if text.lstrip().startswith("<"):
            if NOT_LOGGED_IN in text:
                raise LoginFailed()
            raise CellarTrackerError("CellarTracker returned a web page instead of the export; it may be down.")
        return text.replace("\r\n", "\n")

    # The cache ---------------------------------------------------------------------------------

    def _csv_path(self, name: str) -> Path:
        return self.cache_dir / f"{name}.csv"

    def _meta_path(self, name: str) -> Path:
        return self.cache_dir / f"{name}.json"

    def _meta(self, name: str) -> dict[str, Any] | None:
        try:
            meta = json.loads(self._meta_path(name).read_text(encoding="utf-8"))
            float(meta["fetched_at"])
            return meta
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _load(self, name: str) -> Table | None:
        meta = self._meta(name)
        if meta is None:
            return None
        try:
            text = self._csv_path(name).read_text(encoding="utf-8")
        except OSError:
            return None
        columns, rows = parse_csv(text)
        return Table(name, rows, float(meta["fetched_at"]), fetched_now=False, columns=columns, clock=self._clock)

    def _save(self, name: str, text: str, fetched_at: float, rows: int) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        _write_private(self._csv_path(name), text)
        # The fetch time is written last: a half-written cache is never taken for a fresh one.
        _write_private(self._meta_path(name), json.dumps({"fetched_at": fetched_at, "rows": rows}))


def _write_private(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
