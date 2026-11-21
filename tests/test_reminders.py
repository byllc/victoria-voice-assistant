from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from victoria_voice_assistant.config.schema import AgentsConfig, RemindersConfig, TimersConfig, ToolsConfig, WeatherConfig
from victoria_voice_assistant.tools.reminders import Access, MissingList, ReminderItem, RemindersService, best_matches, parse_due, speak_due
from victoria_voice_assistant.tools.registry import Services, ToolContext, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

AGENTS = AgentsConfig.model_validate({"default": "helper", "agents": [{"name": "helper", "kind": "model", "model": "fast"}]})
# Wednesday 2026-11-11, 10:00 local.
NOW = datetime(2026, 11, 11, 10, 0)
TOOL = {
    "name": "reminders",
    "description": "Apple Reminders.",
    "kind": "builtin",
    "service": "reminders",
    "params": {
        "action": {"type": "string", "enum": ["add", "list", "complete"], "required": True},
        "title": {"type": "string"},
        "due": {"type": "string"},
        "list": {"type": "string"},
        "when": {"type": "string", "enum": ["today", "overdue", "this_week"]},
    },
}


class FakeReminders:
    """Reminders replaced: lists with items, including a private list the tool must never reach."""

    def __init__(self, access: Access = "granted") -> None:
        self._access: Access = access
        self.asked = 0
        self.items: list[ReminderItem] = []
        self.completed: list[str] = []
        self.created: list[str] = []
        self.lists = ["Shopping", "Household", "Private"]

    def access(self) -> Access:
        return self._access

    def request_access(self) -> bool:
        self.asked += 1
        self._access = "granted"
        return True

    def list_names(self) -> list[str]:
        return list(self.lists)

    def incomplete(self, lists: list[str], due_before: datetime | None) -> list[ReminderItem]:
        return [
            item
            for item in self.items
            if item.list in lists and item.id not in self.completed and (due_before is None or (item.due is not None and item.due < due_before))
        ]

    def add(self, list_name: str, title: str, due: datetime | None, has_time: bool) -> ReminderItem:
        if list_name not in self.lists:
            raise MissingList(list_name, len(self.lists))
        item = ReminderItem(id=f"r{len(self.items) + 1}", title=title, list=list_name, due=due, has_time=has_time)
        self.items.append(item)
        return item

    def complete(self, item_id: str) -> None:
        self.completed.append(item_id)

    def default_list_name(self) -> str | None:
        return "Household"

    def all_items(self, list_name: str) -> list[ReminderItem]:
        if list_name not in self.lists:
            raise MissingList(list_name, len(self.lists))
        return [
            ReminderItem(id=i.id, title=i.title, list=i.list, due=i.due, has_time=i.has_time, completed=i.id in self.completed)
            for i in self.items
            if i.list == list_name
        ]

    def set_completed(self, item_id: str, completed: bool) -> None:
        if completed and item_id not in self.completed:
            self.completed.append(item_id)
        elif not completed and item_id in self.completed:
            self.completed.remove(item_id)

    def delete(self, item_id: str) -> None:
        self.items = [i for i in self.items if i.id != item_id]

    def create_list(self, name: str) -> str:
        self.created.append(name)
        self.lists.append(name)
        return "on this Mac"


SETTINGS = RemindersConfig(lists=["Shopping", "Household"], default_list="Household")


def _service(backend: FakeReminders | None = None) -> tuple[RemindersService, FakeReminders]:
    backend = backend or FakeReminders()
    return RemindersService(SETTINGS, backend=backend), backend


def run(service: RemindersService, **arguments: Any):
    return asyncio.run(service.handle(arguments, now=NOW))


