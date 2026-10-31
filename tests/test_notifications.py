from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from victoria_voice_assistant.agents.model_agent import AgentReply
from victoria_voice_assistant.agents.runner import AgentRunner
from victoria_voice_assistant.agents.spend import SpendLog
from victoria_voice_assistant.agents.tasks import TaskEvent, TaskService
from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.config.schema import NotificationsConfig, QuietHoursConfig, TimersConfig, WeatherConfig
from victoria_voice_assistant.notifications.service import GROUP_WINDOW_S, Announcement, NotificationService
from victoria_voice_assistant.notifications.tool import notifications_tool, parse_until, spoken_time
from victoria_voice_assistant.storage.db import migrate
from victoria_voice_assistant.tools.registry import Services, build_registry
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

AFTERNOON = datetime(2026, 10, 31, 14, 0).timestamp()
NIGHT = datetime(2026, 10, 31, 2, 0).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeOutlet:
    def __init__(self, speaks: bool, device: str = "web") -> None:
        self.speaks = speaks
        self.device = device
        self.is_busy = False
        self.count = 0
        self.announced: list[Announcement] = []
        self.shown: list[list[str]] = []

    def busy(self) -> bool:
        return self.is_busy

    def speaking(self) -> bool:
        return False

    def requests(self) -> int:
        return self.count

    async def announce(self, announcement: Announcement) -> None:
        self.announced.append(announcement)

    async def show(self, unread) -> None:
        self.shown.append([item.id for item in unread])


def service_at(now: float, database: Path | None = None, **settings) -> tuple[NotificationService, Clock]:
    clock = Clock(now)
    return NotificationService(NotificationsConfig(**settings), database, clock=clock), clock


def post(service: NotificationService, source: str = "hermes", **kwargs):
    fields = {"kind": "result", "headline": f"{source.title()} has finished.", "summary": "The plan is ready.", "full": "The plan is ready.\n\n# Monday\nSoup."}
    fields.update(kwargs)
    return service.post(source=source, target="web", **fields)


async def settle(service: NotificationService, clock: Clock) -> None:
    clock.now += GROUP_WINDOW_S + 0.1
    await service.tick()
    await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("start", "end", "hour", "quiet"),
    [
        ("00:00", "08:00", 2, True),
        ("00:00", "08:00", 8, False),
        ("00:00", "08:00", 23, False),
        ("22:00", "07:00", 23, True),  # across midnight
        ("22:00", "07:00", 6, True),
        ("22:00", "07:00", 12, False),
    ],
)
def test_quiet_hours(start: str, end: str, hour: int, quiet: bool) -> None:
    service, _ = service_at(datetime(2026, 10, 31, hour, 30).timestamp(), quiet_hours=QuietHoursConfig(start=start, end=end))
    assert service.in_quiet_hours() is quiet


def test_default_quiet_hours_are_midnight_to_eight() -> None:
    hours = NotificationsConfig().quiet_hours
    assert hours is not None and (hours.start, hours.end) == ("00:00", "08:00")


def test_an_idle_screen_shows_the_result_with_a_chime() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        screen = FakeOutlet(speaks=False)
        service.attach(screen)
        item = post(service)
        await service.tick()
        assert screen.announced == []  # waits for the grouping window
        await settle(service, clock)
        return item, screen

    item, screen = asyncio.run(scenario())
    [announcement] = screen.announced
    assert announcement.text == "Hermes has finished." and announcement.sound and not announcement.spoken
    assert announcement.full == [item]
    assert item.state == "delivered" and item.mentioned  # announced, not yet read: the badge stays


def test_a_speaker_asks_before_reading() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        speaker, screen = FakeOutlet(speaks=True), FakeOutlet(speaks=False)
        service.attach(speaker)
        service.attach(screen)
        item = post(service)
        await settle(service, clock)
        return item, speaker, screen

    item, speaker, screen = asyncio.run(scenario())
    [spoken] = speaker.announced
    assert spoken.text == "Hermes has finished. Want to hear it?" and spoken.spoken
    assert spoken.question is not None and spoken.question.on_yes == ("notifications", {"action": "read", "ids": [item.id]})
    assert screen.announced == [spoken]  # the screen gets the full text and the chime
    assert item.state == "delivered"


