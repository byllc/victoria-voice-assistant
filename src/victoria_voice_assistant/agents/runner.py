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

from victoria_voice_assistant.agents.model_agent import AgentError, run_model_agent
from victoria_voice_assistant.agents.tasks import Task, TaskService
from victoria_voice_assistant.chat.llm import build_llm
from victoria_voice_assistant.config.schema import AgentConfig, AppConfig
from victoria_voice_assistant.tools.outcome import ToolOutcome
from victoria_voice_assistant.tools.registry import BUILTIN_TOOLS, ToolContext, build_registry

if TYPE_CHECKING:
    from victoria_voice_assistant.tools.registry import Services

logger = logging.getLogger(__name__)

# A task still running after this long is stopped and marked timed_out.
AGENT_TIMEOUT_S = 300.0


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
    def __init__(self, config: AppConfig, services: Services, tasks: TaskService) -> None:
        self.config = config
        self.services = services
        self.tasks = tasks

    def update_config(self, config: AppConfig) -> None:
        self.config = config

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
        if agent.kind != "model" or agent.model is None:
            # Pools, harness, A2A and relay agents arrive later in Stage 7 and beyond.
            logger.info("Agent kind not supported yet", extra={"event": "agent_unsupported", "agent": agent.name, "kind": agent.kind})
            return ToolOutcome(result={"error": "agent not set up yet"}, speak=f"{label(agent.name)} isn't set up yet.")
        task = self.tasks.create(agent.name, device, request, agent.mode, model=agent.model)
        job = asyncio.create_task(self._run(agent, task), name=f"agent-{task.id}")
        self.tasks.attach(task, job)
        ack = f"Asking {label(agent.name)}."
        if agent.mode == "background":
            ack += " I'll let you know when it's done."
        return ToolOutcome(result={"task_id": task.id, "agent": agent.name, "mode": agent.mode}, speak=ack)

    async def _run(self, agent: AgentConfig, task: Task) -> None:
        name = label(agent.name)
        loop = asyncio.get_running_loop()
        started = loop.time()
        notice = None
        if agent.mode == "auto" and agent.wait_s:
            notice = loop.call_later(
                agent.wait_s, self.tasks.say, task, f"{name} is still working on it. I'll let you know when it's done."
            )
        try:
            reply = await asyncio.wait_for(self._model_agent(agent, task), AGENT_TIMEOUT_S)
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
            self.tasks.say(task, f"{name} couldn't finish that.")
            return
        finally:
            if notice is not None:
                notice.cancel()
        delayed = agent.mode == "background" or (agent.mode == "auto" and loop.time() - started >= (agent.wait_s or 0))
        self.tasks.update(
            task,
            state="finished",
            result=reply.text,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )
        self.tasks.say(task, f"{name} has finished. {reply.text}" if delayed else reply.text)

    async def _model_agent(self, agent: AgentConfig, task: Task) -> Any:
        assert agent.model is not None
        registry = build_registry(self.config.tools, self.config.agents, self.services, log_stubs=False)
        tools = [
            entry
            for name in agent.tools
            if name not in BUILTIN_TOOLS and (entry := registry.get(name)) is not None and entry.status == "ready"
        ]
        llm = build_llm(agent.model, self.config.models)
        reply = await run_model_agent(llm, agent.system, task.request, tools, ToolContext(device=task.device))
        if not reply.text:
            raise AgentError("empty reply")
        return reply
