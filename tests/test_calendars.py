from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Any

from victoria_voice_assistant.config.schema import AgentsConfig, CalendarConfig, TimersConfig, ToolsConfig, WeatherConfig
from victoria_voice_assistant.tools.calendars import CalendarEvent, CalendarInfo, CalendarService, account_label, calendar_names, free_gaps, parse_alert, parse_range
from victoria_voice_assistant.tools.registry import Services, ToolContext, build_registry
from victoria_voice_assistant.tools.reminders import Access
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

AGENTS = AgentsConfig.model_validate({"default": "helper", "agents": [{"name": "helper", "kind": "model", "model": "fast"}]})
# Wednesday 2026-11-11, 10:00 local.
NOW = datetime(2026, 11, 11, 10, 0)
TOOL = {"name": "calendar", "description": "The calendar.", "kind": "builtin", "service": "calendar", "params": {"action": {"type": "string", "required": True}}}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute)


class FakeCalendar:
    """The calendar account replaced: two calendars we may write, a read-only one and a skipped one."""

    def __init__(self, access: Access = "granted") -> None:
        self._access: Access = access
        self.asked = 0
        self.refreshed = 0
        self.infos = [CalendarInfo("Family"), CalendarInfo("Work"), CalendarInfo("US Holidays", writable=False), CalendarInfo("Private")]
        self.stored: list[CalendarEvent] = []
        self.updates: list[tuple[str, dict[str, Any], bool]] = []
        self.deleted: list[tuple[str, bool]] = []
        self.repeats: list[str | None] = []

    def access(self) -> Access:
        return self._access

    def request_access(self) -> bool:
        self.asked += 1
        self._access = "granted"
        return True

    def refresh(self) -> None:
        self.refreshed += 1

    def calendars(self) -> list[CalendarInfo]:
        return list(self.infos)

    def default_calendar(self) -> str | None:
        return "Family"

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return [event for event in self.stored if event.end > start and event.start < end]

    def create(self, draft: CalendarEvent, repeat: str | None) -> CalendarEvent:
        made = replace(draft, id=f"new{len(self.stored)}", repeats=bool(repeat))
        self.stored.append(made)
        self.repeats.append(repeat)
        return made

    def update(self, event: CalendarEvent, changes: dict[str, Any], future: bool) -> CalendarEvent:
        self.updates.append((event.id, changes, future))
        changed = replace(event, **changes)
        self.stored = [changed if stored is event else stored for stored in self.stored]
        return changed

    def delete(self, event: CalendarEvent, future: bool) -> None:
        self.deleted.append((event.id, future))
        self.stored = [stored for stored in self.stored if stored is not event]

    def create_calendar(self, name: str) -> str:
        self.infos.append(CalendarInfo(name))
        return "on this Mac"


def _service(skip: list[str] | None = None) -> tuple[CalendarService, FakeCalendar]:
    backend = FakeCalendar()
    backend.stored = [
        CalendarEvent("d1", "Dentist", "Family", at(13, 14), at(13, 15), location="12 Main St"),
        CalendarEvent("s1", "Standup", "Work", at(11, 9), at(11, 9, 15), repeats=True),
        CalendarEvent("s1", "Standup", "Work", at(12, 9), at(12, 9, 15), repeats=True),
        CalendarEvent("l1", "Lunch with Sam", "Family", at(11, 12), at(11, 13), attendees=["Sam (accepted)"]),
        CalendarEvent("h1", "Veterans Day", "US Holidays", at(11, 0), at(11, 23, 59), all_day=True),
        CalendarEvent("p1", "Secret", "Private", at(11, 15), at(11, 16)),
    ]
    return CalendarService(CalendarConfig(skip=skip if skip is not None else ["Private"]), backend=backend), backend


def run(service: CalendarService, **arguments: Any):
    return asyncio.run(service.handle(arguments, now=NOW, device="kitchen"))


def test_ranges() -> None:
    assert parse_range(None, NOW).label == "today"
    friday = parse_range("Friday afternoon", NOW)
    assert (friday.start, friday.end, friday.label) == (at(13, 12), at(13, 17), "Friday afternoon")
    week = parse_range("this week", NOW)
    assert (week.start, week.end, week.days) == (at(11, 0), at(16, 0), 5)
    assert parse_range("next week", NOW).start == at(16, 0)
    weekend = parse_range("this weekend", NOW)
    assert (weekend.start, weekend.end) == (at(14, 0), at(16, 0))
    assert parse_range("tonight", NOW).label == "tonight"
    assert parse_range("blah", NOW) is None