# Spoken due times ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "due", "has_time"),
    [
        ("tomorrow at 9", datetime(2026, 11, 12, 9, 0), True),
        ("tomorrow at 9 pm", datetime(2026, 11, 12, 21, 0), True),
        ("today at 5:30pm", datetime(2026, 11, 11, 17, 30), True),
        ("at 3", datetime(2026, 11, 11, 15, 0), True),  # a bare "at 3" is the afternoon
        ("at 9 am", datetime(2026, 11, 12, 9, 0), True),  # already past today: tomorrow
        ("tonight", datetime(2026, 11, 11, 20, 0), True),
        ("tomorrow morning", datetime(2026, 11, 12, 9, 0), True),
        ("tomorrow at noon", datetime(2026, 11, 12, 12, 0), True),
        ("Friday", datetime(2026, 11, 13, 0, 0), False),
        ("Wednesday", datetime(2026, 11, 18, 0, 0), False),  # said on a Wednesday: next week's
        ("friday at 6 in the evening", datetime(2026, 11, 13, 18, 0), True),
        ("in 2 hours", datetime(2026, 11, 11, 12, 0), True),
        ("in half an hour", datetime(2026, 11, 11, 10, 30), True),
        ("November 20", datetime(2026, 11, 20, 0, 0), False),
        ("November 20 at 3", datetime(2026, 11, 20, 15, 0), True),
        ("the 5th", datetime(2026, 12, 5, 0, 0), False),
        ("January 2nd", datetime(2027, 1, 2, 0, 0), False),
        ("this weekend", datetime(2026, 11, 14, 0, 0), False),
    ],
)
def test_spoken_due_times_are_read_in_local_time(said: str, due: datetime, has_time: bool) -> None:
    assert parse_due(said, NOW) == (due, has_time)


@pytest.mark.parametrize("said", ["", "whenever", "soonish"])
def test_unreadable_due_times_are_none(said: str) -> None:
    assert parse_due(said, NOW) is None


def test_due_times_are_spoken_relative_to_today() -> None:
    assert speak_due(datetime(2026, 11, 12, 9, 0), True, NOW) == "tomorrow at 9 AM"
    assert speak_due(datetime(2026, 11, 11, 17, 30), True, NOW) == "today at 5:30 PM"
    assert speak_due(datetime(2026, 11, 13), False, NOW) == "Friday"
    assert speak_due(datetime(2026, 11, 20), False, NOW) == "November 20"


# Adding ------------------------------------------------------------------------------------


def test_add_goes_to_the_default_list_with_its_due_time() -> None:
    service, backend = _service()
    outcome = run(service, action="add", title="call the plumber", due="tomorrow at 9")
    assert backend.items == [ReminderItem(id="r1", title="call the plumber", list="Household", due=datetime(2026, 11, 12, 9, 0), has_time=True)]
    assert outcome.speak == "I've added “call the plumber” to Household, due tomorrow at 9 AM."


def test_add_to_a_named_list_by_its_spoken_name() -> None:
    service, backend = _service()
    outcome = run(service, action="add", title="milk", list="the shopping list")
    assert backend.items[0].list == "Shopping"
    assert backend.items[0].due is None
    assert outcome.speak == "I've added “milk” to Shopping."


def test_a_list_not_named_in_config_is_never_reached() -> None:
    service, backend = _service()
    outcome = run(service, action="add", title="secret", list="Private")
    assert backend.items == []
    assert outcome.speak == "I can only use these lists: Shopping and Household."
    backend.items.append(ReminderItem(id="p1", title="secret", list="Private"))
    assert run(service, action="list").result == {"reminders": []}
    assert run(service, action="complete", title="secret").speak == "I couldn't find “secret” on your lists."
    assert backend.completed == []


def test_an_unreadable_due_time_asks_instead_of_guessing() -> None:
    service, backend = _service()
    outcome = run(service, action="add", title="call mom", due="whenever")
    assert backend.items == []
    assert outcome.speak == "When is that due? I didn't catch “whenever”."


def test_a_list_missing_from_reminders_is_said_not_raised() -> None:
    backend = FakeReminders()
    backend.lists = ["Shopping"]
    service, _ = _service(backend)
    outcome = run(service, action="add", title="fix the gate")  # default list Household is gone
    assert outcome.error and outcome.speak == "There's no Household list in Reminders."


# Listing -----------------------------------------------------------------------------------


def _stocked() -> tuple[RemindersService, FakeReminders]:
    service, backend = _service()
    backend.items = [
        ReminderItem(id="1", title="milk", list="Shopping"),
        ReminderItem(id="2", title="eggs", list="Shopping"),
        ReminderItem(id="3", title="pay the water bill", list="Household", due=datetime(2026, 11, 10), has_time=False),
        ReminderItem(id="4", title="call the plumber", list="Household", due=datetime(2026, 11, 11, 15, 0), has_time=True),
        ReminderItem(id="5", title="change the furnace filter", list="Household", due=datetime(2026, 11, 14), has_time=False),
        ReminderItem(id="6", title="renew the passport", list="Household", due=datetime(2026, 12, 1), has_time=False),
    ]
    return service, backend


