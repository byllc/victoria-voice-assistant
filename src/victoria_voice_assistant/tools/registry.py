from __future__ import annotations

import importlib
import inspect
import json
import logging
import string
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

import httpx
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema

from victoria_voice_assistant.config.schema import AgentsConfig, ToolConfig, ToolsConfig
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome
from victoria_voice_assistant.tools.timers import TimerService
from victoria_voice_assistant.tools.weather import WeatherService

if TYPE_CHECKING:
    from victoria_voice_assistant.search.service import PageReader, SearchService

logger = logging.getLogger(__name__)

JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
DEFAULT_HTTP_TIMEOUT_S = 5.0
HTTP_RESULT_CHARS = 2000
# Kinds that exist in the architecture but are built in later stages.
LATER_KINDS = {"mcp": "MCP tools", "ha_service": "Home Assistant service calls", "ha_script": "Home Assistant scripts"}
BUILTIN_TOOLS = ("clarify", "offer_agent", "ask_agent")

Status = Literal["ready", "stub", "auto_stub", "disabled"]
ReplyMode = Literal["model", "template", "silent"]


@dataclass
class ToolContext:
    """Per-call information a handler may need."""

    device: str = "web"


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolOutcome]]


@dataclass
class Services:
    """Built-in services shared by every session."""

    timers: TimerService
    weather: WeatherService
    http: httpx.AsyncClient | None = None
    search: SearchService | None = None
    reader: PageReader | None = None


@dataclass
class ToolEntry:
    name: str
    description: str
    kind: str
    status: Status
    handler: ToolHandler
    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    reply: ReplyMode = "model"
    template: str | None = None
    timeout_s: float | None = None
    reason: str | None = None
    # Spoken output from these is never re-phrased by the model (stubs and built-ins).
    speaks_directly: bool = False

    def schema(self, handler: Any = None) -> FunctionSchema:
        return FunctionSchema(
            name=self.name,
            description=self.description,
            properties=self.properties,
            required=self.required,
            handler=handler,
        )

    def openai_tool(self) -> dict[str, Any]:
        return {"type": "function", "function": self.schema().to_default_dict()}


@dataclass
class ToolRegistry:
    entries: list[ToolEntry]
    default_agent: str | None = None
    agent_names: list[str] = field(default_factory=list)

    @property
    def active(self) -> list[ToolEntry]:
        """Tools the model sees: everything except disabled ones."""
        return [entry for entry in self.entries if entry.status != "disabled"]

    def get(self, name: str) -> ToolEntry | None:
        return next((entry for entry in self.entries if entry.name == name), None)

    def stubs(self) -> list[ToolEntry]:
        return [entry for entry in self.entries if entry.status in ("stub", "auto_stub")]


def _title(text: str) -> str:
    return text[:1].upper() + text[1:]


def _speakable(name: str) -> str:
    return name.replace("_", " ")


class _SafeFormat(dict):
    def __missing__(self, key: str) -> str:
        return ""


def format_template(template: str, arguments: dict[str, Any], result: Any) -> str:
    values = _SafeFormat(arguments)
    if isinstance(result, dict):
        values.update(result)
    values["result"] = result
    try:
        return string.Formatter().vformat(template, (), values)
    except (IndexError, KeyError, ValueError, AttributeError, TypeError):
        return template