def test_a_busy_room_waits_until_the_turn_ends() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        speaker = FakeOutlet(speaks=True)
        speaker.is_busy = True
        service.attach(speaker)
        item = post(service)
        await settle(service, clock)
        waited = (list(speaker.announced), item.state)
        speaker.is_busy = False
        await service.tick()
        return waited, speaker

    (before, state), speaker = asyncio.run(scenario())
    assert before == [] and state == "pending"
    assert len(speaker.announced) == 1


def test_quiet_hours_are_silent_then_mentioned_after_a_request() -> None:
    async def scenario():
        service, clock = service_at(NIGHT)
        speaker, screen = FakeOutlet(speaks=True), FakeOutlet(speaks=False)
        service.attach(speaker)
        service.attach(screen)
        item = post(service)
        await settle(service, clock)
        silent = (list(speaker.announced), list(screen.announced), item.state, item.mentioned)
        await service.tick()
        speaker.count += 1  # someone asks for something in that room
        await service.tick()
        speaker.count += 1
        await service.tick()  # mentioned once, not after every reply
        return silent, speaker

    (spoken, shown, state, mentioned), speaker = asyncio.run(scenario())
    assert spoken == [] and len(shown) == 1 and not shown[0].sound and state == "delivered" and not mentioned
    assert [a.text for a in speaker.announced] == ["By the way, you have a message from Hermes."]


def test_do_not_disturb_is_quiet_too() -> None:
    service, clock = service_at(AFTERNOON)
    service.set_dnd(AFTERNOON + 3600)
    assert service.quiet()
    clock.now += 3601
    assert not service.quiet() and service.dnd_until is None


def test_notifications_ready_together_are_grouped() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        speaker = FakeOutlet(speaks=True)
        service.attach(speaker)
        post(service, "hermes")
        clock.now += 0.5
        post(service, "helper")
        await settle(service, clock)
        return speaker

    speaker = asyncio.run(scenario())
    assert [a.text for a in speaker.announced] == ["Two updates, from Hermes and Helper. Want to hear them?"]


def test_speak_summary_reads_it_and_marks_it_read() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        speaker = FakeOutlet(speaks=True)
        service.attach(speaker)
        item = post(service, style="speak_summary")
        await settle(service, clock)
        return item, speaker, service

    item, speaker, service = asyncio.run(scenario())
    assert speaker.announced[0].text == "Hermes has finished. The plan is ready."
    assert item.state == "acknowledged" and service.unread() == []


def test_chime_only_is_not_spoken() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        speaker, screen = FakeOutlet(speaks=True), FakeOutlet(speaks=False)
        service.attach(speaker)
        service.attach(screen)
        post(service, style="chime_only")
        await settle(service, clock)
        return speaker, screen

    speaker, screen = asyncio.run(scenario())
    assert speaker.announced == [] and screen.announced[0].sound and not screen.announced[0].spoken


def test_nobody_there_keeps_it_pending_until_a_client_connects() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        item = post(service)
        await settle(service, clock)
        before = item.state
        screen = FakeOutlet(speaks=False)
        service.attach(screen)
        await service.tick()
        await asyncio.sleep(0)
        return before, item, screen

    before, item, screen = asyncio.run(scenario())
    assert before == "pending" and item.state == "delivered" and len(screen.announced) == 1
    assert screen.shown and screen.shown[0] == [item.id]  # the badge as soon as it attached


def test_low_priority_is_never_announced() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        speaker = FakeOutlet(speaks=True)
        service.attach(speaker)
        item = post(service, "system", kind="notice", priority="low")
        await settle(service, clock)
        return item, speaker

    item, speaker = asyncio.run(scenario())
    assert speaker.announced == [] and item.state == "pending"


def test_unread_notifications_expire() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON, expire_after_h=1)
        item = post(service)
        clock.now += 3601
        await service.tick()
        return item, service

    item, service = asyncio.run(scenario())
    assert item.state == "expired" and service.unread() == []