def test_list_one_list() -> None:
    service, _ = _stocked()
    assert run(service, action="list", list="shopping").result == {"reminders": [{"title": "milk", "list": "Shopping"}, {"title": "eggs", "list": "Shopping"}]}


def test_list_due_today_includes_overdue_and_says_when() -> None:
    service, _ = _stocked()
    found = run(service, action="list", when="today").result["reminders"]
    assert found == [
        {"title": "pay the water bill", "list": "Household", "due": "yesterday"},
        {"title": "call the plumber", "list": "Household", "due": "today at 3 PM"},
    ]


def test_list_overdue_and_this_week() -> None:
    service, _ = _stocked()
    assert [r["title"] for r in run(service, action="list", when="overdue").result["reminders"]] == ["pay the water bill"]
    week = [r["title"] for r in run(service, action="list", when="this_week").result["reminders"]]
    assert week == ["pay the water bill", "call the plumber", "change the furnace filter"]


def test_an_empty_answer_is_spoken_directly() -> None:
    service, _ = _service()
    assert run(service, action="list", list="Shopping").speak == "Nothing on Shopping."
    assert run(service, action="list", when="overdue").speak == "Nothing overdue on your lists."


def test_a_long_list_is_cut_short_and_says_how_many_more() -> None:
    service, backend = _service()
    backend.items = [ReminderItem(id=str(i), title=f"item {i}", list="Shopping") for i in range(20)]
    result = run(service, action="list").result
    assert len(result["reminders"]) == 15 and result["more"] == 5


# Completing --------------------------------------------------------------------------------


def test_complete_by_a_word_from_the_title() -> None:
    service, backend = _stocked()
    outcome = run(service, action="complete", title="the plumber")
    assert backend.completed == ["4"]
    assert outcome.speak == "Marked “call the plumber” done."


def test_complete_asks_which_when_several_match() -> None:
    service, backend = _service()
    backend.items = [ReminderItem(id="1", title="buy milk", list="Shopping"), ReminderItem(id="2", title="buy oat milk", list="Shopping")]
    outcome = run(service, action="complete", title="milk")
    assert backend.completed == []
    assert outcome.speak == "Which one: buy milk or buy oat milk?"
    assert outcome.pending is not None and outcome.pending.kind == "clarify"
    # The answer names it exactly, and an exact title wins outright.
    assert run(service, action="complete", title="buy milk").speak == "Marked “buy milk” done."
    assert backend.completed == ["1"]


def test_nothing_is_deleted_or_edited() -> None:
    service, backend = _stocked()
    outcome = run(service, action="delete", title="milk")
    assert outcome.error and len(backend.items) == 6 and backend.completed == []


def test_best_matches_ignores_filler_words_and_plurals() -> None:
    items = [ReminderItem(id="1", title="Eggs", list="Shopping"), ReminderItem(id="2", title="milk", list="Shopping")]
    assert [item.id for item in best_matches(items, "the egg")] == ["1"]
    assert best_matches(items, "bread") == []


# Access and the registry -------------------------------------------------------------------


def _services(reminders: RemindersService | None) -> Services:
    return Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})), reminders=reminders)


def _tools(section: bool = True) -> ToolsConfig:
    data: dict[str, Any] = {"tools": [TOOL]}
    if section:
        data["reminders"] = {"lists": ["Shopping", "Household"], "default_list": "Household"}
    return ToolsConfig.model_validate(data)