def test_alert_and_gaps() -> None:
    assert parse_alert("15 minutes before") == 15
    assert parse_alert("an hour") == 60
    assert parse_alert(None) is None
    events = [CalendarEvent("a", "A", "X", at(11, 9), at(11, 10)), CalendarEvent("b", "B", "X", at(11, 10, 15), at(11, 11))]
    assert free_gaps(events, at(11, 8), at(11, 12)) == [(at(11, 8), at(11, 9)), (at(11, 11), at(11, 12))]


def test_today_is_listed_in_order_without_the_skipped_calendar() -> None:
    service, backend = _service()
    outcome = run(service, action="list")
    assert backend.refreshed == 1  # the account is reread for every request
    assert outcome.speak == "3 events today:\n1. All day: Veterans Day\n2. 9 AM: Standup\n3. 12 PM: Lunch with Sam"
    assert "Secret" not in str(outcome.result)


def test_a_week_says_the_days_and_an_empty_day_says_so() -> None:
    service, _ = _service()
    lines = run(service, action="list", when="this week").speak.splitlines()
    assert lines[0] == "5 events this week:"
    assert lines[-1] == "5. Friday, 2 PM: Dentist"
    assert run(service, action="list", when="Saturday").speak == "Nothing on your calendar on Saturday."
    assert run(service, action="list", when="florp").speak.startswith("Which day?")


def test_one_calendar_can_be_named() -> None:
    service, _ = _service()
    assert run(service, action="list", calendar="the work calendar").speak == "1 event today on the Work calendar:\n1. 9 AM: Standup"
    assert run(service, action="list", calendar="private").speak == "There's no private calendar."
    assert run(service, action="calendars").result["calendars"] == ["Family", "US Holidays", "Work"]


def test_next_and_find() -> None:
    service, _ = _service()
    assert run(service, action="next").speak == "Next is Lunch with Sam, today at 12 PM to 1 PM."
    assert run(service, action="find", title="the dentist").speak == "Dentist is Friday at 2 PM to 3 PM, at 12 Main St."
    assert run(service, action="find", title="standup").speak == "Standup is tomorrow at 9 AM to 9:15 AM."  # today's is over
    assert run(service, action="find", when="the dentist").speak == "Dentist is Friday at 2 PM to 3 PM, at 12 Main St."  # the name where the day goes
    assert run(service, action="find", event_name="the dentist").speak == "Dentist is Friday at 2 PM to 3 PM, at 12 Main St."  # a made-up parameter
    assert run(service, action="find", title="dentist", when="today").speak == "Dentist is Friday at 2 PM to 3 PM, at 12 Main St."  # a day nobody said
    # A broken made-up argument isn't taken for the name (2026-11-20: "finish_time": "2 hours',title:").
    assert run(service, action="change", duration="2 hours", finish_time="2 hours',title:").speak == "Which event?"
    assert run(service, action="change", duration="2 hours", length_of_event="2 hours").speak == "Which event?"
    assert run(service, action="find", title="haircut").speak == "I couldn't find “haircut” on your calendar."


def test_details() -> None:
    service, _ = _service()
    assert run(service, action="details", title="dentist").speak == "Dentist: Friday at 2 PM to 3 PM, at 12 Main St, on the Family calendar."
    run(service, action="list")
    assert "Invited: Sam (accepted)." in run(service, action="details", title="3").speak
    assert run(service, action="details", title="number 9").speak.startswith("I don't have an event number 9")


def test_free_time() -> None:
    service, _ = _service()
    assert run(service, action="free", when="Friday afternoon").speak == "On Friday afternoon you're free from 12 PM to 2 PM and from 3 PM to 5 PM."
    assert run(service, action="free", when="Saturday").speak == "You're free on Saturday."
    today = run(service, action="free")
    assert today.speak == "Today you're free from 10 AM to 12 PM and from 1 PM to 10 PM. All day: Veterans Day."
    assert run(service, action="free", when="this week").speak.splitlines()[0] == "Free time this week:"


def test_add_just_happens_and_says_what_was_added() -> None:
    service, backend = _service()
    outcome = run(service, action="add", title="Haircut", when="Friday at noon")
    assert outcome.speak == "I've added “Haircut” to Family, Friday at 12 PM to 1 PM."
    assert outcome.pending is None
    made = backend.stored[-1]
    assert (made.start, made.end, made.all_day, made.calendar) == (at(13, 12), at(13, 13), False, "Family")
    outcome = run(service, action="add", title="Review", when="tomorrow at 9", duration="90 minutes", calendar="work", alert="15 minutes", repeat="weekly", location="Room 4")
    assert outcome.speak == "I've added “Review” to Work, tomorrow at 9 AM to 10:30 AM, repeating weekly, with an alert 15 minutes before."
    assert (backend.stored[-1].location, backend.repeats[-1]) == ("Room 4", "weekly")
    run(service, action="add", title="Call", when="Friday at 2", end="3:30")
    assert backend.stored[-1].end == at(13, 15, 30)


