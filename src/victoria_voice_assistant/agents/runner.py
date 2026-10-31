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
from victoria_voice_assistant.agents.speech import spoken_version
from victoria_voice_assistant.agents.tasks import Task, TaskService
from victoria_voice_assistant.chat.llm import build_llm
from victoria_voice_assistant.config.schema import AgentConfig, AppConfig
from victoria_voice_assistant.decider.engine import build_decider
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome
from victoria_voice_assistant.tools.registry import BUILTIN_TOOLS, ToolContext, build_registry

if TYPE_CHECKING:
    from victoria_voice_assistant.tools.registry import Services

logger = logging.getLogger(__name__)

# A task still running after this long is stopped and marked timed_out.
AGENT_TIMEOUT_S = 300.0


# A reply that opens like this is treated as the model declining, for escalate_on_failure.
REFUSAL = re.compile(
    r"^\W*(?:sorry[,.!]?\s*)?(?:but\s+)?i\s*(?:can(?:no|')?t|am unable|'m unable|am not able|'m not able|won't be able)\b",
    re.IGNORECASE,
)


def looks_like_refusal(text: str) -> bool:
    return bool(REFUSAL.match(text[:160]))


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

    def update_config(self, config: AppConfig) -> None:
        self.config = config
        self.chooser = PoolChooser(config.models, build_decider(config.models), self.spend)

    def find(self, name: str) -> AgentConfig | None:
        wanted = name.casefold().strip()
        return next(
            (agent for agent in self.config.agents.agents if wanted in {a.casefold() for a in [agent.name, *agent.aliases]}),
            None,
        )

    async def ask(self, name: str, request: str, device: str) -> ToolOutcome:
        agent = self.find(name)
        if agent is None:
            return ToolOutcome(result={"error": f"no agent called {name!r}"}, speak=f"I don't know an agent called {name}.")
        supported = (agent.kind == "model" and (agent.model or agent.pool)) or (agent.kind == "harness" and agent.model)
        if not supported:
            # A2A and relay agents come in later stages.
            logger.info("Agent kind not supported yet", extra={"event": "agent_unsupported", "agent": agent.name, "kind": agent.kind})
            return ToolOutcome(result={"error": "agent not set up yet"}, speak=f"{label(agent.name)} isn't set up yet.")
        missing = self._missing_key(agent)
        if missing:
            logger.warning("Agent model has no API key", extra={"event": "agent_unsupported", "agent": agent.name, "reason": missing})
            return ToolOutcome(
                result={"error": f"no API key for server {missing!r}"},
                speak=f"{label(agent.name)} isn't set up yet.",
                chat=f"Add the API key for the `{missing}` server to config/.env (see config/examples/.env.example).",
                chat_title=f"{agent.name}: setup",
            )
        override = None
        if agent.pool is not None:
            if self.chooser.over_budget(agent.pool):
                return self._budget_stop(agent, request)
            override, request = split_override(self.config.models.pools[agent.pool], request)
        task = self.tasks.create(agent.name, device, request, agent.mode, model=agent.model or override)
        job = asyncio.create_task(self._run(agent, task, override), name=f"agent-{task.id}")
        self.tasks.attach(task, job)
        ack = f"Asking {label(agent.name)}."
        if agent.mode == "background":
            ack += " I'll let you know when it's done."
        return ToolOutcome(result={"task_id": task.id, "agent": agent.name, "mode": agent.mode}, speak=ack)

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
            reply = await asyncio.wait_for(self._model_agent(agent, task, override), AGENT_TIMEOUT_S)
        except asyncio.CancelledError:
            self.tasks.update(task, state="cancelled")
            raise
        except TimeoutError:
            self.tasks.update(task, state="timed_out", error=f"no result after {AGENT_TIMEOUT_S:.0f} s")
            self.tasks.say(task, f"{name} took too long, so I stopped it.")
            return
        except Exception as exc:  # noqa: BLE001 - any agent failure is reported, never raised into the session
            logger.warning("Agent failed", extra={"event": "agent_failed", "agent": agent.name, "reason": str(exc)})
            self.tasks.update(task, state="failed", error=str(exc) or type(exc).__name__)
            unreachable = "connection" in str(exc).lower()
            self.tasks.say(task, f"I can't reach {name} right now." if unreachable else f"{name} couldn't finish that.")
            return
        finally:
            if notice is not None:
                notice.cancel()
        delayed = agent.mode == "background" or (agent.mode == "auto" and loop.time() - started >= (agent.wait_s or 0))
        self.tasks.update(task, state="finished", result=reply.text)
        spoken = await spoken_version(reply.text, self._summarizer())
        prefix = f"{name} has finished. " if delayed else ""
        self.tasks.say(task, prefix + spoken, full=prefix + reply.text if spoken != reply.text else None)

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
        if agent.pool is None:
            assert agent.model is not None
            return await self._attempt(agent, task, agent.model, reason="fixed")
        choice = await self.chooser.choose(agent.pool, task.request, override)
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
            model, reason, choice, escalated = step_up, "escalation", None, True

    async def _attempt(
        self, agent: AgentConfig, task: Task, model: str, reason: str, choice: PoolChoice | None = None
    ) -> AgentReply:
        tools = []
        if agent.kind == "model":  # a harness (Hermes) runs its own tools; it gets the request only
            registry = build_registry(self.config.tools, self.config.agents, self.services, log_stubs=False)
            tools = [
                entry
                for name in agent.tools
                if name not in BUILTIN_TOOLS and (entry := registry.get(name)) is not None and entry.status == "ready"
            ]
        self.tasks.update(task, model=model)
        reply = await self._call_model(agent, task, model, tools)
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

    async def _call_model(self, agent: AgentConfig, task: Task, model: str, tools: list) -> AgentReply:
        llm = build_llm(model, self.config.models)
        return await run_model_agent(llm, agent.system, task.request, tools, ToolContext(device=task.device))
