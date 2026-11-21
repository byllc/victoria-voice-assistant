"""The `wine` tool: our CellarTracker cellar, built in (features/20261114-162903_cellartracker-wine-tool.md).

`read` gives the model the cellar's wines, narrowed by colour and words when asked, best scored first,
each with where its bottles are (location and bin); the model answers from that. `update` fetches the
list from CellarTracker now and says so directly. The list itself is CellarTrackerExport's: the saved
copy while it's under 24 hours old, otherwise fetched again.

Speech recognition mishears wine words ("Tempranillo" as "tempra nioh"). Searched words go through the
`wine` corrections (config/corrections.yaml) first. Corrections are learned three ways: told one
(`correct`), a close spelling that found a wine, or the model searching again after no match and
saying what was misheard (`heard`). `corrections` lists them and `forget` removes one.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from victoria_voice_assistant.cellartracker.export import CellarTrackerError, CellarTrackerExport, Table
from victoria_voice_assistant.corrections import Corrections
from victoria_voice_assistant.tools.outcome import ToolOutcome

MAX_WINES = 25  # wines given to the model in one answer; it's told how many more matched
NON_VINTAGE = "1001"
SEARCHED = ("Wine", "Producer", "Varietal", "MasterVarietal", "Country", "Region", "SubRegion", "Appellation", "Designation", "Vineyard", "Type", "Location", "Bin")  # fmt: skip
# Words that don't narrow anything ("any Barolo", "wines from Burgundy").
IGNORED = {"a", "an", "the", "any", "some", "of", "from", "in", "our", "my", "we", "have", "do", "wine", "wines", "bottle", "bottles"}
COLOURS = {"red": "red", "reds": "red", "white": "white", "whites": "white", "rose": "rose", "rosé": "rose", "roses": "rose", "rosés": "rose", "pink": "rose"}  # fmt: skip


def _plain(text: str) -> str:
    """Lower case without accents: "Rosé" and "rose", "Château" and "chateau" match."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _bottles(row: dict[str, str]) -> int:
    quantity = _number(row.get("Quantity"))
    return int(quantity) if quantity else 0


def _hours(table: Table) -> float:
    return round(table.age_s() / 3600, 1)


SCOPE = "wine"  # this tool's section of config/corrections.yaml


