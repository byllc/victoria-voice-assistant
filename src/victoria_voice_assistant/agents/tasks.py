"""Task records for agent handoffs (architecture §10.2), kept in SQLite."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

TaskState = Literal["queued", "running", "waiting", "finished", "failed", "cancelled", "timed_out"]
ACTIVE_STATES = ("queued", "running", "waiting")


@dataclass
class Task:
    id: str
    agent: str
    device: str
    request: str
    mode: str
    state: TaskState = "running"
    model: str | None = None
    result: str | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    question: str | None = None  # what the agent is waiting for an answer to (state `waiting`)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def snapshot(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class TaskEvent:
    """`changed` for any state change; `say` carries text for the task's device (a result, a delay, an error).

    `text` is what's spoken; `full` is the complete reply for screens and the conversation, when it differs.
    """

    kind: Literal["changed", "say"]
    task: Task
    text: str | None = None
    full: str | None = None


TaskListener = Callable[[TaskEvent], Awaitable[None]]


class TaskService:
    def __init__(self, database_path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.database_path = database_path
        self._clock = clock
        self._tasks: dict[str, Task] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._listeners: list[TaskListener] = []
        self._pending: set[asyncio.Task] = set()
        self._answers: dict[str, asyncio.Future[str]] = {}

    def start(self) -> None:
        """Tasks left running when the service stopped can't be resumed: mark them failed."""
        if self.database_path is None:
            return
        now = self._clock()
        with self._connect() as connection:
            stale = connection.execute(
                f"UPDATE tasks SET state = 'failed', error = 'the assistant restarted', updated_at = ? "
                f"WHERE state IN ({', '.join('?' * len(ACTIVE_STATES))})",
                (now, *ACTIVE_STATES),
            ).rowcount
        if stale:
            logger.warning("Tasks interrupted by a restart", extra={"event": "tasks_interrupted", "count": stale})

    def add_listener(self, listener: TaskListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: TaskListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def create(self, agent: str, device: str, request: str, mode: str, model: str | None = None) -> Task:
        now = self._clock()
        task = Task(
            id=uuid.uuid4().hex[:12],
            agent=agent,
            device=device,
            request=request,
            mode=mode,
            model=model,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.id] = task
        self._save(task)
        logger.info("Task started", extra={"event": "task_started", "agent": agent, "task": task.id})
        self._emit(TaskEvent("changed", task))
        return task

    def attach(self, task: Task, job: asyncio.Task) -> None:
        self._jobs[task.id] = job
        job.add_done_callback(lambda _job: self._jobs.pop(task.id, None))

    def update(self, task: Task, **fields: object) -> None:
        """Change a task's fields (state, result, usage...), persist and announce it."""
        for key, value in fields.items():
            setattr(task, key, value)
        task.updated_at = self._clock()
        self._save(task)
        if not task.active:
            logger.info(
                "Task ended",
                extra={
                    "event": "task_ended",
                    "agent": task.agent,
                    "task": task.id,
                    "reason": task.state,
                    "model": task.model,
                    "count": task.input_tokens + task.output_tokens,
                    "duration_s": round(task.updated_at - task.created_at, 1),
                },
            )
        self._emit(TaskEvent("changed", task))

    def say(self, task: Task, text: str, full: str | None = None) -> None:
        self._emit(TaskEvent("say", task, text, full))

    def ask(self, task: Task, question: str) -> asyncio.Future[str]:
        """Put the task in `waiting` with its question; the future resolves with the answer."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._answers[task.id] = future
        logger.info("Agent asked a question", extra={"event": "agent_question", "agent": task.agent, "task": task.id})
        self.update(task, state="waiting", question=question)
        return future

    def resume(self, task: Task) -> None:
        """The question was answered or timed out: back to running."""
        self._answers.pop(task.id, None)
        if task.state == "waiting":
            self.update(task, state="running", question=None)

    def answer(self, task_id: str, text: str) -> bool:
        """Answer a waiting task's question. False if it isn't waiting."""
        future = self._answers.get(task_id)
        if future is None or future.done() or not text.strip():
            return False
        logger.info("Agent question answered", extra={"event": "agent_answered", "task": task_id})
        future.set_result(text.strip())
        return True

    def waiting(self, agent: str | None = None) -> list[Task]:
        """Tasks waiting for an answer, newest first."""
        found = [t for t in self._tasks.values() if t.state == "waiting" and (agent is None or t.agent == agent)]
        return sorted(found, key=lambda t: t.created_at, reverse=True)

    def cancel(self, task_id: str) -> bool:
        """Stop a running task. False if it isn't running."""
        task = self._tasks.get(task_id)
        job = self._jobs.get(task_id)
        if task is None or not task.active:
            return False
        if job is not None:
            job.cancel()  # the runner records `cancelled` as the job unwinds
        else:
            self.update(task, state="cancelled")
        return True

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def active(self, device: str | None = None) -> list[Task]:
        return [t for t in self._tasks.values() if t.active and (device is None or t.device == device)]

    def recent(self, limit: int = 20) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)[:limit]

    def _emit(self, event: TaskEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for listener in list(self._listeners):
            pending = loop.create_task(listener(event))
            self._pending.add(pending)
            pending.add_done_callback(self._pending.discard)

    # Persistence -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        assert self.database_path is not None
        return sqlite3.connect(self.database_path)

    def _save(self, task: Task) -> None:
        if self.database_path is None:
            return
        row = task.snapshot()
        columns = ", ".join(row)
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO tasks ({columns}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
