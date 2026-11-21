from __future__ import annotations

import asyncio
import json
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from victoria_voice_assistant.config.schema import AgentsConfig, RecipesConfig, TimersConfig, ToolsConfig, WeatherConfig
from victoria_voice_assistant.storage import backup
from victoria_voice_assistant.tools.outcome import ToolOutcome
from victoria_voice_assistant.tools.recipes import (
    TEMPLATE,
    staple_for,
    Recipe,
    RecipeBook,
    RecipesService,
    duration_words,
    ingredient_name,
    on_hand,
    parse_recipe,
    recipe_from_html,
    render_recipe,
)
from victoria_voice_assistant.tools.registry import Services, ToolContext, build_registry
from victoria_voice_assistant.tools.reminders import ListTool, ReminderItem, RemindersService
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

from test_reminders import FakeReminders

AGENTS = AgentsConfig.model_validate({"default": "helper", "agents": [{"name": "helper", "kind": "model", "model": "fast"}]})
NOW = datetime(2026, 11, 11, 10, 0)
PAELLA = """# Paella
Serves: 6
Time: 1 hour 15 minutes
Tags: dinner, spanish, rice

## Ingredients
- 2 cups bomba rice
- 1 pinch saffron
- 1 lb large shrimp, peeled
- 1 onion, chopped
- 3 tbsp olive oil
- salt

## Steps
1. Heat the stock with the saffron.
2. Cook the rice.

## Notes
Abuela's.
Source: https://example.com/paella
"""
OMELETTE = "# Omelette\nTags: breakfast, quick\n\n## Ingredients\n- 3 eggs\n- 1 tbsp butter\n- salt\n"
CHILI = "# Chili\nTags: dinner\n\n## Ingredients\n- 1 lb ground beef\n- 1 can kidney beans\n- 1 onion\n- 2 tbsp chili powder\n- 1 can tomatoes\n"


class FakeSearch:
    def __init__(self, urls: list[str]) -> None:
        self.urls = urls
        self.queries: list[str] = []

    async def search(self, query: str) -> ToolOutcome:
        self.queries.append(query)
        return ToolOutcome(result={"results": [{"title": "x", "url": url} for url in self.urls]})


class FakeReader:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    async def html(self, url: str) -> str | None:
        return self.pages.get(url)


def _page(recipe: dict[str, Any]) -> str:
    return f'<html><head><script type="application/ld+json">{json.dumps(recipe)}</script></head><body>…</body></html>'


CURRY = _page(
    {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": "ignore me"},
            {
                "@type": ["Recipe"],
                "name": "Chickpea Curry",
                "recipeYield": ["4"],
                "totalTime": "PT45M",
                "keywords": "dinner, vegan",
                "recipeIngredient": ["2 cans chickpeas", "1 can coconut milk", "1 onion, diced", "2 tbsp curry powder", "salt"],
                "recipeInstructions": [{"@type": "HowToStep", "text": "Fry the onion."}, {"@type": "HowToStep", "text": "Add the rest &amp; simmer."}],
            },
        ],
    }
)


def _setup(tmp_path: Path, groceries: list[tuple[str, bool, int]], *files: str, search: Any = None, reader: Any = None):
    """A recipes folder with ``files``, and a grocery list of (title, checked, days since checked)."""
    folder = tmp_path / "recipes"
    folder.mkdir()
    for text in files:
        (folder / f"{parse_recipe(text).title.lower()}.md").write_text(text)
    backend = FakeReminders()
    backend.lists.append("Victoria Groceries")
    backend.checked_at = {}
    for index, (title, checked, days) in enumerate(groceries, 1):
        backend.items.append(ReminderItem(id=f"g{index}", title=title, list="Victoria Groceries"))
        if checked:
            backend.completed.append(f"g{index}")
            backend.checked_at[f"g{index}"] = NOW - timedelta(days=days)
    original = backend.all_items

    def all_items(list_name: str) -> list[ReminderItem]:
        items = original(list_name)
        for item in items:
            item.completed_at = backend.checked_at.get(item.id)
        return items

    backend.all_items = all_items  # type: ignore[method-assign]
    service = RecipesService(folder, RecipesConfig(), search=search, reader=reader)
    service.update(RecipesConfig(), ListTool(RemindersService(None, backend=backend), "Victoria Groceries"), "recipes")
    return service, backend, folder


