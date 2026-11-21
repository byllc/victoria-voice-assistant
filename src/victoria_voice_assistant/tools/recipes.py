"""The `recipes` tool (feature 20261112-025716): a folder of recipes written to a template, compared
with the grocery list, with web search when nothing in the folder is close to what was asked.

- "What do I need to make paella?": the recipe's ingredients against what we have.
- "With the groceries I have, what can I make?": the folder's recipes with the fewest missing ingredients.

"Have" means checked off the grocery list within `recipes.have_days`, or one of `recipes.staples`.
A web recipe is saved to the folder only when asked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from victoria_voice_assistant.config.schema import RecipesConfig
from victoria_voice_assistant.search.service import UNTRUSTED
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome
from victoria_voice_assistant.tools.reminders import AMBIGUOUS_WITHIN, MATCH_AT_LEAST, ListTool, ReminderItem, _join, list_score

logger = logging.getLogger(__name__)

TEMPLATE_NAME = "_template.md"
TEMPLATE = """# Recipe name
Serves: 4
Time: 45 minutes
Tags: dinner, quick

## Ingredients
- 2 cups rice
- 1 onion, chopped
- 1 pinch saffron

## Steps
1. First step.
2. Second step.

## Notes
Anything worth remembering. A web recipe keeps its link here.
Source: https://example.com/recipe
"""
MAX_SUGGESTIONS = 3
WEB_RESULTS_TRIED = 4
MAX_INGREDIENTS = 60
MAX_LINE = 200


@dataclass
class Recipe:
    title: str
    ingredients: list[str]
    steps: list[str] = field(default_factory=list)
    serves: str | None = None
    time: str | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    source: str | None = None
    path: Path | None = None


# --- the template ------------------------------------------------------------------------------


def parse_recipe(text: str) -> Recipe:
    """A recipe file written to the template. Only the title and the Ingredients list are required."""
    title: str | None = None
    header: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if title is None and line.startswith("# "):
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections.setdefault(current, [])
            continue
        if current is None:
            key, colon, value = line.partition(":")
            if colon and key.strip().lower() in ("serves", "time", "tags"):
                header[key.strip().lower()] = value.strip()
        else:
            sections[current].append(line)
    if not title:
        raise ValueError("no title (a first line like `# Paella`)")
    ingredients = [_item(line) for line in sections.get("ingredients", []) if _item(line)]
    if not ingredients:
        raise ValueError("no `## Ingredients` list (one `- item` per line)")
    steps = [_item(line) for line in sections.get("steps", []) if _item(line)]
    notes_lines = sections.get("notes", [])
    source = next((line.partition(":")[2].strip() for line in notes_lines if line.lower().startswith("source:")), None)
    notes = "\n".join(line for line in notes_lines if not line.lower().startswith("source:")).strip()
    return Recipe(
        title=title,
        ingredients=ingredients,
        steps=steps,
        serves=header.get("serves") or None,
        time=header.get("time") or None,
        tags=[tag.strip() for tag in header.get("tags", "").split(",") if tag.strip()],
        notes=notes,
        source=source or None,
    )


def _item(line: str) -> str:
    """A list line's text: "- 2 cups rice" and "1. Heat the stock." without their markers."""
    return re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip() if re.match(r"^\s*(?:[-*•]|\d+[.)])\s+\S", line) else ""


def render_recipe(recipe: Recipe) -> str:
    """The recipe as a file in the template."""
    lines = [f"# {recipe.title}"]
    if recipe.serves:
        lines.append(f"Serves: {recipe.serves}")
    if recipe.time:
        lines.append(f"Time: {recipe.time}")
    if recipe.tags:
        lines.append(f"Tags: {', '.join(recipe.tags)}")
    lines += ["", "## Ingredients", *(f"- {item}" for item in recipe.ingredients)]
    if recipe.steps:
        lines += ["", "## Steps", *(f"{index}. {step}" for index, step in enumerate(recipe.steps, 1))]
    if recipe.notes or recipe.source:
        lines += ["", "## Notes"]
        if recipe.notes:
            lines.append(recipe.notes)
        if recipe.source:
            lines.append(f"Source: {recipe.source}")
    return "\n".join(lines) + "\n"


# --- the folder --------------------------------------------------------------------------------