def _properties(tool: ToolConfig) -> tuple[dict[str, Any], list[str]]:
    """Turn `params` into JSON Schema properties. `required: true` on a param marks it required."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in tool.params.items():
        spec = dict(spec)
        if spec.pop("required", False):
            required.append(name)
        kind = spec.get("type", "string")
        if kind not in JSON_TYPES:
            raise ValueError(f"param {name!r} has unknown type {kind!r}")
        spec["type"] = kind
        properties[name] = spec
    return properties, required


def _reply_mode(tool: ToolConfig) -> tuple[ReplyMode, str | None]:
    reply = tool.reply
    if reply is None or reply.model:
        return "model", None
    if reply.silent:
        return "silent", None
    if reply.template:
        return "template", reply.template if isinstance(reply.template, str) else None
    return "model", None


# Built-in tools ---------------------------------------------------------------


def _agent_label(name: str) -> str:
    return _title(name)


def builtin_entries(agents: AgentsConfig) -> list[ToolEntry]:
    names = [agent.name for agent in agents.agents]
    default = agents.default
    agent_param: dict[str, Any] = {"type": "string", "description": "agent name"}
    if names:
        agent_param["enum"] = names

    async def clarify(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
        question = str(arguments.get("question") or "Could you say that another way?").strip()
        options = [str(option) for option in arguments.get("options") or []]
        return ToolOutcome(
            result={"asked": question},
            speak=question,
            pending=PendingQuestion(kind="clarify", question=question, options=options),
        )

    async def ask_agent(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
        agent = str(arguments.get("agent") or default or "")
        logger.info(
            "Agent requested before agents exist",
            extra={"event": "agent_requested", "agent": agent, "arguments": arguments},
        )
        return ToolOutcome(
            result={"error": "agents are not set up yet", "agent": agent},
            speak="Agents aren't set up yet.",
        )

    async def offer_agent(arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        agent = str(arguments.get("agent") or "")
        if agent not in names:
            agent = default or (names[0] if names else "")
        if not agent:
            return ToolOutcome(result={"offered": None}, speak="Sorry, I can't do that.")
        question = f"I can't do that myself. Want me to ask {_agent_label(agent)}?"
        summary = str(arguments.get("summary") or "")
        return ToolOutcome(
            result={"offered": agent},
            speak=question,
            pending=PendingQuestion(
                kind="yes_no",
                question=question,
                on_yes=("ask_agent", {"agent": agent, "task": summary}),
            ),
        )

    return [
        ToolEntry(
            name="clarify",
            description=(
                "Ask the user one short question when a required detail is missing or the request is ambiguous."
            ),
            kind="builtin",
            status="ready",
            handler=clarify,
            properties={
                "question": {"type": "string", "description": "one short spoken question"},
                "options": {"type": "array", "items": {"type": "string"}, "description": "choices, if any"},
            },
            required=["question"],
            speaks_directly=True,
        ),
        ToolEntry(
            name="ask_agent",
            description="Hand the request to a named agent when the user asks for one (for example 'ask helper ...').",
            kind="builtin",
            status="ready",
            handler=ask_agent,
            properties={"agent": agent_param, "task": {"type": "string", "description": "what the agent should do"}},
            required=["agent", "task"],
            speaks_directly=True,
        ),
        ToolEntry(
            name="offer_agent",
            description="No tool fits and you can't answer: offer to pass the request to an agent.",
            kind="builtin",
            status="ready",
            handler=offer_agent,
            properties={
                "agent": agent_param,
                "summary": {"type": "string", "description": "one-line summary of the request"},
            },
            required=["summary"],
            speaks_directly=True,
        ),
    ]


# Configured tools ---------------------------------------------------------------


class Unresolved(Exception):
    """The tool's implementation can't be found; the tool becomes an automatic stub."""


def _stub_handler(tool: ToolConfig, reason: str | None, builtins: list[ToolEntry], default_agent: str | None) -> ToolHandler:
    notice = tool.notice or f"{_title(_speakable(tool.name))} isn't set up yet."
    setup = tool.setup or ""
    if reason:
        setup = f"{setup}\n\nMissing: {reason}".strip()

    async def handler(arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        logger.warning(
            "Stub tool called",
            extra={"event": "stub_called", "tool": tool.name, "arguments": arguments, "reason": reason},
        )
        outcome = ToolOutcome(
            result={"error": f"{tool.name} is not set up yet"},
            speak=notice,
            chat=setup or None,
            chat_title=f"{tool.name}: setup",
        )
        then = tool.then or {}
        follow_up = None
        if then.get("offer_agent"):
            offer = next(entry for entry in builtins if entry.name == "offer_agent")
            follow_up = await offer.handler({"agent": default_agent or "", "summary": f"{tool.name} {json.dumps(arguments)}"}, ctx)
            # The offer stands on its own sentence after the notice.
            if follow_up.speak:
                follow_up.speak = follow_up.speak.replace("I can't do that myself. ", "")
        elif isinstance(then.get("ask_agent"), str):
            ask = next(entry for entry in builtins if entry.name == "ask_agent")
            follow_up = await ask.handler({"agent": then["ask_agent"], "task": json.dumps(arguments)}, ctx)
        if follow_up is not None:
            outcome.speak = f"{notice} {follow_up.speak}".strip()
            outcome.pending = follow_up.pending
        return outcome

    return handler


def _builtin_handler(tool: ToolConfig, tools: ToolsConfig, services: Services) -> ToolHandler:
    service = tool.service
    if service == "timers":

        async def timers(arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            return await services.timers.handle(ctx.device, arguments)

        return timers
    if service == "weather":
        if tools.weather is None:
            raise Unresolved("the `weather:` section is missing from tools.yaml")

        async def weather(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
            return await services.weather.handle(arguments)

        return weather
    if service in ("search", "search.read"):
        if tools.search is None:
            raise Unresolved("the `search:` section is missing from tools.yaml")
        if service == "search":

            async def web_search(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
                if services.search is None:
                    return ToolOutcome(result={"error": "search unavailable"}, speak="I couldn't search right now.", error=True)
                return await services.search.search(str(arguments.get("query") or ""))

            return web_search

        async def web_read(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
            if services.reader is None:
                return ToolOutcome(result={"error": "page reading unavailable", "instead": "answer from the search snippets"})
            return await services.reader.read(str(arguments.get("url") or ""))

        return web_read
    if service is None:
        raise Unresolved("built-in tool has no `service`")
    raise Unresolved(f"built-in service `{service}` isn't available yet")


def _python_handler(tool: ToolConfig) -> ToolHandler:
    target = tool.service
    if not target:
        raise Unresolved("python tool has no `service` (expected `package.module:function`)")
    module_name, _, attribute = target.partition(":")
    if not attribute:
        module_name, _, attribute = target.rpartition(".")
    try:
        function = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError, ValueError) as exc:
        raise Unresolved(f"handler `{target}` not found") from exc
    if not callable(function):
        raise Unresolved(f"handler `{target}` is not callable")

    async def handler(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
        value = function(**arguments)
        if inspect.isawaitable(value):
            value = await value
        return value if isinstance(value, ToolOutcome) else ToolOutcome(result=value)

    return handler


def _http_handler(tool: ToolConfig, services: Services) -> ToolHandler:
    spec = tool.http
    if spec is None:
        raise Unresolved("http tool has no `http:` section with a url")
    timeout = tool.timeout_s or DEFAULT_HTTP_TIMEOUT_S

    def fill(value: Any, arguments: dict[str, Any]) -> Any:
        if isinstance(value, str):
            return format_template(value, arguments, None)
        if isinstance(value, dict):
            return {key: fill(child, arguments) for key, child in value.items()}
        if isinstance(value, list):
            return [fill(child, arguments) for child in value]
        return value

    async def handler(arguments: dict[str, Any], _ctx: ToolContext) -> ToolOutcome:
        client = services.http or httpx.AsyncClient()
        try:
            response = await client.request(
                spec.method,
                fill(spec.url, arguments),
                params=fill(spec.query, arguments),
                headers=fill(spec.headers, arguments),
                json=fill(spec.body, arguments) if spec.body is not None else None,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "HTTP tool failed",
                extra={"event": "http_tool_failed", "tool": tool.name, "error": str(exc) or type(exc).__name__},
            )
            return ToolOutcome(
                result={"error": "service unavailable"},
                speak=f"I couldn't reach the {_speakable(tool.name)} service.",
                error=True,
            )
        finally:
            if services.http is None:
                await client.aclose()
        try:
            result: Any = response.json()
        except ValueError:
            result = response.text[:HTTP_RESULT_CHARS]
        return ToolOutcome(result=result)

    return handler


def _resolve(tool: ToolConfig, tools: ToolsConfig, services: Services) -> ToolHandler:
    if tool.broken:
        raise Unresolved(tool.broken)
    if tool.kind in LATER_KINDS:
        raise Unresolved(f"{LATER_KINDS[tool.kind]} (kind `{tool.kind}`) aren't supported yet")
    if tool.kind == "builtin":
        return _builtin_handler(tool, tools, services)
    if tool.kind == "python":
        return _python_handler(tool)
    if tool.kind == "http":
        return _http_handler(tool, services)
    raise Unresolved(f"unknown kind `{tool.kind}`")


def build_registry(
    tools: ToolsConfig,
    agents: AgentsConfig,
    services: Services,
    log_stubs: bool = False,
) -> ToolRegistry:
    """Build the tools for one session. ``log_stubs`` warns about automatic stubs (startup and reload)."""
    builtins = builtin_entries(agents)
    entries: list[ToolEntry] = []
    for tool in tools.tools:
        if tool.name in BUILTIN_TOOLS:
            logger.warning(
                "Tool name clashes with a built-in tool; ignored",
                extra={"event": "tool_name_reserved", "tool": tool.name},
            )
            continue
        reply, template = _reply_mode(tool)
        try:
            properties, required = _properties(tool)
        except ValueError as exc:
            properties, required = {}, []
            tool = tool.model_copy(update={"broken": f"invalid params: {exc}"})
        status: Status
        reason = None
        if tool.kind == "stub" and not tool.broken:
            status = "stub"
            handler = _stub_handler(tool, None, builtins, agents.default)
        else:
            try:
                handler = _resolve(tool, tools, services)
                status = "ready"
            except Unresolved as exc:
                reason = str(exc)
                status = "auto_stub"
                handler = _stub_handler(tool, reason, builtins, agents.default)
                if log_stubs:
                    logger.warning(
                        "Tool became an automatic stub",
                        extra={"event": "tool_auto_stub", "tool": tool.name, "reason": reason},
                    )
        if not tool.enabled:
            status = "disabled"
        entries.append(
            ToolEntry(
                name=tool.name,
                description=tool.description,
                kind=tool.kind,
                status=status,
                handler=handler,
                properties=properties,
                required=required,
                reply=reply,
                template=template,
                timeout_s=tool.timeout_s,
                reason=reason,
                speaks_directly=status in ("stub", "auto_stub"),
            )
        )
    return ToolRegistry(
        entries=entries + builtins,
        default_agent=agents.default,
        agent_names=[agent.name for agent in agents.agents],
    )