def ask(service: RecipesService, device: str = "web", **arguments: Any) -> ToolOutcome:
    return asyncio.run(service.handle(arguments, device=device, now=NOW))


# The template ---------------------------------------------------------------------------


def test_a_recipe_file_is_read_from_the_template() -> None:
    recipe = parse_recipe(PAELLA)
    assert recipe.title == "Paella" and recipe.serves == "6" and recipe.time == "1 hour 15 minutes"
    assert recipe.tags == ["dinner", "spanish", "rice"]
    assert recipe.ingredients[0] == "2 cups bomba rice" and len(recipe.ingredients) == 6
    assert recipe.steps == ["Heat the stock with the saffron.", "Cook the rice."]
    assert recipe.notes == "Abuela's." and recipe.source == "https://example.com/paella"


def test_only_the_title_and_ingredients_are_required() -> None:
    assert parse_recipe("# Toast\n\n## Ingredients\n- bread\n").ingredients == ["bread"]
    with pytest.raises(ValueError, match="no title"):
        parse_recipe("## Ingredients\n- bread\n")
    with pytest.raises(ValueError, match="Ingredients"):
        parse_recipe("# Toast\nJust toast it.\n")


def test_a_recipe_written_out_reads_back_the_same() -> None:
    recipe = parse_recipe(PAELLA)
    again = parse_recipe(render_recipe(recipe))
    assert (again.title, again.ingredients, again.steps, again.tags, again.source) == (recipe.title, recipe.ingredients, recipe.steps, recipe.tags, recipe.source)
    assert parse_recipe(TEMPLATE).title == "Recipe name"  # the template itself fits the template


def test_the_folder_gets_a_template_and_skips_files_that_dont_fit(tmp_path: Path) -> None:
    book = RecipeBook(tmp_path / "recipes")
    book.ensure()
    (book.folder / "paella.md").write_text(PAELLA)
    (book.folder / "broken.md").write_text("no heading here\n")
    assert (book.folder / "_template.md").read_text() == TEMPLATE
    assert [recipe.title for recipe in book.load()] == ["Paella"]  # not the template, not the broken one
    assert book.problems[0][0] == "broken.md" and "no title" in book.problems[0][1]


# Ingredients against what we have -----------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("2 cups bomba rice", "bomba rice"),
        ("1 lb large shrimp, peeled", "shrimp"),
        ("3 tbsp olive oil", "olive oil"),
        ("1 pinch saffron", "saffron"),
        ("1 onion, chopped", "onion"),
        ("1 (14 oz) can diced tomatoes", "tomatoes"),
        ("salt and pepper to taste", "salt and pepper"),
        ("½ cup freshly grated parmesan", "parmesan"),
    ],
)
def test_an_ingredient_line_gives_the_thing_itself(line: str, name: str) -> None:
    assert ingredient_name(line) == name


def test_an_item_covers_an_ingredient_when_all_its_words_are_there() -> None:
    pantry = ["Rice", "Egg noodles", "Onions", "olive oil", "oil"]
    assert on_hand("bomba rice", pantry) == "Rice"
    assert on_hand("onion", pantry) == "Onions"
    assert on_hand("olive oil", pantry) == "olive oil"  # the longest match
    assert on_hand("eggs", pantry) is None  # Egg noodles isn't eggs
    assert on_hand("saffron", pantry) is None


# What do I need to make ... ---------------------------------------------------------------


def test_need_says_what_we_have_and_lack_and_offers_to_add_it(tmp_path: Path) -> None:
    service, backend, _ = _setup(tmp_path, [("Rice", True, 2), ("Onions", True, 3), ("Shrimp", False, 0)], PAELLA)
    outcome = ask(service, action="need", recipe="paella")
    assert outcome.speak == (
        "For Paella, you have bomba rice (your Rice), onion, olive oil and salt. You'd need saffron and shrimp. Add those to groceries?"
    )
    assert outcome.direct and outcome.pending is not None and outcome.pending.on_yes == ("recipes", {"action": "add_missing"})
    assert outcome.result["missing"] == ["saffron", "shrimp"]
    # "Yes": the missing ones go on the grocery list. Shrimp was already there, unchecked.
    added = ask(service, action="add_missing")
    assert added.speak == "I've added “Saffron” to Victoria Groceries. “Shrimp” is already on Victoria Groceries."
    assert [item.title for item in backend.items][-1] == "Saffron"
    assert ask(service, action="add_missing").speak == "There's nothing to add."