def test_unread_notifications_survive_a_restart(tmp_path: Path) -> None:
    database = migrate(tmp_path / "a.sqlite3")

    async def scenario():
        first, _ = service_at(AFTERNOON, database)
        kept = post(first)
        read = post(first, "helper")
        first.acknowledge([read])
        second, _ = service_at(AFTERNOON, database)
        second.start()
        await second.close()
        return kept, second

    kept, second = asyncio.run(scenario())
    assert [item.id for item in second.unread()] == [kept.id]
    assert second.last_from("helper") is not None  # read ones are still there for "what did Helper say?"


# The tool --------------------------------------------------------------------


def test_parse_until() -> None:
    evening = datetime(2026, 10, 31, 22, 15)
    assert parse_until("8 a.m.", evening) == datetime(2026, 11, 1, 8, 0)
    assert parse_until("until 11:30pm", evening) == datetime(2026, 10, 31, 23, 30)
    assert parse_until("07:00", evening) == datetime(2026, 11, 1, 7, 0)
    assert parse_until("noon", evening) == datetime(2026, 11, 1, 12, 0)
    assert parse_until("soon", evening) is None
    assert spoken_time(datetime(2026, 11, 1, 8, 0)) == "8 a.m." and spoken_time(datetime(2026, 11, 1, 23, 30)) == "11:30 p.m."


def test_tool_lists_reads_and_clears() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        post(service, "hermes")
        clock.now += 60
        post(service, "helper", headline="Helper has finished.", summary="Leeks go well with potatoes.", full="Leeks go well with potatoes.")
        listed = await notifications_tool(service, {"action": "list"}, "web")
        read = await notifications_tool(service, {"action": "read"}, "web")
        after = await notifications_tool(service, {"action": "list"}, "web")
        again = await notifications_tool(service, {"action": "read", "source": "hermes"}, "web")
        post(service, "hermes")
        cleared = await notifications_tool(service, {"action": "dismiss"}, "web")
        return listed, read, after, again, cleared, service

    listed, read, after, again, cleared, service = asyncio.run(scenario())
    assert listed.speak == "You have 2 notifications, from Helper and Hermes. Want to hear them?"
    assert listed.pending is not None and listed.pending.on_yes[1]["action"] == "read"
    assert read.speak == "Helper has finished. Leeks go well with potatoes. Hermes has finished. The plan is ready."
    assert "# Monday" in (read.chat or "")  # the full text goes to the screen
    assert after.speak == "No new notifications."
    assert again.speak == "Hermes said: The plan is ready."  # replays the last result, read or not
    assert cleared.speak == "Okay, cleared." and service.unread() == []


def test_tool_reads_the_ids_from_a_yes() -> None:
    async def scenario():
        service, _ = service_at(AFTERNOON)
        first = post(service, "hermes")
        post(service, "helper")
        return await notifications_tool(service, {"action": "read", "ids": [first.id]}, "web"), service

    outcome, service = asyncio.run(scenario())
    assert outcome.speak == "Hermes has finished. The plan is ready."
    assert [item.source for item in service.unread()] == ["helper"]


def test_what_did_an_agent_say_falls_back_to_the_task() -> None:
    async def scenario():
        service, _ = service_at(AFTERNOON)
        tasks = TaskService(None)
        task = tasks.create("helper", "web", "what goes with leeks", "wait")
        tasks.update(task, state="finished", result="Potatoes. Also cream.")
        found = await notifications_tool(service, {"action": "read", "source": "helper"}, "web", tasks)
        missing = await notifications_tool(service, {"action": "read", "source": "hermes"}, "web", tasks)
        return found, missing

    found, missing = asyncio.run(scenario())
    assert found.speak == "Helper said: Potatoes. Also cream."
    assert missing.speak == "I don't have anything from Hermes."