def test_add_without_a_time_is_all_day_and_questions_fill_gaps() -> None:
    service, backend = _service()
    assert run(service, action="add", title="Sam's birthday", when="November 20").speak == "I've added “Sam's birthday” to Family, November 20, all day."
    assert backend.stored[-1].all_day
    assert run(service, action="add", when="Friday").speak == "What's the event called?"
    assert run(service, action="add", title="Lunch").speak == "When is it?"
    assert run(service, action="add", title="Parade", when="Friday", calendar="holidays").speak == "The US Holidays calendar is read-only."
    assert len(backend.stored) == 7


def test_a_configured_default_that_is_missing_is_not_replaced() -> None:
    service, backend = _service()
    service.settings = CalendarConfig(default_calendar="Victoria Test")
    assert run(service, action="add", title="Haircut", when="Friday at noon").speak == "There's no Victoria Test calendar."
    assert len(backend.stored) == 6


def test_a_move_asks_first_and_runs_on_a_yes() -> None:
    service, backend = _service()
    outcome = run(service, action="change", title="dentist", new_when="3")
    assert outcome.speak == "Dentist, Friday at 2 PM to 3 PM: move it to Friday at 3 PM to 4 PM?"
    assert outcome.pending.kind == "yes_no" and outcome.pending.on_yes == ("calendar", {"action": "confirm"})
    assert backend.updates == []
    done = run(service, action="confirm")
    assert done.speak == "Done. Dentist is Friday at 3 PM to 4 PM."
    assert backend.updates == [("d1", {"start": at(13, 15), "end": at(13, 16), "all_day": False}, False)]
    assert run(service, action="confirm").speak == "There's nothing waiting to confirm."


def test_other_changes_and_nothing_to_change() -> None:
    service, backend = _service()
    question = run(service, action="change", title="dentist", new_when="Monday", duration="2 hours", new_title="Orthodontist", location="5 Oak Ave").speak
    assert question == "Dentist, Friday at 2 PM to 3 PM: rename it “Orthodontist”, move it to Monday at 2 PM to 4 PM and set the place to 5 Oak Ave?"
    run(service, action="confirm")
    assert backend.updates[0][1] == {"title": "Orthodontist", "start": at(16, 14), "end": at(16, 16), "all_day": False, "location": "5 Oak Ave"}
    assert run(service, action="change", title="lunch").speak == "What should I change about Lunch with Sam?"
    assert run(service, action="change", title="veterans day", new_when="Friday").speak == "The US Holidays calendar is read-only."


def test_a_repeating_event_asks_which_ones() -> None:
    service, backend = _service()
    outcome = run(service, action="delete", title="standup")
    assert outcome.speak == "Standup repeats. Delete just this one, or all of them from now on?"
    assert outcome.pending.kind == "clarify"
    outcome = run(service, action="delete", title="standup", scope="future")
    assert outcome.speak == "Delete Standup, tomorrow at 9 AM to 9:15 AM, and all after it?"
    assert backend.deleted == []
    assert run(service, action="confirm").speak == "Deleted Standup."
    assert backend.deleted == [("s1", True)]


def test_a_delete_asks_first_and_several_matches_ask_which() -> None:
    service, backend = _service()
    backend.stored.append(CalendarEvent("d2", "Dentist", "Family", at(20, 9), at(20, 10)))
    outcome = run(service, action="delete", title="dentist")
    assert outcome.speak == "Which one?\n1. Dentist, Friday at 2 PM to 3 PM\n2. Dentist, November 20 at 9 AM to 10 AM"
    assert run(service, action="delete", title="2").speak == "Delete Dentist, November 20 at 9 AM to 10 AM?"
    assert run(service, action="confirm").result == {"deleted": "Dentist"}
    assert backend.deleted == [("d2", False)]
    # Another request in between drops the waiting delete.
    run(service, action="delete", title="lunch")
    run(service, action="list")
    assert run(service, action="confirm").speak == "There's nothing waiting to confirm."
    # A yes is only good on the device that was asked.
    run(service, action="delete", title="lunch")
    assert asyncio.run(service.handle({"action": "confirm"}, now=NOW, device="office")).speak == "There's nothing waiting to confirm."
    assert len(backend.deleted) == 1


def _services(calendar: Any) -> Services:
    return Services(timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig(home={"lat": 1, "lon": 2})), calendar=calendar)