def test_something_checked_off_long_ago_no_longer_counts(tmp_path: Path) -> None:
    service, _, _ = _setup(tmp_path, [("Rice", True, 30), ("Onions", True, 1)], PAELLA)
    assert "bomba rice" in ask(service, action="need", recipe="paella").result["missing"]


def test_having_everything_says_so(tmp_path: Path) -> None:
    service, _, _ = _setup(tmp_path, [("Eggs", True, 1)], OMELETTE)
    outcome = ask(service, action="need", recipe="omelette")
    assert outcome.speak == "You have everything for Omelette." and outcome.pending is None


def test_a_recipe_is_found_by_a_misspelled_name_or_a_tag_and_asks_when_two_fit(tmp_path: Path) -> None:
    service, _, _ = _setup(tmp_path, [], PAELLA, OMELETTE, CHILI)
    assert ask(service, action="need", recipe="paela").result["recipe"] == "Paella"
    assert ask(service, action="need", recipe="breakfast").result["recipe"] == "Omelette"
    outcome = ask(service, action="need", recipe="dinner")
    assert outcome.speak == "Which recipe: Paella or Chili?" or outcome.speak == "Which recipe: Chili or Paella?"
    assert outcome.pending is not None and outcome.pending.kind == "clarify"


# The web, when the folder has nothing close --------------------------------------------------


def test_nothing_close_in_the_folder_goes_to_the_web(tmp_path: Path) -> None:
    search = FakeSearch(["https://blog.example/no-recipe", "https://food.example/chickpea-curry"])
    reader = FakeReader({"https://blog.example/no-recipe": "<html>nothing</html>", "https://food.example/chickpea-curry": CURRY})
    service, backend, folder = _setup(tmp_path, [("Onions", True, 1)], PAELLA, search=search, reader=reader)
    outcome = ask(service, action="need", recipe="chickpea curry")
    assert search.queries == ["chickpea curry recipe"]
    assert outcome.speak == (
        "I found Chickpea Curry on food.example. For Chickpea Curry, you have onion and salt. "
        "You'd need chickpeas, coconut milk and curry powder. Add those to groceries?"
    )
    assert not (folder / "chickpea-curry.md").exists()  # never saved without being asked
    # Yes to the groceries, then it offers to save; yes writes it in the template.
    added = ask(service, action="add_missing")
    assert added.speak.endswith("Save this recipe to your recipes?") and added.pending.on_yes == ("recipes", {"action": "save"})
    assert ask(service, action="save").speak == "Saved Chickpea Curry to your recipes."
    saved = parse_recipe((folder / "chickpea-curry.md").read_text())
    assert saved.source == "https://food.example/chickpea-curry" and saved.time == "45 minutes" and saved.serves == "4"
    assert saved.steps == ["Fry the onion.", "Add the rest & simmer."]
    assert ask(service, action="save").speak == "Chickpea Curry is already in your recipes."
    # Next time it comes from the folder.
    assert ask(service, action="need", recipe="chickpea curry").result["from"] == "your recipes"
    assert len(search.queries) == 1


def test_no_recipe_anywhere_is_said(tmp_path: Path) -> None:
    service, _, _ = _setup(tmp_path, [], PAELLA, search=FakeSearch(["https://blog.example/x"]), reader=FakeReader({}))
    assert ask(service, action="need", recipe="moon cheese souffle").speak == "I couldn't find a recipe for moon cheese souffle."
    assert ask(service, action="save").speak == "There's no web recipe to save."


def test_a_web_page_without_recipe_data_isnt_a_recipe() -> None:
    assert recipe_from_html("<html><script type='application/ld+json'>{\"@type\": \"Article\"}</script></html>", "https://x.example") is None
    assert recipe_from_html("<html><script type='application/ld+json'>not json</script></html>", "https://x.example") is None
    assert duration_words("PT1H15M") == "1 hour 15 minutes" and duration_words("PT90M") == "1 hour 30 minutes" and duration_words("soon") is None