def test_tool_do_not_disturb() -> None:
    async def scenario():
        service, _ = service_at(AFTERNOON)
        on = await notifications_tool(service, {"action": "do_not_disturb", "until": "8 a.m."}, "web")
        quiet = service.dnd_until is not None
        off = await notifications_tool(service, {"action": "do_not_disturb", "until": "off"}, "web")
        bad = await notifications_tool(service, {"action": "do_not_disturb", "until": "later"}, "web")
        return on, quiet, off, bad, service

    on, quiet, off, bad, service = asyncio.run(scenario())
    assert on.speak == "Do not disturb until 8 a.m." and quiet
    assert off.speak == "Do not disturb is off." and service.dnd_until is None
    assert bad.speak == "Until what time?"


# Agents ----------------------------------------------------------------------


def _runner(example_config_dir: Path, mode: str) -> tuple[AgentRunner, NotificationService, list[TaskEvent]]:
    config = load_config(example_config_dir)
    agents = config.agents.model_copy(
        update={
            "agents": [
                a.model_copy(update={"mode": mode, "wait_s": None if mode != "auto" else a.wait_s}) if a.name == "helper" else a
                for a in config.agents.agents
            ]
        }
    )
    config = config.model_copy(update={"agents": agents})
    notifications = NotificationService(NotificationsConfig())
    services = Services(
        timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()), notifications=notifications
    )
    tasks = TaskService(None)
    events: list[TaskEvent] = []

    async def listen(event: TaskEvent) -> None:
        events.append(event)

    tasks.add_listener(listen)
    runner = AgentRunner(config, services, tasks, SpendLog(None))
    services.agents = runner

    async def fake_call_model(agent, task, model, tools, history=None) -> AgentReply:
        return AgentReply("Leeks and potatoes make a fine soup.")

    runner._call_model = fake_call_model  # type: ignore[method-assign]
    return runner, notifications, events


async def _finish(runner: AgentRunner) -> None:
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not runner.tasks.active():
            return


def test_a_background_result_becomes_a_notification(example_config_dir: Path) -> None:
    async def scenario():
        runner, notifications, events = _runner(example_config_dir, "background")
        await runner.ask("helper", "what goes with leeks", "web")
        await _finish(runner)
        return notifications.unread(), events

    unread, events = asyncio.run(scenario())
    assert [e for e in events if e.kind == "say"] == []  # not spoken into a room that stopped waiting
    [item] = unread
    assert (item.source, item.kind, item.headline, item.summary) == (
        "helper", "result", "Helper has finished.", "Leeks and potatoes make a fine soup."
    )
    assert item.style == "ask_first" and item.task_id is not None


def test_a_waited_for_result_is_said_straight_away(example_config_dir: Path) -> None:
    async def scenario():
        runner, notifications, events = _runner(example_config_dir, "wait")
        await runner.ask("helper", "what goes with leeks", "web")
        await _finish(runner)
        return notifications.unread(), events

    unread, events = asyncio.run(scenario())
    assert unread == [] and [e.text for e in events if e.kind == "say"] == ["Leeks and potatoes make a fine soup."]


def test_the_notifications_tool_is_built_in(example_config_dir: Path) -> None:
    config = load_config(example_config_dir)
    services = Services(
        timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()), notifications=NotificationService()
    )
    entry = build_registry(config.tools, config.agents, services).get("notifications")
    assert entry is not None and entry.status == "ready" and entry.speaks_directly


# Agent questions ---------------------------------------------------------------

from victoria_voice_assistant.agents.runner import harness_question  # noqa: E402
from victoria_voice_assistant.agents.tasks_tool import tasks_tool  # noqa: E402


def test_harness_question_detection() -> None:
    assert harness_question("I can save it. Which folder should I use?") == "I can save it. Which folder should I use?"
    assert harness_question("Saved to story.txt. Anything else?") is None
    assert harness_question("Saved to story.txt. Let me know if you want changes?") is None
    assert harness_question("Saved to story.txt.") is None
    long = "Here is what I found. " * 20 + "Should I keep the British spelling?"
    assert harness_question(long) == "Should I keep the British spelling?"