def test_registry_builds_the_tool_or_a_stub_with_the_reason() -> None:
    tools = ToolsConfig.model_validate({"tools": [TOOL], "calendar": {"skip": ["Private"]}})
    service, _ = _service(skip=[])
    entry = build_registry(tools, AGENTS, _services(service)).get("calendar")
    assert entry.status == "ready"
    outcome = asyncio.run(entry.handler({"action": "calendars"}, ToolContext(device="kitchen")))
    assert "Private" not in outcome.result["calendars"]  # the registry passed the settings on

    waiting = CalendarService(backend=FakeCalendar(access="denied"))
    entry = build_registry(tools, AGENTS, _services(waiting)).get("calendar")
    assert entry.status == "auto_stub" and "Calendar access isn't granted" in entry.reason
    assert build_registry(tools, AGENTS, _services(None)).get("calendar").status == "auto_stub"


def test_no_access_is_said() -> None:
    service = CalendarService(backend=FakeCalendar(access="denied"))
    outcome = run(service, action="list")
    assert outcome.error and outcome.speak == "I can't reach the calendar yet."


def test_calendars_with_the_same_title_are_named_by_account() -> None:
    assert account_label("iCloud") == "iCloud"
    assert account_label("someone@gmail.com") == "Google"
    assert account_label("someone@example.org") == "example.org"
    names = calendar_names([("a", "Family", "iCloud"), ("b", "Family", "someone@gmail.com"), ("c", "Work", "iCloud"), ("d", "Work", "iCloud")])
    assert names == {"a": "Family (iCloud)", "b": "Family (Google)", "c": "Work (iCloud)", "d": "Work (iCloud) 2"}


def _two_families() -> tuple[CalendarService, FakeCalendar]:
    service, backend = _service()
    backend.infos = [
        CalendarInfo("Family (iCloud)", account="iCloud", title="Family"),
        CalendarInfo("Family (Google)", account="someone@gmail.com", title="Family"),
        CalendarInfo("Work"),
    ]
    backend.stored = [
        CalendarEvent("a1", "Soccer", "Family (iCloud)", at(11, 17), at(11, 18)),
        CalendarEvent("b1", "Piano", "Family (Google)", at(11, 16), at(11, 17)),
        CalendarEvent("w1", "Standup", "Work", at(11, 11), at(11, 12)),
    ]
    return service, backend


def test_the_family_calendar_reads_both_and_adding_asks_which() -> None:
    service, backend = _two_families()
    outcome = run(service, action="list", calendar="family")
    assert outcome.speak == "2 events today on the Family (Google) and Family (iCloud) calendars:\n1. 4 PM: Piano\n2. 5 PM: Soccer"
    assert run(service, action="list", calendar="family google").speak == "1 event today on the Family (Google) calendar:\n1. 4 PM: Piano"
    asked = run(service, action="add", title="Recital", when="Friday at 6", calendar="the family calendar")
    assert asked.speak == "Which calendar: Family (Google) or Family (iCloud)?"
    assert asked.pending.kind == "clarify" and len(backend.stored) == 3
    made = run(service, action="add", title="Recital", when="Friday at 6", calendar="Family (iCloud)")
    assert made.speak == "I've added “Recital” to Family (iCloud), Friday at 6 PM to 7 PM."


def test_skip_and_default_by_title() -> None:
    service, backend = _two_families()
    service.settings = CalendarConfig(skip=["Family"])
    assert run(service, action="calendars").result["calendars"] == ["Work"]
    service.settings = CalendarConfig(skip=["Family (Google)"], default_calendar="family")
    assert run(service, action="add", title="Recital", when="Friday at 6").speak == "I've added “Recital” to Family (iCloud), Friday at 6 PM to 7 PM."
    service.settings = CalendarConfig(default_calendar="family")
    assert run(service, action="add", title="Recital", when="Friday at 6").speak == "There's no family calendar."


def test_a_yes_that_does_not_stick_asks_once_and_does_not_rebuild() -> None:
    """macOS can answer yes for a launchd process while the status stays undetermined: no reload loop."""

    class Unchanged(FakeCalendar):
        def request_access(self) -> bool:
            self.asked += 1
            return True  # the status stays not_determined

    backend = Unchanged(access="not_determined")
    service = CalendarService(backend=backend)
    rebuilt: list[int] = []
    service.on_granted = lambda: rebuilt.append(1)
    for _ in range(3):
        service.ask_for_access()
        for thread in [t for t in __import__("threading").enumerate() if t.name == "calendar-access"]:
            thread.join(timeout=2)
    assert backend.asked == 1
    assert rebuilt == []


def test_a_real_grant_rebuilds_the_tools() -> None:
    backend = FakeCalendar(access="not_determined")
    service = CalendarService(backend=backend)
    rebuilt: list[int] = []
    service.on_granted = lambda: rebuilt.append(1)
    service.ask_for_access()
    for thread in [t for t in __import__("threading").enumerate() if t.name == "calendar-access"]:
        thread.join(timeout=2)
    assert backend.asked == 1 and rebuilt == [1]
