"""Hands requests to agents by name (architecture §9, §10.1) and tracks each one as a task.

The handoff is acknowledged at once ("Asking Helper."). The agent then runs in the
background, and its result, a delay notice or an error reaches the requesting device as
a `say` task event, which the chat and voice sessions deliver. `mode` decides the wording:
`wait` gives the bare result, `background` says it will report back, and `auto` stays
quiet for `wait_s` and then says it's still working.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from victoria_voice_assistant.agents.model_agent import AgentError, AgentReply, run_model_agent
from victoria_voice_assistant.agents.pools import PoolChoice, PoolChooser, split_override
from victoria_voice_assistant.agents.spend import SpendEntry, SpendLog, cost_usd
from victoria_voice_assistant.agents.speech import plain_text, spoken_version
from victoria_voice_assistant.agents.tasks import Task, TaskService
from victoria_voice_assistant.chat.llm import build_llm
from victoria_voice_assistant.config.schema import AgentConfig, AppConfig
from victoria_voice_assistant.decider.engine import build_decider
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome
from victoria_voice_assistant.tools.registry import BUILTIN_TOOLS, ToolContext, ToolEntry, build_registry

if TYPE_CHECKING:
    from victoria_voice_assistant.tools.registry import Services

logger = logging.getLogger(__name__)

# Where the chat note for an agent that isn't set up yet points.
SETUP_DOCS = {"anthropic": "docs/claude-setup.md", "harness": "docs/hermes.md"}

# A task still running after this long is stopped and marked timed_out (time spent waiting for an answer doesn't count).
AGENT_TIMEOUT_S = 300.0
# Questions a harness agent may ask in one task before its reply is taken as the result.
MAX_HARNESS_QUESTIONS = 3
NO_ANSWER = "No answer from the user. Continue with a sensible default, or stop and say what you need."

# A closing offer like these isn't a question the task is waiting on.
CLOSER = re.compile(
    r"\b(anything else|let me know|need anything|something else|how (else )?can i help|can i help with|"
    r"any (other|more) questions|would you like (me to do )?(anything|something))\b",
    re.IGNORECASE,
)


# A reply that opens like this is treated as the model declining, for escalate_on_failure.
REFUSAL = re.compile(
    r"^\W*(?:sorry[,.!]?\s*)?(?:but\s+)?i\s*(?:can(?:no|')?t|am unable|'m unable|am not able|'m not able|won't be able)\b",
    re.IGNORECASE,
)


def looks_like_refusal(text: str) -> bool:
    return bool(REFUSAL.match(text[:160]))


def harness_question(text: str) -> str | None:
    """Bill, 2026-10-31: a harness reply that ends with a question is the agent asking one (Hermes's gateway
    can't pause mid-request). Returns the question as it should be read out, or None for a final reply."""
    plain = plain_text(text).strip()
    if not plain.endswith("?"):
        return None
    sentences = re.split(r"(?<=[.!?])\s+", plain)
    if CLOSER.search(sentences[-1]):
        return None
    return plain if len(plain) <= 280 else sentences[-1]


def label(name: str) -> str:
    return name[:1].upper() + name[1:]


def match_agent_address(text: str, agents: list[AgentConfig]) -> tuple[AgentConfig, str] | None:
    """ "Ask Helper to plan dinners" -> (helper, "plan dinners"). Deterministic, no model (architecture §5 step 4)."""
    names = {alias.casefold(): agent for agent in agents for alias in [agent.name, *agent.aliases]}
    if not names:
        return None
    alternatives = "|".join(re.escape(alias) for alias in sorted(names, key=len, reverse=True))
    match = re.match(
        rf"^\W*(?:(?:hey\s+)?victoria\W+)?(?:please\s+)?(?:ask|tell|have)\s+({alternatives})\b[\s,:]*(?:to\s+)?(.+?)\W*$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None or not match.group(2).strip():
        return None
    return names[match.group(1).casefold()], match.group(2).strip()


class AgentRunner:
    def __init__(self, config: AppConfig, services: Services, tasks: TaskService, spend: SpendLog | None = None) -> None:
        self.config = config
        self.services = services
        self.tasks = tasks
        self.spend = spend or SpendLog(None)
        self.chooser = PoolChooser(config.models, build_decider(config.models), self.spend)
        self._limits: dict[str, asyncio.Timeout] = {}
        self._asked: set[str] = set()

    def update_config(self, config: AppConfig) -> None:
        self.config = config
        self.chooser = PoolChooser(config.models, build_decider(config.models), self.spend)

    def find(self, name: str) -> AgentConfig | None:
        wanted = name.casefold().strip()
        return next(
            (agent for agent in self.config.agents.agents if wanted in {a.casefold() for a in [agent.name, *agent.aliases]}),
            None,
        )

    async def ask(self, name: str, request: str, device: str, model: str | None = None) -> ToolOutcome:
        """Hand `request` to an agent. `model` picks a pool member directly (the dashboard's re-send)."""
        agent = self.find(name)
        if agent is None:
            return ToolOutcome(result={"error": f"no agent called {name!r}"}, speak=f"I don't know an agent called {name}.")
        waiting = self.tasks.waiting(agent.name)
        if waiting and model is None:
            # "Tell Hermes the blue folder": the agent asked something, so this is the answer.
            return self.answer(waiting[0].id, request)
        supported = (agent.kind == "model" and (agent.model or agent.pool)) or (agent.kind == "harness" and agent.model)
        if not supported:
            # A2A and relay agents come in later stages.
            logger.info("Agent kind not supported yet", extra={"event": "agent_unsupported", "agent": agent.name, "kind": agent.kind})
            return ToolOutcome(result={"error": "agent not set up yet"}, speak=f"{label(agent.name)} isn't set up yet.")
        missing = self._missing_key(agent)
        if missing:
            logger.warning("Agent model has no API key", extra={"event": "agent_unsupported", "agent": agent.name, "reason": missing})
            kind = "harness" if agent.kind == "harness" else self.config.models.servers[missing].type
            guide = SETUP_DOCS.get(kind, "config/examples/.env.example")
            return ToolOutcome(
                result={"error": f"no API key for server {missing!r}"},
                speak=f"{label(agent.name)} isn't set up yet.",
                chat=f"Add the API key for the `{missing}` server to config/.env and restart the assistant. Setup steps: {guide}.",
                chat_title=f"{agent.name}: setup",
            )
        override = None
        if agent.pool is not None:
            if self.chooser.over_budget(agent.pool):
                return self._budget_stop(agent, request)
            override, request = split_override(self.config.models.pools[agent.pool], request)
            if model is not None:
                override = model
        task = self.tasks.create(agent.name, device, request, agent.mode, model=agent.model or override)
        job = asyncio.create_task(self._run(agent, task, override), name=f"agent-{task.id}")
        self.tasks.attach(task, job)
        ack = f"Asking {label(agent.name)}."
        if agent.mode == "background":
            ack += " I'll let you know when it's done."
        return ToolOutcome(result={"task_id": task.id, "agent": agent.name, "mode": agent.mode}, speak=ack)

    async def retry(self, task_id: str, agent: str | None = None, model: str | None = None) -> ToolOutcome:
        """Run an earlier task's request again: as it was, with another agent, or on another pool member."""
        task = self.tasks.get(task_id)
        if task is None:
            return ToolOutcome(result={"error": "no such task"}, speak="I can't find that task.")
        if model is not None:
            target = self.find(agent or task.agent)
            pool = self.config.models.pools.get(target.pool) if target and target.pool else None
            if pool is None or model not in [member.model for member in pool.members]:
                return ToolOutcome(result={"error": f"{model!r} isn't in that agent's pool"}, speak="That model isn't available.")
        return await self.ask(agent or task.agent, task.request, task.device, model=model)

    def resend_choices(self, task: Task) -> dict[str, list[str]]:
        """Where the dashboard can re-send a task: other agents, and stronger members of its pool."""
        agents = [a.name for a in self.config.agents.agents if a.name != task.agent and a.kind in ("model", "harness")]
        agent = self.find(task.agent)
        stronger: list[str] = []
        if agent is not None and agent.pool is not None:
            members = [member.model for member in self.config.models.pools[agent.pool].members]
            if task.model in members:
                stronger = members[members.index(task.model) + 1 :]
        return {"agents": agents, "models": stronger}

    def describe(self, task: Task) -> str:
        """ "Claude (Sonnet)", or just "Hermes" for an agent with one model."""
        agent = self.find(task.agent)
        name = label(task.agent)
        if agent is not None and agent.pool is not None and task.model:
            return f"{name} ({self._member_label(agent, task.model)})"
        return name

    @staticmethod
    def _member_label(agent: AgentConfig, model: str) -> str:
        """ "claude-sonnet" in the claude pool -> "Sonnet"."""
        return label(model.removeprefix(f"{agent.pool}-"))

    def cancel_note(self, task: Task) -> str | None:
        """Architecture §10.2: what cancelling can and can't stop."""
        agent = self.find(task.agent)
        if agent is not None and agent.kind == "harness":
            return f"Stops the reply; {label(task.agent)} may finish work it already started."
        return None

    def answer(self, task_id: str, text: str) -> ToolOutcome:
        task = self.tasks.get(task_id)
        if task is None or not self.tasks.answer(task_id, text):
            return ToolOutcome(result={"error": "that question isn't waiting any more"}, speak="That question has already closed.")
        return ToolOutcome(result={"answered": task_id}, speak=f"Okay, I'll tell {label(task.agent)}.")

    async def _ask_user(self, agent: AgentConfig, task: Task, question: str) -> str | None:
        """Ask the user on the agent's behalf and wait up to `question_timeout_min`. None if nobody answers."""
        settings = self.config.tools.notifications
        notifications = self.services.notifications
        name = label(agent.name)
        self._asked.add(task.id)
        future = self.tasks.ask(task, question)
        self.tasks.progress(task, f"Asked: {question}")
        if notifications is not None:
            notifications.post(
                source=agent.name,
                kind="question",
                headline=f"{name} has a question.",
                summary=question,
                full=question,
                target=task.device,
                priority="urgent",
                task_id=task.id,
            )
        else:
            self.tasks.say(task, f"{name} has a question. {question}")
        limit = self._limits.get(task.id)
        if limit is not None:
            limit.reschedule(None)  # waiting for us doesn't count against the agent's time
        answer: str | None = None
        try:
            answer = await asyncio.wait_for(future, settings.question_timeout_min * 60)
        except TimeoutError:
            logger.info("Agent question went unanswered", extra={"event": "agent_question_timeout", "task": task.id})
        finally:
            if answer is not None:
                self.tasks.progress(task, f"Your answer: {answer}")
            elif not future.cancelled():
                self.tasks.progress(task, "No answer")
            self.tasks.resume(task)
            if notifications is not None:
                notifications.close_question(task.id, answered=answer is not None)
            if limit is not None:
                limit.reschedule(asyncio.get_running_loop().time() + AGENT_TIMEOUT_S)
        return answer

    def _missing_key(self, agent: AgentConfig) -> str | None:
        """The name of a cloud server this agent needs whose API key isn't set, if any."""
        models = self.config.models
        names = [agent.model] if agent.model else [m.model for m in models.pools[agent.pool].members] if agent.pool else []
        for name in names:
            server_name = models.models[name].server
            server = models.servers[server_name]
            # Cloud models and agent harnesses (Hermes checks its bearer token) need a key; LM Studio doesn't.
            if (server.type == "anthropic" or agent.kind == "harness") and not server.api_key:
                return server_name
        return None

    def _budget_stop(self, agent: AgentConfig, request: str) -> ToolOutcome:
        """Past `stop_at`: no paid call. Offer the default agent instead, if it's a different one."""
        logger.warning("Pool budget used up", extra={"event": "budget_stop", "agent": agent.name})
        notice = f"We've hit this month's {label(agent.name)} budget."
        default = self.find(self.config.agents.default or "")
        if default is None or default.name == agent.name:
            return ToolOutcome(result={"error": "budget used up"}, speak=notice)
        question = f"{notice} Want me to ask {label(default.name)}?"
        return ToolOutcome(
            result={"error": "budget used up", "offered": default.name},
            speak=question,
            pending=PendingQuestion(kind="yes_no", question=question, on_yes=("ask_agent", {"agent": default.name, "task": request})),
        )

    async def _run(self, agent: AgentConfig, task: Task, override: str | None = None) -> None:
        name = label(agent.name)
        loop = asyncio.get_running_loop()
        started = loop.time()
        notice = None
        if agent.mode == "auto" and agent.wait_s:
            notice = loop.call_later(
                agent.wait_s, self.tasks.say, task, f"{name} is still working on it. I'll let you know when it's done."
            )
        try:
            async with asyncio.timeout(AGENT_TIMEOUT_S) as limit:
                self._limits[task.id] = limit
                reply = await self._model_agent(agent, task, override)
        except asyncio.CancelledError:
            self.tasks.progress(task, "Cancelled")
            self.tasks.update(task, state="cancelled")
            raise
        except TimeoutError:
            self.tasks.progress(task, "Stopped: took too long")
            self.tasks.update(task, state="timed_out", error=f"no result after {AGENT_TIMEOUT_S:.0f} s")
            self._report(agent, task, "error", f"{name} took too long, so I stopped it.", delayed=True)
            return
        except Exception as exc:  # noqa: BLE001 - any agent failure is reported, never raised into the session
            logger.warning("Agent failed", extra={"event": "agent_failed", "agent": agent.name, "reason": str(exc)})
            self.tasks.progress(task, f"Failed: {str(exc) or type(exc).__name__}")
            self.tasks.update(task, state="failed", error=str(exc) or type(exc).__name__)
            unreachable = "connection" in str(exc).lower()
            text = f"I can't reach {name} right now." if unreachable else f"{name} couldn't finish that."
            self._report(agent, task, "error", text, delayed=self._delayed(agent, loop.time() - started))
            return
        finally:
            self._limits.pop(task.id, None)
            if notice is not None:
                notice.cancel()
        delayed = self._delayed(agent, loop.time() - started) or task.id in self._asked
        self._asked.discard(task.id)
        self.tasks.progress(task, "Finished")
        self.tasks.update(task, state="finished", result=reply.text)
        spoken = await spoken_version(reply.text, self._summarizer())
        if delayed:
            self._report(agent, task, "result", f"{name} has finished.", delayed=True, summary=spoken, full=reply.text)
        else:
            self.tasks.say(task, spoken, full=reply.text if spoken != reply.text else None)

    @staticmethod
    def _delayed(agent: AgentConfig, elapsed_s: float) -> bool:
        """Whether the requester has stopped waiting: background mode, or auto mode past wait_s."""
        return agent.mode == "background" or (agent.mode == "auto" and elapsed_s >= (agent.wait_s or 0))

    def _report(
        self,
        agent: AgentConfig,
        task: Task,
        kind: str,
        headline: str,
        delayed: bool,
        summary: str = "",
        full: str | None = None,
    ) -> None:
        """A result or error. Said straight away while the requester waits; afterwards it's a notification
        (architecture §10.1 step 3: never just spoken into an empty room and lost)."""
        notifications = self.services.notifications
        if not delayed or notifications is None:
            text = f"{headline} {summary}".strip()
            self.tasks.say(task, text, full=f"{headline} {full}".strip() if full and full != summary else None)
            return
        notifications.post(
            source=agent.name,
            kind=kind,  # type: ignore[arg-type]
            headline=headline,
            summary=summary,
            full=full,
            target=task.device,
            style=agent.notify,
            task_id=task.id,
        )

    def _summarizer(self) -> Any:
        role = self.config.models.roles.summarizer
        if not role or role not in self.config.models.models:
            return None
        try:
            return build_llm(role, self.config.models)
        except ValueError:
            return None

    async def _model_agent(self, agent: AgentConfig, task: Task, override: str | None = None) -> AgentReply:
        """Run the agent on its model, or on the pool member chosen for this request, escalating once if allowed."""
        if agent.kind == "harness":
            assert agent.model is not None
            return await self._harness(agent, task, agent.model)
        if agent.pool is None:
            assert agent.model is not None
            return await self._attempt(agent, task, agent.model, reason="fixed")
        choice = await self.chooser.choose(agent.pool, task.request, override)
        if choice.model is not None:
            why = {"decider": f"picked by the decider ({choice.confidence:.0%} sure)" if choice.confidence else "picked by the decider",
                   "override": "asked for by name", "low_confidence": "the default (decider unsure)",
                   "budget_downgrade": "the default (budget nearly used)"}.get(choice.reason, choice.reason)
            self.tasks.progress(task, f"{self._member_label(agent, choice.model)}: {why}")
        if choice.model is None:
            raise AgentError("this month's budget is used up")
        pool = self.config.models.pools[agent.pool]
        model, reason = choice.model, choice.reason
        escalated = False
        while True:
            try:
                reply = await self._attempt(agent, task, model, reason, choice)
                failure: Exception | None = AgentError("the model declined") if looks_like_refusal(reply.text) else None
            except AgentError as exc:
                reply, failure = None, exc
            # Architecture §7.1: retry once, one step up.
            step_up = self.chooser.escalate(agent.pool, model) if failure and pool.escalate_on_failure and not escalated else None
            if step_up is None:
                if reply is not None:
                    return reply  # a refusal with nowhere to escalate is still the answer
                assert failure is not None
                raise failure
            logger.info(
                "Escalating to the next model",
                extra={"event": "agent_escalated", "agent": agent.name, "model": step_up, "reason": str(failure)},
            )
            self.tasks.progress(task, f"Trying {self._member_label(agent, step_up)} ({failure})")
            model, reason, choice, escalated = step_up, "escalation", None, True

    async def _harness(self, agent: AgentConfig, task: Task, model: str) -> AgentReply:
        """A harness reply ending in a question waits for the answer, then continues with the earlier exchange."""
        history: list[dict[str, str]] | None = None
        input_tokens = output_tokens = 0
        for asked in range(MAX_HARNESS_QUESTIONS + 1):
            reply = await self._attempt(agent, task, model, reason="fixed", history=history)
            input_tokens += reply.input_tokens
            output_tokens += reply.output_tokens
            question = harness_question(reply.text) if asked < MAX_HARNESS_QUESTIONS else None
            if question is None:
                break
            answer = await self._ask_user(agent, task, question)
            if answer is None:
                break  # nobody answered: the reply so far is the result, with no further call (decisions log)
            self.tasks.progress(task, f"Sent your answer to {label(agent.name)}")
            history = [*(history or [{"role": "user", "content": task.request}]),
                       {"role": "assistant", "content": reply.text}, {"role": "user", "content": answer}]
        return AgentReply(reply.text, input_tokens, output_tokens)

    def _ask_user_entry(self, agent: AgentConfig, task: Task) -> ToolEntry:
        """The `ask_user` tool every model agent gets (architecture §10.3: agent questions)."""

        async def handler(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
            question = str(arguments.get("question") or "").strip()
            if not question:
                return ToolOutcome(result={"error": "no question given"})
            answer = await self._ask_user(agent, task, question)
            return ToolOutcome(result={"answer": answer} if answer is not None else {"answer": None, "note": NO_ANSWER})

        return ToolEntry(
            name="ask_user",
            description=(
                "Ask the user one short question when you can't finish without their answer. It may take a while. "
                "Returns their answer, or null if nobody answered."
            ),
            kind="builtin",
            status="ready",
            handler=handler,
            properties={"question": {"type": "string", "description": "one short question, as it will be read aloud"}},
            required=["question"],
        )

    async def _attempt(
        self,
        agent: AgentConfig,
        task: Task,
        model: str,
        reason: str,
        choice: PoolChoice | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> AgentReply:
        tools = []
        if agent.kind == "model":  # a harness (Hermes) runs its own tools; it gets the request only
            registry = build_registry(self.config.tools, self.config.agents, self.services, log_stubs=False)
            tools = [
                entry
                for name in agent.tools
                if name not in BUILTIN_TOOLS and (entry := registry.get(name)) is not None and entry.status == "ready"
            ]
            tools.append(self._ask_user_entry(agent, task))
        self.tasks.update(task, model=model)
        reply = await self._call_model(agent, task, model, tools, history)
        cost = cost_usd(self.config.models.models[model], reply.input_tokens, reply.output_tokens)
        self.spend.record(
            SpendEntry(
                agent=agent.name,
                model=model,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                cost_usd=cost,
                pool=agent.pool,
                task_id=task.id,
                reason=reason,
                decider_choice=choice.decider_choice if choice else None,
                confidence=choice.confidence if choice else None,
            )
        )
        self.tasks.update(
            task,
            input_tokens=task.input_tokens + reply.input_tokens,
            output_tokens=task.output_tokens + reply.output_tokens,
            cost_usd=task.cost_usd + cost,
        )
        if not reply.text:
            raise AgentError("empty reply")
        return reply

    async def _call_model(
        self, agent: AgentConfig, task: Task, model: str, tools: list, history: list[dict[str, str]] | None = None
    ) -> AgentReply:
        llm = build_llm(model, self.config.models)
        context = ToolContext(device=task.device, progress=lambda text: self.tasks.progress(task, text))
        return await run_model_agent(llm, agent.system, task.request, tools, context, history=history)