def _question_runner(example_config_dir: Path, agent_name: str, timeout_min: float = 30):
    config = load_config(example_config_dir)
    tools = config.tools.model_copy(update={"notifications": NotificationsConfig(question_timeout_min=timeout_min)})
    config = config.model_copy(update={"tools": tools})
    clock = Clock(AFTERNOON)
    notifications = NotificationService(config.tools.notifications, clock=clock)
    services = Services(
        timers=TimerService(TimersConfig()), weather=WeatherService(WeatherConfig()), notifications=notifications
    )
    runner = AgentRunner(config, services, TaskService(None), SpendLog(None))
    services.agents = runner
    return runner, notifications, clock


def test_a_model_agent_asks_and_gets_an_answer(example_config_dir: Path) -> None:
    async def scenario():
        runner, notifications, clock = _question_runner(example_config_dir, "helper")
        answers = []

        async def fake_call_model(agent, task, model, tools, history=None) -> AgentReply:
            ask_user = next(t for t in tools if t.name == "ask_user")
            outcome = await ask_user.handler({"question": "Vegetarian or not?"}, None)
            answers.append(outcome.result)
            return AgentReply(f"Here's a {outcome.result['answer']} plan.")

        runner._call_model = fake_call_model  # type: ignore[method-assign]
        outcome = await runner.ask("helper", "plan dinners", "web")
        await asyncio.sleep(0.05)
        task = runner.tasks.get(outcome.result["task_id"])
        waiting = (task.state, task.question, [(i.kind, i.priority, i.summary) for i in notifications.unread()])
        listed = await tasks_tool(runner.tasks, {"action": "list"})
        reply = await runner.ask("helper", "vegetarian", "web")  # "Tell Helper vegetarian"
        await _finish(runner)
        return waiting, listed, reply, answers, task, notifications

    waiting, listed, reply, answers, task, notifications = asyncio.run(scenario())
    assert waiting == ("waiting", "Vegetarian or not?", [("question", "urgent", "Vegetarian or not?")])
    assert listed.speak == "Helper is waiting for your answer: Vegetarian or not?"
    assert reply.speak == "Okay, I'll tell Helper."
    assert answers == [{"answer": "vegetarian"}] and task.state == "finished" and task.question is None
    # the question left the badge; the result came after a question, so it's a notification, not said at once
    assert [(i.kind, i.summary) for i in notifications.unread()] == [("result", "Here's a vegetarian plan.")]


def test_an_unanswered_question_tells_the_agent(example_config_dir: Path) -> None:
    async def scenario():
        runner, notifications, _ = _question_runner(example_config_dir, "helper", timeout_min=0.001)
        seen = []

        async def fake_call_model(agent, task, model, tools, history=None) -> AgentReply:
            outcome = await next(t for t in tools if t.name == "ask_user").handler({"question": "Which day?"}, None)
            seen.append(outcome.result)
            return AgentReply("I planned it for Saturday.")

        runner._call_model = fake_call_model  # type: ignore[method-assign]
        await runner.ask("helper", "plan a picnic", "web")
        await _finish(runner)
        return seen, notifications

    seen, notifications = asyncio.run(scenario())
    assert seen[0]["answer"] is None and "No answer" in seen[0]["note"]
    assert [i.kind for i in notifications.unread()] == ["result"]  # the question expired off the badge


def test_a_harness_question_continues_with_the_exchange(example_config_dir: Path) -> None:
    async def scenario():
        runner, notifications, _ = _question_runner(example_config_dir, "hermes")
        calls = []

        async def fake_call_model(agent, task, model, tools, history=None) -> AgentReply:
            calls.append(history)
            return AgentReply("Saved it to blue/story.txt." if history else "Which folder should I save it in?")

        runner._call_model = fake_call_model  # type: ignore[method-assign]
        runner._missing_key = lambda agent: None  # type: ignore[method-assign]
        outcome = await runner.ask("hermes", "write a story", "web")
        await asyncio.sleep(0.05)
        question = notifications.unread()[0]
        answered = runner.answer(question.task_id, "the blue folder")
        await _finish(runner)
        return calls, answered, runner.tasks.get(outcome.result["task_id"]), notifications

    calls, answered, task, notifications = asyncio.run(scenario())
    assert answered.speak == "Okay, I'll tell Hermes."
    assert calls == [
        None,
        [
            {"role": "user", "content": "write a story"},
            {"role": "assistant", "content": "Which folder should I save it in?"},
            {"role": "user", "content": "the blue folder"},
        ],
    ]
    assert task.state == "finished" and task.result == "Saved it to blue/story.txt."