def test_without_access_the_tool_is_an_automatic_stub_and_access_is_asked_once() -> None:
    backend = FakeReminders(access="not_determined")
    service = RemindersService(None, backend=backend)
    granted: list[bool] = []
    service.on_granted = lambda: granted.append(True)
    entry = build_registry(_tools(), AGENTS, _services(service)).get("reminders")
    assert entry is not None and entry.status == "auto_stub"
    assert "Privacy & Security > Reminders" in (entry.reason or "")
    for _ in range(100):  # the question is answered on another thread
        if granted:
            break
        asyncio.run(asyncio.sleep(0.01))
    assert backend.asked == 1 and granted == [True]
    # Rebuilt once access arrives (the app reloads the tools): now it works.
    entry = build_registry(_tools(), AGENTS, _services(service)).get("reminders")
    assert entry is not None and entry.status == "ready"
    outcome = asyncio.run(entry.handler({"action": "add", "title": "milk", "list": "Shopping"}, ToolContext(device="web")))
    assert outcome.speak == "I've added “milk” to Shopping."


def test_denied_access_isnt_asked_again() -> None:
    backend = FakeReminders(access="denied")
    entry = build_registry(_tools(), AGENTS, _services(RemindersService(None, backend=backend))).get("reminders")
    assert entry is not None and entry.status == "auto_stub" and backend.asked == 0


def test_a_missing_section_or_service_makes_a_stub() -> None:
    entry = build_registry(_tools(section=False), AGENTS, _services(RemindersService(None, backend=FakeReminders()))).get("reminders")
    assert entry is not None and entry.status == "auto_stub" and "`reminders:` section" in (entry.reason or "")
    entry = build_registry(_tools(), AGENTS, _services(None)).get("reminders")
    assert entry is not None and entry.status == "auto_stub"


def test_the_default_list_must_be_one_of_the_lists() -> None:
    with pytest.raises(ValueError, match="isn't one of"):
        RemindersConfig(lists=["Shopping"], default_list="Household")
    with pytest.raises(ValueError):
        RemindersConfig(lists=[])
    assert RemindersService(RemindersConfig(lists=["Shopping", "Household"]), backend=FakeReminders()).default_list() == "Shopping"


def test_no_access_at_call_time_is_said() -> None:
    backend = FakeReminders()
    service, _ = _service(backend)
    backend._access = "denied"  # taken away in System Settings after the tool loaded
    outcome = run(service, action="list")
    assert outcome.error and outcome.speak == "I can't reach Reminders yet."


def test_a_configured_list_missing_from_reminders_is_created_once() -> None:
    backend = FakeReminders()
    backend.lists = ["shopping", "Private"]  # Shopping is there (any case); Household isn't
    service = RemindersService(None, backend=backend)
    for _ in range(2):
        entry = build_registry(_tools(), AGENTS, _services(service)).get("reminders")
        assert entry is not None and entry.status == "ready"
    assert backend.created == ["Household"]
    assert run(service, action="add", title="fix the gate").speak == "I've added “fix the gate” to Household."


def test_the_same_title_twice_is_one_answer_not_a_question() -> None:
    service, backend = _service()
    backend.items = [ReminderItem(id="1", title="milk", list="Shopping"), ReminderItem(id="2", title="Milk", list="Shopping")]
    assert run(service, action="complete", title="milk").speak == "Marked “milk” done."
    assert backend.completed == ["1"]


def test_all_lists_reaches_every_list_and_names_them() -> None:
    backend = FakeReminders()
    backend.items = [ReminderItem(id="p1", title="secret", list="Private"), ReminderItem(id="1", title="milk", list="Shopping")]
    service = RemindersService(RemindersConfig(lists="all"), backend=backend)
    assert run(service, action="lists").result == {"lists": ["Shopping", "Household", "Private"], "default": "Household"}
    assert [r["title"] for r in run(service, action="list").result["reminders"]] == ["secret", "milk"]
    assert run(service, action="list", list="private").result["reminders"] == [{"title": "secret", "list": "Private"}]
    assert run(service, action="add", title="call mom").speak == "I've added “call mom” to Household."  # the account's default list
    assert backend.items[-1].list == "Household"
    assert run(service, action="add", title="x", list="Garage").speak == "There's no Garage list. The lists are:\n1. Shopping\n2. Household\n3. Private"
    assert run(service, action="complete", title="secret").speak == "Marked “secret” done."


def test_all_lists_creates_nothing_and_takes_any_default() -> None:
    backend = FakeReminders()
    service = RemindersService(RemindersConfig(lists="all", default_list="Victoria Test"), backend=backend)
    service.ensure_lists()
    assert backend.created == [] and service.default_list() == "Victoria Test"