# What can I make? -------------------------------------------------------------------------


def test_suggest_ranks_the_folders_recipes_by_what_they_lack(tmp_path: Path) -> None:
    service, _, _ = _setup(tmp_path, [("Eggs", True, 1), ("Onions", True, 1), ("Rice", True, 1)], PAELLA, OMELETTE, CHILI)
    outcome = ask(service, action="suggest")
    assert outcome.speak is None  # the model phrases it, from only these
    assert outcome.result["can_make"] == [
        {"recipe": "Omelette", "missing": []},
        {"recipe": "Paella", "missing": ["saffron", "shrimp"]},
        {"recipe": "Chili", "missing": ["ground beef", "kidney beans", "chili powder", "tomatoes"]},
    ]


def test_suggest_with_an_empty_folder_says_so(tmp_path: Path) -> None:
    service, _, _ = _setup(tmp_path, [])
    assert ask(service, action="suggest").speak == "There are no recipes in your recipes folder yet."


# Show --------------------------------------------------------------------------------------


def test_show_puts_the_whole_recipe_in_the_chat(tmp_path: Path) -> None:
    service, _, _ = _setup(tmp_path, [], PAELLA)
    outcome = ask(service, action="show", recipe="paella")
    assert outcome.speak == "Here's Paella." and outcome.chat_title == "Paella"
    assert "## Ingredients\n- 2 cups bomba rice" in outcome.chat and "## Steps\n1. Heat the stock" in outcome.chat
    # "Show me the recipe" after asking about one, on the same device only.
    ask(service, device="kitchen", action="need", recipe="paella")
    assert ask(service, device="kitchen", action="show").speak == "Here's Paella."
    assert ask(service, device="office", action="show").speak == "Which recipe?"


# Registry, config, backup --------------------------------------------------------------------

RECIPES_TOOL = {"name": "recipes", "description": "Recipes.", "kind": "builtin", "service": "recipes",
                "params": {"action": {"type": "string", "required": True}, "recipe": {"type": "string"}}}
GROCERIES_TOOL = {"name": "groceries", "description": "The grocery list.", "kind": "builtin", "service": "reminders.list",
                  "list": "Victoria Groceries", "params": {"action": {"type": "string", "required": True}, "item": {"type": "string"}}}


def test_the_registry_builds_it_on_the_groceries_tool(tmp_path: Path) -> None:
    folder = tmp_path / "recipes"
    reminders = RemindersService(None, backend=FakeReminders())
    services = Services(
        timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})),
        reminders=reminders, recipes=RecipesService(folder),
    )
    tools = ToolsConfig.model_validate({"tools": [GROCERIES_TOOL, RECIPES_TOOL]})
    entry = build_registry(tools, AGENTS, services).get("recipes")
    assert entry is not None and entry.status == "ready"
    assert (folder / "_template.md").is_file()  # the folder and its template are made when the tool loads
    (folder / "omelette.md").write_text(OMELETTE)
    outcome = asyncio.run(entry.handler({"action": "need", "recipe": "omelette"}, ToolContext(device="kitchen")))
    assert outcome.result["missing"] == ["eggs"]
    # Without the groceries tool, or without the service, it's a stub that says why.
    alone = build_registry(ToolsConfig.model_validate({"tools": [RECIPES_TOOL]}), AGENTS, services).get("recipes")
    assert alone is not None and alone.status == "auto_stub" and "`groceries` tool" in (alone.reason or "")
    services.recipes = None
    assert build_registry(tools, AGENTS, services).get("recipes").status == "auto_stub"


def test_the_folder_can_be_set_in_config(tmp_path: Path) -> None:
    elsewhere = tmp_path / "shared" / "Recipes"
    service = RecipesService(tmp_path / "recipes", RecipesConfig(folder=str(elsewhere)))
    service.update(RecipesConfig(folder=str(elsewhere)), None, "recipes")
    assert (elsewhere / "_template.md").is_file() and not (tmp_path / "recipes").exists()
    with pytest.raises(ValueError):
        RecipesConfig(have_days=0)