class RecipeBook:
    """The recipes folder: one Markdown file per recipe, and `_template.md`."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.problems: list[tuple[str, str]] = []  # files that don't fit the template, and why

    def ensure(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        template = self.folder / TEMPLATE_NAME
        if not template.exists():
            template.write_text(TEMPLATE, encoding="utf-8")

    def load(self) -> list[Recipe]:
        recipes: list[Recipe] = []
        self.problems = []
        if not self.folder.is_dir():
            return recipes
        for path in sorted(self.folder.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                recipe = parse_recipe(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                self.problems.append((path.name, str(exc)))
                continue
            recipe.path = path
            recipes.append(recipe)
        for name, reason in self.problems:
            logger.warning("Recipe file skipped", extra={"event": "recipe_skipped", "names": [name], "reason": reason})
        return recipes

    def find(self, asked: str) -> list[Recipe]:
        """The recipes ``asked`` means, by title or tags: one, several close ones (ask which), or none."""
        recipes = self.load()
        exact = [recipe for recipe in recipes if recipe.title.strip().casefold() == asked.strip().casefold()]
        if exact:
            return exact[:1]
        ranked = sorted(
            ((max([list_score(asked, recipe.title), *(0.9 * list_score(asked, tag) for tag in recipe.tags)]), index) for index, recipe in enumerate(recipes)),
            reverse=True,
        )
        if not ranked or ranked[0][0] < MATCH_AT_LEAST:
            return []
        best = ranked[0][0]
        return [recipes[index] for score, index in ranked if score >= MATCH_AT_LEAST and best - score < AMBIGUOUS_WITHIN]

    def save(self, recipe: Recipe) -> Path:
        self.ensure()
        slug = re.sub(r"[^a-z0-9]+", "-", recipe.title.lower()).strip("-")[:60] or "recipe"
        path = self.folder / f"{slug}.md"
        number = 2
        while path.exists():  # never over a recipe that's already there
            path = self.folder / f"{slug}-{number}.md"
            number += 1
        path.write_text(render_recipe(recipe), encoding="utf-8")
        recipe.path = path
        return path


# --- ingredients against what we have ----------------------------------------------------------

_UNITS = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons", "oz", "ounce", "ounces",
    "lb", "lbs", "pound", "pounds", "g", "gram", "grams", "kg", "ml", "l", "liter", "liters", "litre", "litres",
    "pinch", "dash", "clove", "cloves", "can", "cans", "stick", "sticks", "slice", "slices", "piece", "pieces",
    "bunch", "package", "pkg", "jar", "bottle", "handful", "sprig", "sprigs", "quart", "quarts", "pint", "pints", "packet", "packets",
}
_FILLER = {
    "of", "a", "an", "the", "to", "for", "fresh", "freshly", "dried", "large", "small", "medium",
    "chopped", "diced", "minced", "sliced", "grated", "peeled", "crushed", "finely", "roughly", "thinly",
    "optional", "taste", "about", "plus", "more", "good", "ripe", "cooked", "uncooked", "boneless", "skinless", "jumbo",
}


def ingredient_name(line: str) -> str:
    """The thing itself: "2 cups bomba rice" -> "bomba rice"; "1 lb large shrimp, peeled" -> "shrimp"."""
    text = re.sub(r"\([^)]*\)", " ", line.lower())
    text = text.split(",")[0]
    words = [
        word
        for word in re.findall(r"[a-zà-ÿ'-]+", text)
        if word not in _UNITS and word not in _FILLER and word.strip("-'")
    ]
    return " ".join(words) or line.strip().lower()


def _close(a: str, b: str) -> bool:
    return a == b or a.rstrip("s") == b.rstrip("s") or SequenceMatcher(None, a, b).ratio() >= 0.85


def on_hand(ingredient: str, pantry: list[str]) -> str | None:
    """The pantry item that covers ``ingredient``: every word of the item is in the ingredient ("Rice" covers
    "bomba rice"; "Egg noodles" doesn't cover "eggs"). The longest such item wins."""
    words = ingredient.split()
    found = [
        item
        for item in pantry
        if (item_words := re.findall(r"[a-zà-ÿ'-]+", item.lower())) and all(any(_close(w, have) for have in words) for w in item_words)
    ]
    return max(found, key=len) if found else None


_STAPLE_DESCRIBERS = {
    "black", "white", "ground", "kosher", "sea", "table", "coarse", "fine", "granulated", "all-purpose", "plain",
    "unsalted", "salted", "vegetable", "cooking", "extra", "virgin", "extra-virgin", "cold", "warm", "hot", "boiling",
}


def staple_for(ingredient: str, staples: list[str]) -> str | None:
    """The staple an ingredient is: the same thing, give or take a plain describer ("black pepper", "kosher salt").

    Stricter than grocery items: "pepper" isn't red bell pepper or cayenne pepper.
    """
    words = [word for word in ingredient.split() if word not in _STAPLE_DESCRIBERS and word not in ("and", "or")]
    for staple in sorted(staples, key=len, reverse=True):
        staple_words = staple.lower().split()
        if words and len(words) == len(staple_words) and all(_close(a, b) for a, b in zip(words, staple_words)):
            return staple
    if words and all(any(_close(word, staple.lower()) for staple in staples) for word in words):
        return " and ".join(words)  # "salt pepper" (salt and pepper to taste)
    return None


def duration_words(iso: str) -> str | None:
    """schema.org's "PT1H15M" as "1 hour 15 minutes"."""
    match = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", iso.strip().upper())
    if not match or not (match.group(1) or match.group(2)):
        return None
    hours, minutes = int(match.group(1) or 0), int(match.group(2) or 0)
    hours, minutes = hours + minutes // 60, minutes % 60
    parts = ([f"{hours} hour{'s' if hours != 1 else ''}"] if hours else []) + ([f"{minutes} minute{'s' if minutes != 1 else ''}"] if minutes else [])
    return " ".join(parts) or None


def recipe_from_html(html: str, url: str) -> Recipe | None:
    """The recipe a web page describes in its schema.org data (what search engines read), if it has one.

    Page content is data: it's cut to size and only ever shown, saved when asked, or compared with the list.
    """
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in _walk(data):
            kinds = node.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if "Recipe" not in kinds or not isinstance(node.get("recipeIngredient"), list):
                continue
            ingredients = [_clean(item) for item in node["recipeIngredient"][:MAX_INGREDIENTS] if isinstance(item, str) and _clean(item)]
            title = _clean(str(node.get("name") or ""))
            if not title or len(ingredients) < 2:
                continue
            serves = node.get("recipeYield")
            serves = serves[0] if isinstance(serves, list) and serves else serves
            keywords = node.get("keywords")
            tags = [tag.strip() for tag in keywords.split(",")] if isinstance(keywords, str) else [str(tag) for tag in keywords or [] if isinstance(tag, str)]
            return Recipe(
                title=title[:80],
                ingredients=ingredients,
                steps=_steps(node.get("recipeInstructions")),
                serves=_clean(str(serves))[:40] if serves else None,
                time=duration_words(str(node.get("totalTime") or "")),
                tags=[_clean(tag)[:30] for tag in tags[:6] if _clean(tag)],
                source=url,
            )
    return None


def _walk(data: Any):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _walk(value)
    elif isinstance(data, list):
        for value in data:
            yield from _walk(value)


def _clean(text: str) -> str:
    import html as html_lib  # noqa: PLC0415

    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).split())[:MAX_LINE]