def test_lists_and_reminders_are_answered_as_numbered_lists() -> None:
    service, _ = _stocked()
    assert run(service, action="lists").speak == "You have 2 lists:\n1. Shopping\n2. Household"
    assert run(service, action="list", list="shopping").speak == "2 reminders on Shopping:\n1. milk\n2. eggs"
    assert run(service, action="list", when="today").speak == (
        "2 reminders due today on your lists:\n"
        "1. pay the water bill (Household, yesterday)\n"
        "2. call the plumber (Household, today at 3 PM)"
    )
    service, backend = _service()
    backend.items = [ReminderItem(id=str(i), title=f"item {i}", list="Shopping") for i in range(17)]
    assert run(service, action="list", list="Shopping").speak.endswith("15. item 14\nand 2 more")


# Saying a list's name or number ------------------------------------------------------

BILLS = ["Carriage House Needs", "Family", "Groceries", "House", "Shared Shopping List", "Shared TODO", "Vacation Packing", "Bill Work Trip Packing"]


def _everything() -> tuple[RemindersService, FakeReminders]:
    backend = FakeReminders()
    backend.lists = list(BILLS)
    backend.items = [
        ReminderItem(id="g1", title="milk", list="Groceries"),
        ReminderItem(id="g2", title="eggs", list="Groceries"),
        ReminderItem(id="h1", title="fix the gate", list="House"),
    ]
    return RemindersService(RemindersConfig(lists="all"), backend=backend), backend


@pytest.mark.parametrize(
    ("said", "meant"),
    [
        ("groceries", "Groceries"),
        ("GROCERIES", "Groceries"),
        ("Grocerys", "Groceries"),
        ("the grocery list", "Groceries"),
        ("famly", "Family"),
        ("carrige house", "Carriage House Needs"),
        ("house", "House"),  # not Carriage House Needs
        ("shopping", "Shared Shopping List"),
        ("todo", "Shared TODO"),
    ],
)
def test_a_list_is_found_whatever_the_case_or_small_misspellings(said: str, meant: str) -> None:
    service, _ = _everything()
    outcome = run(service, action="list", list=said)
    assert outcome.speak is not None and outcome.speak.split(":")[0].endswith(f"on {meant}") or outcome.speak.startswith(f"Nothing on {meant}")


@pytest.mark.parametrize(("said", "options"), [("packing", "Vacation Packing or Bill Work Trip Packing"), ("shared", "Shared Shopping List or Shared TODO")])
def test_a_name_that_fits_two_lists_asks_which(said: str, options: str) -> None:
    service, _ = _everything()
    outcome = run(service, action="list", list=said)
    assert outcome.pending is not None and set(outcome.speak.removeprefix("Which list: ").rstrip("?").split(" or ")) == set(options.split(" or "))


def test_an_unknown_list_answers_with_the_numbered_lists() -> None:
    service, _ = _everything()
    outcome = run(service, action="list", list="garage")
    assert outcome.speak.startswith("There's no garage list. The lists are:\n1. Carriage House Needs\n2. Family")
    assert run(service, action="list", list="3").speak.startswith("2 reminders on Groceries:")  # numbers work from it too


@pytest.mark.parametrize("said", ["3", "number 3", "three", "the third one", "3rd", 3])
def test_a_list_is_chosen_by_its_number_from_the_last_listing(said: Any) -> None:
    service, _ = _everything()
    run(service, action="lists")
    assert run(service, action="list", list=said).speak == "2 reminders on Groceries:\n1. milk\n2. eggs"


def test_numbers_belong_to_the_device_they_were_shown_on_and_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _everything()
    asyncio.run(service.handle({"action": "lists"}, now=NOW, device="kitchen"))
    other = asyncio.run(service.handle({"action": "list", "list": "3"}, now=NOW, device="office"))
    assert other.speak == "I don't have a list number 3. Ask what lists there are first."
    assert asyncio.run(service.handle({"action": "list", "list": "3"}, now=NOW, device="kitchen")).speak.startswith("2 reminders on Groceries")
    import victoria_voice_assistant.tools.reminders as reminders

    later = reminders.time.monotonic() + reminders.SHOWN_FOR_S + 1
    monkeypatch.setattr(reminders.time, "monotonic", lambda: later)
    assert asyncio.run(service.handle({"action": "list", "list": "3"}, now=NOW, device="kitchen")).speak.startswith("I don't have a list number 3")
    assert run(service, action="list", list="12").speak.startswith("I don't have a list number 12")