def test_the_backup_carries_the_recipes_folder_and_puts_it_back(tmp_path: Path) -> None:
    database = tmp_path / "data" / "assistant.sqlite3"
    (database.parent / "recipes").mkdir(parents=True)
    (database.parent / "recipes" / "paella.md").write_text(PAELLA)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    archive = backup.create(config_dir, database, tmp_path / "backups")
    with tarfile.open(archive) as tar:
        assert "recipes/paella.md" in tar.getnames()
    (database.parent / "recipes" / "paella.md").write_text("# Ruined\n")
    (database.parent / "recipes" / "newer.md").write_text(OMELETTE)
    backup.restore(archive, config_dir, database, tmp_path / "backups")
    assert (database.parent / "recipes" / "paella.md").read_text() == PAELLA
    assert (database.parent / "recipes" / "newer.md").exists()  # written since the backup: kept


def test_a_staple_is_only_the_same_thing() -> None:
    staples = RecipesConfig().staples
    assert staple_for("black pepper", staples) == "pepper"
    assert staple_for("kosher salt", staples) == "salt"
    assert staple_for("extra virgin olive oil", staples) == "olive oil"
    assert staple_for("salt and pepper", staples) == "salt and pepper"
    assert staple_for("red bell pepper", staples) is None
    assert staple_for("cayenne pepper", staples) is None
    assert staple_for("peanut butter", staples) is None
    assert staple_for("coconut oil", staples) is None


def test_an_ingredient_listed_twice_is_said_once(tmp_path: Path) -> None:
    twice = "# Fry-up\n\n## Ingredients\n- 1 tbsp olive oil\n- 2 tbsp olive oil, for frying\n- 1 red bell pepper\n"
    service, _, _ = _setup(tmp_path, [], twice)
    outcome = ask(service, action="need", recipe="fry-up")
    assert outcome.speak == "For Fry-up, you have olive oil. You'd need red bell pepper. Add those to groceries?"


# From the code review, 2026-11-12 ----------------------------------------------------


def test_web_recipe_text_is_marked_untrusted_and_folder_recipes_arent(tmp_path: Path) -> None:
    from victoria_voice_assistant.search.service import UNTRUSTED

    search = FakeSearch(["https://food.example/chickpea-curry"])
    service, _, _ = _setup(tmp_path, [], PAELLA, search=search, reader=FakeReader({"https://food.example/chickpea-curry": CURRY}))
    assert ask(service, action="need", recipe="chickpea curry").result["note"] == UNTRUSTED
    assert ask(service, action="show", recipe="chickpea curry").result["note"] == UNTRUSTED
    assert "note" not in ask(service, action="need", recipe="paella").result


def test_recipe_pages_are_fetched_together_and_the_best_ranked_one_wins(tmp_path: Path) -> None:
    other = CURRY.replace("Chickpea Curry", "Second Curry")

    class SlowFirst:
        def __init__(self) -> None:
            self.running = self.most = 0

        async def html(self, url: str) -> str | None:
            self.running += 1
            self.most = max(self.most, self.running)
            await asyncio.sleep(0.05 if "first" in url else 0.0)  # the best-ranked page answers last
            self.running -= 1
            if "broken" in url:
                raise RuntimeError("boom")
            return CURRY if "first" in url else other

    reader = SlowFirst()
    search = FakeSearch(["https://a.example/broken", "https://b.example/first", "https://c.example/second"])
    service, _, _ = _setup(tmp_path, [], search=search, reader=reader)
    assert ask(service, action="need", recipe="curry").result["recipe"] == "Chickpea Curry"
    assert reader.most == 3  # all at once, not one after another


def test_adding_missing_ingredients_reads_the_grocery_list_once(tmp_path: Path) -> None:
    service, backend, _ = _setup(tmp_path, [], "# Dip\n\n## Ingredients\n- 1 cup sour cream\n- 1 packet onion soup mix\n- chips and salsa\n")
    ask(service, action="need", recipe="dip")
    reads: list[str] = []
    original = backend.all_items
    backend.all_items = lambda name: (reads.append(name), original(name))[1]  # type: ignore[method-assign]
    added = ask(service, action="add_missing")
    assert reads == ["Victoria Groceries"]
    assert added.speak == "I've added “Sour cream”, “Onion soup mix” and “Chips and salsa” to Victoria Groceries."