def test_questions_are_asked_at_once_unless_the_assistant_is_speaking() -> None:
    async def scenario():
        service, clock = service_at(AFTERNOON)
        speaker = FakeOutlet(speaks=True)
        speaker.is_busy = True  # mid-conversation, but not talking right now
        speaker.speaking = lambda: talking[0]  # type: ignore[method-assign]
        talking = [True]
        service.attach(speaker)
        item = post(service, kind="question", headline="Hermes has a question.", summary="Which folder?", priority="urgent", task_id="t1")
        await service.tick()
        held = list(speaker.announced)
        talking[0] = False
        await service.tick()
        return held, speaker, item

    held, speaker, item = asyncio.run(scenario())
    assert held == []
    [asked] = speaker.announced
    assert asked.text == "Hermes has a question. Which folder?"
    assert asked.question is not None and (asked.question.kind, asked.question.task_id) == ("answer", "t1")
    assert item.state == "delivered"


def test_questions_wait_silently_in_quiet_hours() -> None:
    async def scenario():
        service, _ = service_at(NIGHT)
        speaker, screen = FakeOutlet(speaks=True), FakeOutlet(speaks=False)
        service.attach(speaker)
        service.attach(screen)
        item = post(service, kind="question", headline="Hermes has a question.", summary="Which folder?", priority="urgent", task_id="t1")
        await service.tick()
        speaker.count += 1
        await service.tick()
        return speaker, screen, item, service

    speaker, screen, item, service = asyncio.run(scenario())
    assert speaker.announced == [] and screen.announced == []  # the list only: no card, no sound, no "by the way"
    assert service.unread() == [item]


def test_reading_a_question_makes_the_next_turn_its_answer() -> None:
    async def scenario():
        service, _ = service_at(AFTERNOON)
        post(service, kind="question", headline="Hermes has a question.", summary="Which folder?", priority="urgent", task_id="t1")
        return await notifications_tool(service, {"action": "read"}, "web"), service

    outcome, service = asyncio.run(scenario())
    assert outcome.speak == "Hermes has a question. Which folder?"
    assert outcome.pending is not None and (outcome.pending.kind, outcome.pending.task_id) == ("answer", "t1")
    assert len(service.unread()) == 1  # still unread until it's answered


def test_questions_expire_with_a_restart(tmp_path: Path) -> None:
    database = migrate(tmp_path / "a.sqlite3")

    async def scenario():
        first, _ = service_at(AFTERNOON, database)
        post(first, kind="question", headline="Hermes has a question.", summary="Which folder?", priority="urgent", task_id="t1")
        second, _ = service_at(AFTERNOON, database)
        second.start()
        await second.close()
        return second

    assert asyncio.run(scenario()).unread() == []


def test_the_screen_can_acknowledge_and_answer() -> None:
    from pipecat.frames.frames import InputTransportMessageFrame

    from victoria_voice_assistant.chat.serializer import ChatJsonSerializer

    serializer = ChatJsonSerializer()

    async def scenario():
        return [
            await serializer.deserialize(text)
            for text in [
                '{"type": "notification_ack", "ids": "all"}',
                '{"type": "notification_ack", "ids": ["a1"]}',
                '{"type": "agent_answer", "task_id": "t1", "text": " the blue folder "}',
                '{"type": "agent_answer", "task_id": "t1", "text": "  "}',
                '{"type": "notification_ack", "ids": 3}',
            ]
        ]

    frames = asyncio.run(scenario())
    assert [f.message if isinstance(f, InputTransportMessageFrame) else None for f in frames] == [
        {"type": "notification_ack", "ids": "all"},
        {"type": "notification_ack", "ids": ["a1"]},
        {"type": "agent_answer", "task_id": "t1", "text": "the blue folder"},
        None,
        None,
    ]