def test_a_reminder_is_completed_by_its_number_from_the_last_listing() -> None:
    service, backend = _everything()
    run(service, action="list", list="groceries")
    assert run(service, action="complete", title="number 2").speak == "Marked “eggs” done."
    assert backend.completed == ["g2"]
    assert run(service, action="complete", title="5").speak == "I don't have a reminder number 5. Ask what's on the list first."



# The groceries tool: one list, read whole --------------------------------------------

from victoria_voice_assistant.tools.reminders import ListTool  # noqa: E402


def _groceries(*items: tuple[str, bool]) -> tuple[ListTool, FakeReminders]:
    backend = FakeReminders()
    backend.lists.append("Victoria Groceries")
    for index, (title, checked) in enumerate(items, 1):
        backend.items.append(ReminderItem(id=f"g{index}", title=title, list="Victoria Groceries"))
        if checked:
            backend.completed.append(f"g{index}")
    return ListTool(RemindersService(None, backend=backend), "Victoria Groceries"), backend


def grocery(tool: ListTool, **arguments: Any):
    return asyncio.run(tool.handle(arguments))


def test_reading_gives_the_whole_list_to_the_model() -> None:
    tool, _ = _groceries(("Carrots", False), ("Milk", True), ("Pizza", True), ("Coffee", False))
    outcome = grocery(tool, action="read")
    assert outcome.speak is None  # the model answers ("Yes, carrots are on the list.")
    assert outcome.result["need"] == ["Carrots", "Coffee"]
    assert outcome.result["have"] == ["Milk", "Pizza"]


def test_need_adds_a_new_item_capitalised_and_says_so_directly() -> None:
    tool, backend = _groceries(("Carrots", False))
    outcome = grocery(tool, action="need", item="ice cream")
    assert backend.items[-1].title == "Ice cream" and backend.items[-1].list == "Victoria Groceries"
    assert outcome.speak == "I've added “Ice cream” to Victoria Groceries." and outcome.direct


def test_out_of_something_checked_unchecks_it() -> None:
    tool, backend = _groceries(("Pizza", True))
    outcome = grocery(tool, action="need", item="pizza")
    assert backend.completed == [] and len(backend.items) == 1
    assert outcome.speak == "I've put “Pizza” back on Victoria Groceries."


def test_need_something_already_needed_adds_nothing() -> None:
    tool, backend = _groceries(("Carrots", False))
    assert grocery(tool, action="need", item="carrot").speak == "“Carrots” is already on Victoria Groceries."
    assert len(backend.items) == 1


def test_got_checks_it_off() -> None:
    tool, backend = _groceries(("Milk", False), ("Eggs", False))
    assert grocery(tool, action="got", item="the milk").speak == "I've checked off “Milk” on Victoria Groceries."
    assert backend.completed == ["g1"]
    assert grocery(tool, action="got", item="milk").speak == "“milk” isn't unchecked on Victoria Groceries."


def test_remove_deletes_the_item() -> None:
    tool, backend = _groceries(("Carrots", False), ("Milk", True))
    assert grocery(tool, action="remove", item="carrots").speak == "I've taken “Carrots” off Victoria Groceries."
    assert [i.title for i in backend.items] == ["Milk"]
    assert grocery(tool, action="remove", item="bread").speak == "“bread” isn't on Victoria Groceries."


@pytest.mark.parametrize(
    ("said", "matched"),
    [("carrot", "Carrots"), ("CARROTS", "Carrots"), ("icecream", "Ice cream"), ("tomatos", "Tomatoes"), ("the oat milk", "Oat milk")],
)
def test_items_match_whatever_the_case_plural_or_small_misspelling(said: str, matched: str) -> None:
    tool, _ = _groceries(("Carrots", False), ("Ice cream", False), ("Tomatoes", False), ("Oat milk", False))
    assert grocery(tool, action="got", item=said).speak == f"I've checked off “{matched}” on Victoria Groceries."