def _steps(instructions: Any) -> list[str]:
    steps: list[str] = []
    if isinstance(instructions, str):
        steps = [part for part in (_clean(p) for p in re.split(r"\n+|(?<=\.)\s{2,}", instructions)) if part]
    elif isinstance(instructions, list):
        for item in instructions:
            if isinstance(item, str):
                steps.append(_clean(item))
            elif isinstance(item, dict) and isinstance(item.get("itemListElement"), list):
                steps += _steps(item["itemListElement"])
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                steps.append(_clean(item["text"]))
    return [step for step in steps if step][:40]


# --- the tool ----------------------------------------------------------------------------------


@dataclass
class _Last:
    """What a device was last told, for "add those" and "save that recipe"."""

    recipe: Recipe
    missing: list[str]
    from_web: bool


class RecipesService:
    def __init__(self, folder: Path, settings: RecipesConfig | None = None, search: Any = None, reader: Any = None) -> None:
        self.default_folder = folder
        self.settings = settings or RecipesConfig()
        self.search = search  # SearchService
        self.reader = reader  # PageReader
        self.groceries: ListTool | None = None
        self.tool_name = "recipes"
        self._last: dict[str, _Last] = {}

    @property
    def book(self) -> RecipeBook:
        folder = Path(self.settings.folder).expanduser() if self.settings.folder else self.default_folder
        return RecipeBook(folder)

    def update(self, settings: RecipesConfig | None, groceries: ListTool | None, tool_name: str) -> None:
        self.settings = settings or RecipesConfig()
        self.groceries = groceries
        self.tool_name = tool_name
        try:
            self.book.ensure()
        except OSError as exc:
            logger.warning("Recipes folder can't be made", extra={"event": "recipes_folder_failed", "reason": str(exc)})

    # What we have -----------------------------------------------------------------------

    async def pantry(self, now: datetime) -> list[str]:
        """Grocery items checked off within `have_days`. (Staples are matched on their own, more strictly.)"""
        have: list[str] = []
        if self.groceries is not None:
            items: list[ReminderItem] = await asyncio.to_thread(self.groceries.reminders.backend.all_items, self.groceries.list_name)
            since = now - timedelta(days=self.settings.have_days)
            have += [item.title for item in items if item.completed and (item.completed_at is None or item.completed_at >= since)]
        return have

    def compare(self, recipe: Recipe, pantry: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
        """(ingredient, the item that covers it) for what we have, and the ingredients we don't."""
        have: list[tuple[str, str]] = []
        missing: list[str] = []
        for line in recipe.ingredients:
            name = ingredient_name(line)
            covered = on_hand(name, pantry) or staple_for(name, self.settings.staples)
            if covered is not None:
                if all(name != known for known, _ in have):  # olive oil twice in a recipe is said once
                    have.append((name, covered))
            elif name not in missing:
                missing.append(name)
        return have, missing

    # Actions ----------------------------------------------------------------------------

    async def handle(self, arguments: dict[str, Any], device: str = "web", now: datetime | None = None) -> ToolOutcome:
        now = now or datetime.now()
        action = str(arguments.get("action") or "")
        asked = str(arguments.get("recipe") or "").strip()
        try:
            if action == "need":
                return await self._need(asked, device, now)
            if action == "suggest":
                return await self._suggest(now)
            if action == "show":
                return await self._show(asked, device)
            if action == "add_missing":
                return await self._add_missing(device)
            if action == "save":
                return self._save(device)
        except TimeoutError:
            return ToolOutcome(result={"error": "Reminders didn't answer"}, speak="The grocery list didn't answer.", error=True)
        except (LookupError, RuntimeError, OSError) as exc:
            logger.warning("Recipes failed", extra={"event": "recipes_failed", "kind": action, "reason": str(exc)})
            return ToolOutcome(result={"error": str(exc)}, speak="That didn't work.", error=True)
        return ToolOutcome(result={"error": f"unknown action {action!r}", "actions": ["need", "suggest", "show", "save"]}, error=True)

    async def _find(self, asked: str, device: str) -> tuple[Recipe | None, bool, ToolOutcome | None]:
        """The recipe asked for: from the folder, else the web. (recipe, from the web, or what to say instead)."""
        if not asked:
            last = self._last.get(device)
            if last is not None:
                return last.recipe, last.from_web, None
            return None, False, ToolOutcome(result={"error": "no recipe named"}, speak="Which recipe?", direct=True)
        matches = self.book.find(asked)
        if len(matches) > 1:
            names = [recipe.title for recipe in matches]
            question = f"Which recipe: {_join(names, 'or')}?"
            return None, False, ToolOutcome(result={"question": question, "options": names}, speak=question, pending=PendingQuestion(kind="clarify", question=question, options=names))
        if matches:
            return matches[0], False, None
        recipe = await self._from_web(asked)
        if recipe is None:
            return None, True, ToolOutcome(result={"error": f"no recipe found for {asked}"}, speak=f"I couldn't find a recipe for {asked}.", direct=True)
        return recipe, True, None

    async def _from_web(self, asked: str) -> Recipe | None:
        if self.search is None or self.reader is None:
            return None
        found = await self.search.search(f"{asked} recipe")
        results = found.result.get("results") if isinstance(found.result, dict) else None
        urls = [result.get("url") for result in (results or [])[:WEB_RESULTS_TRIED] if isinstance(result.get("url"), str)]
        # Fetched together (a slow page costs its own timeout once, not once per page); the best-ranked recipe wins.
        pages = await asyncio.gather(*(self.reader.html(url) for url in urls), return_exceptions=True)
        for url, html in zip(urls, pages):
            recipe = recipe_from_html(html, url) if isinstance(html, str) else None
            if recipe is not None:
                logger.info("Recipe found on the web", extra={"event": "recipe_web", "count": len(recipe.ingredients)})
                return recipe
        logger.info("No web recipe found", extra={"event": "recipe_web", "count": 0})
        return None

    async def _need(self, asked: str, device: str, now: datetime) -> ToolOutcome:
        recipe, from_web, instead = await self._find(asked, device)
        if recipe is None:
            assert instead is not None
            return instead
        have, missing = self.compare(recipe, await self.pantry(now))
        self._last[device] = _Last(recipe, missing, from_web)
        source = f" on {urlparse(recipe.source).hostname or 'the web'}" if from_web and recipe.source else ""
        opening = f"I found {recipe.title}{source}. " if from_web else ""
        # Names the grocery item it counted, when that isn't the ingredient's own name (it may be wrong).
        have_said = [name if _close(name, item.lower()) else f"{name} (your {item})" for name, item in have]
        result = {
            "recipe": recipe.title,
            "from": "the web" if from_web else "your recipes",
            "have": [{"ingredient": name, "counted": item} for name, item in have],
            "missing": missing,
        }
        if from_web:
            result["note"] = UNTRUSTED  # the recipe's words come from a web page, like search results
        if not missing:
            speak = f"{opening}You have everything for {recipe.title}."
            if from_web:
                question = "Save this to your recipes?"
                return ToolOutcome(
                    result=result,
                    speak=f"{speak} {question}",
                    direct=True,
                    pending=PendingQuestion(kind="yes_no", question=question, on_yes=(self.tool_name, {"action": "save"})),
                )
            return ToolOutcome(result=result, speak=speak, direct=True)
        have_part = f"you have {_join(have_said)}. You'd need" if have else "you'd need"
        question = "Add those to groceries?" if self.groceries is not None else None
        speak = f"{opening}For {recipe.title}, {have_part} {_join(missing)}."
        if question is None:
            return ToolOutcome(result=result, speak=speak, direct=True)
        return ToolOutcome(
            result=result,
            speak=f"{speak} {question}",
            direct=True,
            pending=PendingQuestion(kind="yes_no", question=question, on_yes=(self.tool_name, {"action": "add_missing"})),
        )

    async def _add_missing(self, device: str) -> ToolOutcome:
        last = self._last.get(device)
        if last is None or not last.missing or self.groceries is None:
            return ToolOutcome(result={"error": "nothing to add"}, speak="There's nothing to add.", direct=True)
        # All at once: the grocery list is read once, and an ingredient with "and" in it stays one item.
        speak = (await self.groceries.handle_many("need", list(last.missing))).speak or ""
        last.missing = []
        if last.from_web and last.recipe.path is None:
            question = "Save this recipe to your recipes?"
            return ToolOutcome(
                result={"added": True},
                speak=f"{speak} {question}",
                direct=True,
                pending=PendingQuestion(kind="yes_no", question=question, on_yes=(self.tool_name, {"action": "save"})),
            )
        return ToolOutcome(result={"added": True}, speak=speak, direct=True)

    def _save(self, device: str) -> ToolOutcome:
        last = self._last.get(device)
        if last is None or not last.from_web:
            return ToolOutcome(result={"error": "no web recipe to save"}, speak="There's no web recipe to save.", direct=True)
        if last.recipe.path is not None:
            return ToolOutcome(result={"saved": last.recipe.title}, speak=f"{last.recipe.title} is already in your recipes.", direct=True)
        path = self.book.save(last.recipe)
        logger.info("Recipe saved", extra={"event": "recipe_saved", "names": [path.name]})
        return ToolOutcome(result={"saved": last.recipe.title, "file": path.name}, speak=f"Saved {last.recipe.title} to your recipes.", direct=True)

    async def _suggest(self, now: datetime) -> ToolOutcome:
        recipes = self.book.load()
        if not recipes:
            return ToolOutcome(result={"error": "no recipes"}, speak="There are no recipes in your recipes folder yet.", direct=True)
        pantry = await self.pantry(now)
        ranked = sorted(((len(self.compare(recipe, pantry)[1]), recipe.title.lower(), recipe) for recipe in recipes), key=lambda entry: entry[:2])
        dishes = [{"recipe": recipe.title, "missing": self.compare(recipe, pantry)[1]} for _count, _title, recipe in ranked[:MAX_SUGGESTIONS]]
        return ToolOutcome(
            result={
                "can_make": dishes,
                "note": "Suggest only these recipes, fewest missing first; say what each is missing (nothing missing: we have everything).",
            }
        )

    async def _show(self, asked: str, device: str) -> ToolOutcome:
        recipe, from_web, instead = await self._find(asked, device)
        if recipe is None:
            assert instead is not None
            return instead
        if device not in self._last or self._last[device].recipe is not recipe:
            self._last[device] = _Last(recipe, [], from_web)
        return ToolOutcome(
            result={"recipe": recipe.title, "ingredients": len(recipe.ingredients), "steps": len(recipe.steps)} | ({"note": UNTRUSTED} if from_web else {}),
            speak=f"Here's {recipe.title}." + (" Say “save that recipe” to keep it." if from_web and recipe.path is None else ""),
            chat=render_recipe(recipe),
            chat_title=recipe.title,
            direct=True,
        )