class WineTool:
    def __init__(self, export: CellarTrackerExport, corrections: Corrections | None = None) -> None:
        self.export = export
        self.corrections = corrections or Corrections()

    async def handle(self, arguments: dict[str, Any]) -> ToolOutcome:
        action = str(arguments.get("action") or "read").strip().lower()
        if action == "update":
            return await self._update()
        if action == "correct":
            return self._correct(arguments)
        if action == "corrections":
            return self._list_corrections()
        if action == "forget":
            return self._forget(arguments)
        return await self._read(arguments)

    def _correct(self, arguments: dict[str, Any]) -> ToolOutcome:
        heard, meant = str(arguments.get("heard") or "").strip(), str(arguments.get("meant") or "").strip()
        if not heard or not meant:
            return ToolOutcome(result={"error": "correct needs heard and meant"}, speak="What did I mishear, and what did you mean?", error=True, direct=True)
        if not self.corrections.learn(SCOPE, heard, meant):
            return ToolOutcome(result={"saved": False}, speak=f"“{heard}” and “{meant}” are the same words, so there's nothing to correct.", direct=True)
        return ToolOutcome(result={"saved": True, "heard": heard, "meant": meant}, speak=f"Got it: when I hear “{heard}”, you mean {meant}.", direct=True)

    def _list_corrections(self) -> ToolOutcome:
        entries = self.corrections.entries(SCOPE)
        if not entries:
            return ToolOutcome(result={"corrections": {}}, speak="I don't have any wine corrections yet.", direct=True)
        said = "; ".join(f"“{heard}” means {meant}" for heard, meant in entries.items())
        count = f"{len(entries)} wine {'correction' if len(entries) == 1 else 'corrections'}"
        return ToolOutcome(result={"corrections": entries}, speak=f"I have {count}: {said}.", direct=True)

    def _forget(self, arguments: dict[str, Any]) -> ToolOutcome:
        heard = str(arguments.get("heard") or "").strip()
        meant = self.corrections.forget(SCOPE, heard) if heard else None
        if meant is None:
            return ToolOutcome(result={"forgotten": False}, speak=f"I don't have a wine correction for “{heard}”.", direct=True)
        return ToolOutcome(result={"forgotten": True, "heard": heard, "meant": meant}, speak=f"I've forgotten that “{heard}” means {meant}.", direct=True)

    async def _update(self) -> ToolOutcome:
        try:
            table = await self.export.table("List", force=True)
        except CellarTrackerError as exc:
            return ToolOutcome(result={"error": str(exc)}, speak=f"I couldn't update the wine list. {exc}", error=True, direct=True)
        if table.problem:
            return ToolOutcome(
                result={"updated": False, "problem": table.problem, "list_age_hours": _hours(table)},
                speak=f"I couldn't update the wine list, so I'm still using the one from {_age(table)}. {table.problem}",
                error=True,
                direct=True,
            )
        wines = _group(table.rows)
        bottles = sum(wine["bottles"] for wine in wines)
        return ToolOutcome(
            result={"updated": True, "wines": len(wines), "bottles": bottles},
            speak=f"I've updated the wine list: {bottles} {'bottle' if bottles == 1 else 'bottles'} of {len(wines)} {'wine' if len(wines) == 1 else 'wines'}.",
            direct=True,
        )

    async def _read(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            table = await self.export.table("List")
        except CellarTrackerError as exc:
            return ToolOutcome(result={"error": str(exc)}, speak=f"I can't read the wine list. {exc}", error=True, direct=True)
        colour = COLOURS.get(_plain(str(arguments.get("color") or "").strip()))
        search, corrected = self.corrections.apply(SCOPE, str(arguments.get("search") or ""))
        words = [word for word in (_plain(w) for w in re.findall(r"[\w'-]+", search)) if word not in IGNORED]
        coloured = [
            wine for wine in _group(table.rows) if colour is None or _plain(wine["row"].get("Color", "")).startswith(colour)
        ]
        wines = [wine for wine in coloured if all(any(word in text for text in wine["text"]) for word in words)]
        close = False
        learned: list[tuple[str, str]] = []
        if words and not wines:
            # Nothing has those words. Speech recognition often mishears wine words ("temperanillo"), so
            # take near spellings, and failing that give the model what the cellar holds to match against.
            wines, spellings = _close(coloured, words)
            close = True
            for heard, meant in spellings.items():  # one wine word for that heard word: remember it
                if self.corrections.learn(SCOPE, heard, meant):
                    learned.append((heard, meant))
        heard = str(arguments.get("heard") or "").strip()
        if heard and wines and not close and search.strip():
            # The model searched again after no match and said what was misheard ("Nioh" → Tempranillo).
            if self.corrections.learn(SCOPE, heard, search.strip()):
                learned.append((heard, search.strip()))
        # Best scored first: the community score (CT), then ours (MY); unscored wines last.
        wines.sort(key=lambda wine: (-(wine["ct"] or 0), -(wine["my"] or 0), wine["row"].get("Wine", "")))
        result: dict[str, Any] = {}
        if corrected:
            result["corrected"] = [f"“{heard}” → {meant}" for heard, meant in corrected]
        if learned:
            result["learned"] = [f"“{heard}” means {meant}" for heard, meant in learned]
        if close:
            searched = " ".join(words)
            if wines:
                result["no_exact_match"] = f"nothing has the words “{searched}”; these are close spellings (perhaps misheard)"
            else:
                result["no_match"] = (
                    f"nothing has the words “{searched}”. The words may be misheard: compare what the user said with "
                    "the grapes, regions and producers below, and search again with the right one"
                )
                result["in_cellar"] = _vocabulary(coloured)
        result |= {
            "matched": len(wines),
            "bottles": sum(wine["bottles"] for wine in wines),
            "wines": [_describe(wine) for wine in wines[:MAX_WINES]],
            "list_age_hours": _hours(table),
        }
        if len(wines) > MAX_WINES:
            result["more"] = f"{len(wines) - MAX_WINES} more matched; narrow by colour or words to see them"
        if table.problem:
            result["problem"] = f"{table.problem} This is the saved list from {_age(table)}."
        return ToolOutcome(result=result)


CLOSE = 0.8  # how alike a heard word and a wine's word must be (difflib ratio): "temprenillo" and "tempranillo" are 0.91


def _close(wines: list[dict[str, Any]], words: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Wines with a word spelled like one of the searched words, or two of them run together; and, for
    each heard phrase that matched exactly one wine word, that word as the wine list writes it."""
    units = {word: word for word in words} | {f"{a} {b}": a + b for a, b in zip(words, words[1:])}
    units = {phrase: joined for phrase, joined in units.items() if len(joined) >= 4}
    found, matched = [], {}
    for wine in wines:
        tokens = {token for text in wine["text"] for token in re.findall(r"\w+", text) if len(token) >= 4}
        hit = False
        for phrase, joined in units.items():
            for token in tokens:
                if SequenceMatcher(None, joined, token).ratio() >= CLOSE:
                    matched.setdefault(phrase, set()).add(_original(wine["row"], token))
                    hit = True
        if hit:
            found.append(wine)
    # Learn only the unambiguous ones, and prefer two words run together over one of them alone.
    spellings = {phrase: next(iter(tokens)) for phrase, tokens in matched.items() if len(tokens) == 1}
    for phrase in [p for p in spellings if " " in p]:
        for word in phrase.split():
            spellings.pop(word, None)
    return found, spellings


def _original(row: dict[str, str], token: str) -> str:
    """The wine list's own spelling of a matched word ("tempranillo" → "Tempranillo", "unico" → "Único")."""
    for column in SEARCHED:
        for word in re.findall(r"\w+", row.get(column, "")):
            if _plain(word) == token:
                return word
    return token


def _vocabulary(wines: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The cellar's distinct grapes, regions and producers, for matching a misheard word."""
    columns = {"grapes": ("Varietal", "MasterVarietal"), "regions": ("Country", "Region", "Appellation"), "producers": ("Producer",)}
    vocabulary = {}
    for name, keys in columns.items():
        values = {wine["row"].get(key, "") for wine in wines for key in keys}
        vocabulary[name] = sorted(value for value in values if value and value != "Unknown")
    return vocabulary


def _age(table: Table) -> str:
    hours = table.age_s() / 3600
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        return f"{round(hours)} {'hour' if round(hours) == 1 else 'hours'} ago"
    return f"{round(hours / 24)} days ago"


def _group(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The export has a row per wine, location and bin; this is one entry per wine with its places."""
    wines: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("iWine") or f"{row.get('Wine')}|{row.get('Vintage')}|{row.get('Size')}"
        wine = wines.get(key)
        if wine is None:
            wine = wines[key] = {
                "row": row,
                "places": [],
                "bottles": 0,
                "ct": _number(row.get("CT")),
                "my": _number(row.get("MY")),
                "text": [_plain(row.get(column, "")) for column in SEARCHED] + [row.get("Vintage", "")],
            }
        bottles = _bottles(row)
        wine["bottles"] += bottles
        place = {key: value for key, value in (("location", row.get("Location")), ("bin", row.get("Bin"))) if value}
        place["bottles"] = bottles
        wine["places"].append(place)
        wine["text"] += [_plain(row.get("Location", "")), _plain(row.get("Bin", ""))]
    return list(wines.values())


def _describe(wine: dict[str, Any]) -> dict[str, Any]:
    row = wine["row"]
    vintage = row.get("Vintage", "")
    described: dict[str, Any] = {
        "wine": row.get("Wine", ""),
        "vintage": "NV" if vintage == NON_VINTAGE else vintage,
        "color": row.get("Color", ""),
        "varietal": row.get("Varietal") or row.get("MasterVarietal", ""),
        "region": ", ".join(part for part in (row.get("Region"), row.get("Country")) if part),
        "bottles": wine["bottles"],
        "where": wine["places"],
    }
    if row.get("Size") and row["Size"] != "750ml":
        described["size"] = row["Size"]
    if wine["ct"] is not None:
        described["community_score"] = round(wine["ct"], 1)
    if wine["my"] is not None:
        described["our_score"] = round(wine["my"], 1)
    begin, end = row.get("BeginConsume", ""), row.get("EndConsume", "")
    if begin or end:
        described["drink"] = f"{begin or '?'}-{end or '?'}"
    return described


# Whisper's prompt ------------------------------------------------------------------------------------
# Whisper hears a word it's been shown far more reliably ("Tempranillo", not "tempra nioh"), so voice
# sessions add the cellar's words to its prompt. Whisper keeps 223 tokens of prompt and wine names cost
# about one token per three letters, so the words stop at PROMPT_CHARS; what's most likely to be said
# comes first: what corrections say was meant, then grapes, regions and producers by bottles held.
PROMPT_CHARS = 400
GENERIC = {"blend", "red", "white", "rose", "rosé", "unknown", "other", "sparkling", "dessert", "fortified"}


def prompt_words(rows: list[dict[str, str]], meant: list[str] = (), limit: int = PROMPT_CHARS) -> list[str]:
    """The cellar's words for Whisper's prompt, most useful first, within `limit` characters in all."""
    counts: dict[str, dict[str, int]] = {"grapes": {}, "regions": {}, "producers": {}}
    columns = {"grapes": ("MasterVarietal", "Varietal"), "regions": ("Appellation", "Region", "SubRegion"), "producers": ("Producer",)}
    for row in rows:
        for name, keys in columns.items():
            for key in keys:
                value = re.sub(r"\s+Blend$", "", row.get(key, "").strip(), flags=re.IGNORECASE)
                if value and value.lower() not in GENERIC:
                    counts[name][value] = counts[name].get(value, 0) + _bottles(row)
    words: list[str] = []
    seen: set[str] = set()
    ranked = [sorted(found, key=lambda value: (-found[value], value)) for found in counts.values()]
    for value in [*meant, *ranked[0], *ranked[1], *ranked[2]]:
        if _plain(value) in seen:
            continue
        if sum(len(word) + 2 for word in words) + len(value) > limit:
            break
        seen.add(_plain(value))
        words.append(value)
    return words


class PromptWords:
    """`prompt_words` for the saved wine list and the `wine` corrections, worked out again only when
    either file changes. Never fetches: a voice turn mustn't wait on CellarTracker."""

    def __init__(self, export: CellarTrackerExport, corrections: Corrections | None = None) -> None:
        self.export = export
        self.corrections = corrections or Corrections()
        self._key: tuple[float, float] | None = None
        self._words: list[str] = []

    def __call__(self) -> list[str]:
        key = (_mtime(self.export.cache_dir / "List.json"), _mtime(self.corrections.path))
        if key != self._key:
            table = self.export.saved("List")
            meant = list(dict.fromkeys(self.corrections.entries(SCOPE).values()))
            self._words = prompt_words(table.rows if table else [], meant)
            self._key = key
        return self._words


def _mtime(path: Any) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