def test_a_word_from_a_longer_item_is_a_new_item() -> None:
    tool, backend = _groceries(("Egg noodles", True))
    assert grocery(tool, action="need", item="eggs").speak == "I've added “Eggs” to Victoria Groceries."
    assert backend.completed == ["g1"]  # Egg noodles left alone


def test_two_close_items_ask_which() -> None:
    tool, backend = _groceries(("Red onions", False), ("White onions", False))
    outcome = grocery(tool, action="got", item="onions")
    assert outcome.pending is not None and outcome.speak in ("Which one: Red onions or White onions?",)
    assert backend.completed == []


def test_the_same_item_twice_is_one() -> None:
    tool, backend = _groceries(("Milk", False), ("milk", False))
    assert grocery(tool, action="got", item="milk").speak == "I've checked off “Milk” on Victoria Groceries."
    assert sorted(backend.completed) == ["g1", "g2"]


def test_a_missing_list_is_made_once_and_said_when_gone() -> None:
    backend = FakeReminders()
    tool = ListTool(RemindersService(None, backend=backend), "Victoria Groceries")
    tool.ensure_list()
    tool.ensure_list()
    assert backend.created == ["Victoria Groceries"]
    backend.lists.remove("Victoria Groceries")
    outcome = grocery(tool, action="read")
    assert outcome.error and outcome.speak == "There's no Victoria Groceries list in Reminders."


def test_the_registry_builds_it_from_config_and_needs_a_list() -> None:
    backend = FakeReminders()
    service = RemindersService(None, backend=backend)
    entry = {"name": "groceries", "description": "The grocery list.", "kind": "builtin", "service": "reminders.list",
             "list": "Victoria Groceries", "params": {"action": {"type": "string", "required": True}, "item": {"type": "string"}}}
    built = build_registry(ToolsConfig.model_validate({"tools": [entry]}), AGENTS, _services(service)).get("groceries")
    assert built is not None and built.status == "ready" and "Victoria Groceries" in backend.lists
    outcome = asyncio.run(built.handler({"action": "need", "item": "ice cream"}, ToolContext(device="kitchen")))
    assert outcome.speak == "I've added “Ice cream” to Victoria Groceries."
    no_list = {k: v for k, v in entry.items() if k != "list"}
    built = build_registry(ToolsConfig.model_validate({"tools": [no_list]}), AGENTS, _services(service)).get("groceries")
    assert built is not None and built.status == "auto_stub" and "needs `list:`" in (built.reason or "")


def test_several_items_at_once() -> None:
    tool, backend = _groceries(("Pizza", True))
    outcome = grocery(tool, action="need", item="carrots, milk and pizza")
    assert [i.title for i in backend.items] == ["Pizza", "Carrots", "Milk"] and backend.completed == []
    assert outcome.speak == "I've added “Carrots” and “Milk” to Victoria Groceries. I've put “Pizza” back on Victoria Groceries."
    assert grocery(tool, action="need", item="carrots, milk").speak == "“Carrots” and “Milk” are already on Victoria Groceries."
    assert grocery(tool, action="got", item="carrots, bread").speak == "I've checked off “Carrots” on Victoria Groceries. “bread” isn't unchecked on Victoria Groceries."
    assert grocery(tool, action="need", item="mac and cheese").speak == "I've added “Mac and cheese” to Victoria Groceries."


# From the code review, 2026-11-12 ----------------------------------------------------


def test_a_lists_own_name_comes_before_numbers_and_filler_words() -> None:
    backend = FakeReminders()
    backend.lists = ["My List", "List", "2026", "Groceries"]
    service = RemindersService(RemindersConfig(lists="all"), backend=backend)
    for name in ("My List", "my list", "List", "2026"):
        backend.items = [ReminderItem(id="1", title="thing", list=name if name != "my list" else "My List")]
        assert run(service, action="list", list=name).speak.startswith("1 reminder on " + ("My List" if name == "my list" else name))


def test_numbers_are_remembered_per_browser_not_shared_by_every_dashboard() -> None:
    backend = FakeReminders()
    backend.items = [ReminderItem(id="1", title="milk", list="Shopping"), ReminderItem(id="2", title="eggs", list="Shopping")]
    entry = build_registry(_tools(), AGENTS, _services(RemindersService(None, backend=backend))).get("reminders")
    assert entry is not None

    def call(client: str | None, **arguments: Any):
        return asyncio.run(entry.handler(arguments, ToolContext(device="web", client_id=client)))

    call("browser-a", action="list", list="Shopping")
    assert call("browser-b", action="complete", title="2").speak.startswith("I don't have a reminder number 2")
    assert call("browser-a", action="complete", title="2").speak == "Marked “eggs” done."
    assert backend.completed == ["2"]


def test_reminders_is_asked_off_the_event_loop() -> None:
    import threading

    backend = FakeReminders()
    seen: set[int] = set()
    for method in ("access", "list_names", "default_list_name", "incomplete"):
        original = getattr(backend, method)

        def wrapped(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            seen.add(threading.get_ident())
            return _original(*args, **kwargs)

        setattr(backend, method, wrapped)
    service = RemindersService(RemindersConfig(lists="all"), backend=backend)

    async def go() -> int:
        await service.handle({"action": "list", "list": "shopping"}, now=NOW)
        await service.handle({"action": "lists"}, now=NOW)
        return threading.get_ident()

    loop_thread = asyncio.run(go())
    assert seen and loop_thread not in seen


def test_several_items_read_the_list_once_and_keep_a_which_one_question() -> None:
    tool, backend = _groceries(("Milk", False), ("Red onions", False), ("White onions", False))
    reads: list[str] = []
    original = backend.all_items
    backend.all_items = lambda name: (reads.append(name), original(name))[1]  # type: ignore[method-assign]
    outcome = grocery(tool, action="got", item="milk, onions, bread")
    assert reads == ["Victoria Groceries"]
    assert outcome.speak == "I've checked off “Milk” on Victoria Groceries. Which one: Red onions or White onions? “bread” isn't unchecked on Victoria Groceries."
    assert outcome.pending is not None and outcome.pending.options == ["Red onions", "White onions"]
    # The same item twice in one call: the second sees what the first did.
    assert grocery(tool, action="need", item="kale, kale").speak == "I've added “Kale” to Victoria Groceries. “Kale” is already on Victoria Groceries."
    assert [i.title for i in backend.items].count("Kale") == 1


def test_a_long_spoken_question_stays_open_long_enough_to_be_answered() -> None:
    import time

    from victoria_voice_assistant.chat.tools import SPOKEN_WORDS_PER_S, SessionState, ToolRuntime
    from victoria_voice_assistant.tools.outcome import VOICE_PENDING_S

    backend = FakeReminders()
    backend.lists.append("Victoria Groceries")
    backend.items = [ReminderItem(id=str(i), title=name, list="Victoria Groceries") for i, name in enumerate(["Red onions", "White onions"])]
    entry = {"name": "groceries", "description": "The grocery list.", "kind": "builtin", "service": "reminders.list",
             "list": "Victoria Groceries", "params": {"action": {"type": "string", "required": True}, "item": {"type": "string"}}}
    registry = build_registry(ToolsConfig.model_validate({"tools": [entry]}), AGENTS, _services(RemindersService(None, backend=backend)))
    state = SessionState(device="kitchen", pending_window_s=VOICE_PENDING_S)

    async def nothing(_message: dict[str, Any]) -> None:
        return None

    runtime = ToolRuntime(registry, state, notify=nothing)
    outcome, direct = asyncio.run(runtime.run(registry.get("groceries"), {"action": "got", "item": "onions"}))
    words = len(direct.split())
    assert state.pending is not None
    assert state.pending.expires_at - time.monotonic() == pytest.approx(VOICE_PENDING_S + words / SPOKEN_WORDS_PER_S, abs=0.5)


def test_every_listener_rebuilds_when_reminders_access_arrives() -> None:
    from victoria_voice_assistant.config.manager import ConfigManager

    manager = ConfigManager.__new__(ConfigManager)
    manager.current = "the config"  # type: ignore[assignment]
    seen: list[tuple[str, Any]] = []
    manager._listeners = [lambda config: seen.append(("chat", config)), lambda config: seen.append(("satellites", config))]
    manager.notify()
    assert seen == [("chat", "the config"), ("satellites", "the config")]
