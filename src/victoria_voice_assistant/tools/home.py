"""Home control through Home Assistant (architecture §8; Stage 9).

`home_control` keeps one small tool for the fast model (action, target, value) and maps it onto
Home Assistant's MCP Server intents (HassTurnOn, HassTurnOff, HassLightSet,
HassClimateSetTemperature) through Pipecat's MCPClient. Locks use `ha_service` tools (direct
service calls) with a spoken confirmation.

Only entities carrying the configured label (`voice`) can be controlled. Home Assistant's MCP
server knows only "exposed to Assist", so every target is resolved here first, against the
labelled entities read from Home Assistant's registries, and named exactly in the intent call.
Replies are templates; no second model call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from victoria_voice_assistant.config.schema import McpServerConfig, ToolConfig
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome

logger = logging.getLogger(__name__)

REGISTRY_TTL_S = 60.0
CONFIRM_TTL_S = 30.0
INTENTS = ["HassTurnOn", "HassTurnOff", "HassLightSet", "HassClimateSetTemperature"]
# Words that don't help tell entities apart.
FILLER = {"the", "a", "my", "our", "please", "light", "lights", "lamp", "lamps", "switch", "all", "in", "room"}
NUMBER_WORDS = {"half": 50, "full": 100, "max": 100, "maximum": 100, "minimum": 1, "dim": 20}


@dataclass
class HaEntity:
    entity_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    area: str | None = None
    labelled: bool = False
    state: str = "unknown"
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))


def _key(text: str) -> tuple[str, ...]:
    """A name's distinguishing words: lower case, no filler, plurals folded ("lights" -> "light")."""
    words = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in _words(text)]
    return tuple(w for w in words if w not in FILLER and w.rstrip("s") not in FILLER)


def _plural(target: str) -> bool:
    words = _words(target)
    return bool(words) and (words[-1] in ("lights", "lamps", "switches") or "all" in words)


# Intent and REST calls; replaced in tests -----------------------------------------------------

IntentCaller = Callable[[str, dict[str, Any]], Awaitable[str]]


class HomeAssistant:
    """One Home Assistant: its registries (cached), its MCP intents, and its REST API."""

    def __init__(
        self,
        server: McpServerConfig,
        label: str = "voice",
        intent_caller: IntentCaller | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = urlsplit(str(server.url))
        self.base = f"{url.scheme}://{url.netloc}"
        self.mcp_url = str(server.url)
        self.token = server.token or ""
        self.label = label
        self._intent_caller = intent_caller
        self._transport = transport
        self._mcp: Any = None
        self._handlers: dict[str, Any] | None = None
        self._entities: list[HaEntity] = []
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # Registries ---------------------------------------------------------------------------

    async def entities(self, fresh: bool = False) -> list[HaEntity]:
        async with self._lock:
            if fresh or time.monotonic() - self._loaded_at > REGISTRY_TTL_S or not self._entities:
                self._entities = await self._load()
                self._loaded_at = time.monotonic()
                logger.info(
                    "Home Assistant entities loaded",
                    extra={"event": "ha_entities_loaded", "count": sum(1 for e in self._entities if e.labelled)},
                )
            return self._entities

    async def _load(self) -> list[HaEntity]:
        """Labels, entities, areas and states from Home Assistant's websocket API (registries aren't in REST)."""
        import websockets

        scheme = "wss" if self.base.startswith("https") else "ws"
        async with websockets.connect(f"{scheme}://{self.base.split('://', 1)[1]}/api/websocket", max_size=None) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                raise PermissionError("Home Assistant rejected the token")
            replies: dict[str, Any] = {}
            requests = ["config/label_registry/list", "config/entity_registry/list", "config/area_registry/list", "get_states"]
            for number, kind in enumerate(requests, start=1):
                await ws.send(json.dumps({"id": number, "type": kind}))
            while len(replies) < len(requests):
                message = json.loads(await ws.recv())
                if message.get("id"):
                    replies[requests[message["id"] - 1]] = message.get("result") or []
        return entities_from_registries(
            self.label,
            replies["config/label_registry/list"],
            replies["config/entity_registry/list"],
            replies["config/area_registry/list"],
            replies["get_states"],
        )

    # Calls ----------------------------------------------------------------------------------

    async def intent(self, name: str, arguments: dict[str, Any]) -> str:
        if self._intent_caller is not None:
            return await self._intent_caller(name, arguments)
        handlers = await self._intent_handlers()
        found = handlers.get(name)
        if found is None:
            raise LookupError(f"Home Assistant offers no {name} tool")
        full_name, handler = found
        result: list[str] = []

        class Params:  # the shape Pipecat's MCP tool handlers read
            function_name = full_name
            tool_call_id = f"victoria-{secrets.token_hex(4)}"

            def __init__(self) -> None:
                self.arguments = arguments

            async def result_callback(self, value: Any, *args: Any, **kwargs: Any) -> None:
                result.append(value if isinstance(value, str) else json.dumps(value))

        await handler(Params())
        return "".join(result)

    async def _intent_handlers(self) -> dict[str, Any]:
        if self._handlers is None:
            from mcp.client.session_group import StreamableHttpParameters
            from pipecat.services.mcp_service import MCPClient

            self._mcp = MCPClient(StreamableHttpParameters(url=self.mcp_url, headers=self.headers, timeout=10))
            schema = await self._mcp.tools()
            # Home Assistant prefixes each tool with its integration ("intent__HassTurnOn", "light__HassLightSet").
            self._handlers = {
                tool.name.rsplit("__", 1)[-1]: (tool.name, tool.handler)
                for tool in schema.standard_tools
                if tool.name.rsplit("__", 1)[-1] in INTENTS
            }
            logger.info("Home Assistant MCP connected", extra={"event": "ha_mcp_connected", "count": len(self._handlers)})
        return self._handlers

    async def state(self, entity_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=self._transport, timeout=10) as client:
            response = await client.get(f"{self.base}/api/states/{entity_id}", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        async with httpx.AsyncClient(transport=self._transport, timeout=10) as client:
            response = await client.post(f"{self.base}/api/services/{domain}/{service}", headers=self.headers, json=data)
            response.raise_for_status()

    async def close(self) -> None:
        if self._mcp is not None:
            await self._mcp.close()
            self._mcp = None
            self._handlers = None


def entities_from_registries(
    label: str, labels: list[dict], registry: list[dict], areas: list[dict], states: list[dict]
) -> list[HaEntity]:
    label_ids = {entry["label_id"] for entry in labels if entry.get("name", "").lower() == label.lower() or entry["label_id"] == label}
    area_names = {entry["area_id"]: entry["name"] for entry in areas}
    by_id = {entry["entity_id"]: entry for entry in registry}
    found = []
    for state in states:
        entity_id = state["entity_id"]
        entry = by_id.get(entity_id, {})
        if entry.get("disabled_by"):
            continue
        attributes = state.get("attributes") or {}
        found.append(
            HaEntity(
                entity_id=entity_id,
                name=entry.get("name") or attributes.get("friendly_name") or entity_id,
                aliases=list(entry.get("aliases") or []),
                area=area_names.get(entry.get("area_id")),
                labelled=bool(label_ids & set(entry.get("labels") or [])),
                state=state.get("state", "unknown"),
                attributes=attributes,
            )
        )
    return found


def resolve(target: str, entities: list[HaEntity], domains: list[str] | None = None) -> tuple[list[HaEntity], list[HaEntity]]:
    """(labelled matches, unlabelled matches) for a spoken target: by name or alias, then by area.

    A name whose distinguishing words all appear in the target matches ("bedroom light" for
    "turn on the bedroom lights"); the most specific match wins.
    """
    wanted = set(_key(target))
    pool = [e for e in entities if not domains or e.domain in domains]
    if not wanted:
        return [], []

    def score(entity: HaEntity, partial: bool) -> int:
        best = 0
        for name in [entity.name, *entity.aliases]:
            key = set(_key(name))
            if not key:
                continue
            if not partial and key <= wanted:  # every word of the name was said: "bedroom light" in "the bedroom lights"
                best = max(best, len(key) * 10 + (5 if key == wanted else 0))
            elif partial and wanted <= key:  # the target is part of the name: "front door" for "Front Door Lock"
                best = max(best, 10 - len(key))  # the shortest such name first
        return best

    def best(partial: bool) -> list[HaEntity]:
        scored = [(score(e, partial), e) for e in pool]
        top = max((s for s, _ in scored), default=0)
        return [e for s, e in scored if s == top] if top else []

    # Whole names first, then an area ("the kitchen": everything there that switches), then part of a name.
    matches = best(partial=False) or [
        e for e in pool
        if e.area and set(_key(e.area)) <= wanted and e.domain in ("light", "switch", "fan", "cover")
    ] or best(partial=True)
    return [e for e in matches if e.labelled], [e for e in matches if not e.labelled]


def _parse_level(value: str) -> int | None:
    text = value.lower().strip()
    if text in NUMBER_WORDS:
        return NUMBER_WORDS[text]
    match = re.search(r"\d+(?:\.\d+)?", text)
    return round(float(match.group())) if match else None


def _names(entities: list[HaEntity]) -> str:
    names = [e.name for e in entities]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _cap(text: str) -> str:
    """Capitalize the first letter only ("the Upper Porch Light" -> "The Upper Porch Light")."""
    return text[:1].upper() + text[1:]


def _the(name: str) -> str:
    return name if name[:1].isupper() and " " not in name else f"the {name}"


def _failed(reply: str) -> bool:
    """Home Assistant's intent result reports failures in its text."""
    text = reply.lower()
    if text.startswith("error"):
        return True
    try:
        data = json.loads(reply)
    except ValueError:
        return False
    if isinstance(data, dict):
        if data.get("response_type") == "error" or data.get("isError"):
            return True
        failed = (data.get("data") or {}).get("failed")
        return bool(failed)
    return False


def describe_state(entity: HaEntity, state: dict[str, Any]) -> str:
    value = state.get("state", "unknown")
    attributes = state.get("attributes") or {}
    name = _the(entity.name)
    if value == "unavailable":
        return f"{_cap(name)} isn't responding."
    if entity.domain == "climate":
        current, setpoint = attributes.get("current_temperature"), attributes.get("temperature")
        parts = [f"{_cap(name)} is {value}"]
        if setpoint is not None:
            parts.append(f"set to {setpoint:g}")
        sentence = ", ".join(parts) + "."
        return sentence + (f" It's {current:g} degrees now." if current is not None else "")
    if entity.domain == "light" and value == "on" and attributes.get("brightness") is not None:
        return f"{_cap(name)} is on at {round(attributes['brightness'] / 255 * 100)} percent."
    return f"{_cap(name)} is {value}."


class HomeControl:
    """The `home_control` tool: action turn_on, turn_off, set or query; a target; a value for set."""

    def __init__(self, home: HomeAssistant, domains: list[str] | None = None) -> None:
        self.home = home
        self.domains = domains or None

    async def handle(self, arguments: dict[str, Any]) -> ToolOutcome:
        action = str(arguments.get("action") or "").lower().replace(" ", "_")
        target = str(arguments.get("target") or "").strip()
        value = str(arguments.get("value") or "").strip()
        if not target:
            return ToolOutcome(result={"error": "no target"}, speak="Which one?")
        try:
            entities = await self.home.entities()
        except Exception as exc:  # noqa: BLE001 - Home Assistant down or the token rejected
            logger.warning("Home Assistant unreachable", extra={"event": "ha_unreachable", "reason": str(exc)})
            return ToolOutcome(result={"error": "Home Assistant unreachable"}, speak="I can't reach Home Assistant right now.", error=True)
        labelled, unlabelled = resolve(target, entities, self.domains)
        if not labelled:
            if unlabelled:
                logger.info("Home control refused: not labelled", extra={"event": "ha_not_labelled", "tool": target})
                return ToolOutcome(
                    result={"error": "not labelled for voice", "entities": [e.entity_id for e in unlabelled]},
                    speak=f"{_cap(_the(unlabelled[0].name))} isn't set up for voice control.",
                )
            return ToolOutcome(result={"error": "no match"}, speak=f"I couldn't find {target}.")
        if len(labelled) > 1 and not _plural(target) and action != "query":
            question = f"Which one: {_names(labelled[:4])}?"
            return ToolOutcome(
                result={"choices": [e.entity_id for e in labelled]},
                speak=question,
                pending=PendingQuestion(kind="clarify", question=question, options=[e.name for e in labelled[:4]]),
            )
        if action == "query":
            return await self._query(labelled[:3])
        if action in ("turn_on", "on", "activate", "start"):
            return await self._switch(labelled, on=True)
        if action in ("turn_off", "off", "stop"):
            return await self._switch(labelled, on=False)
        if action in ("set", "dim", "brighten"):
            return await self._set(labelled, value)
        return ToolOutcome(result={"error": f"unknown action {action!r}"}, speak="I can turn things on or off, set them, or check them.")

    async def _query(self, targets: list[HaEntity]) -> ToolOutcome:
        sentences, states = [], {}
        for entity in targets:
            state = await self.home.state(entity.entity_id)
            states[entity.entity_id] = state.get("state")
            sentences.append(describe_state(entity, state))
        return ToolOutcome(result={"states": states}, speak=" ".join(sentences))

    async def _switch(self, targets: list[HaEntity], on: bool) -> ToolOutcome:
        offline = [e for e in targets if e.state == "unavailable"]
        live = [e for e in targets if e.state != "unavailable"]
        if not live:
            return ToolOutcome(result={"error": "unavailable"}, speak=f"{_cap(_the(_names(offline)))} isn't responding.")
        intent = "HassTurnOn" if on else "HassTurnOff"
        done, failed = [], []
        for entity in live:
            reply = await self._intent(intent, {"name": entity.name, "domain": [entity.domain]})
            (failed if reply is None or _failed(reply) else done).append(entity)
        logger.info(
            "Home control",
            extra={"event": "ha_control", "action": intent, "count": len(done), "entities": [e.entity_id for e in done]},
        )
        if not done:
            return ToolOutcome(result={"error": "Home Assistant refused"}, speak=f"Home Assistant couldn't do that for {_the(_names(failed))}.", error=True)
        if all(e.domain == "scene" for e in done):
            speak = f"Done: {_names(done)}."
        else:
            speak = f"Turned {'on' if on else 'off'} {_the(_names(done))}."
        if offline or failed:
            speak += f" {_cap(_the(_names(offline + failed)))} didn't respond."
        return ToolOutcome(result={"done": [e.entity_id for e in done], "failed": [e.entity_id for e in offline + failed]}, speak=speak)

    async def _set(self, targets: list[HaEntity], value: str) -> ToolOutcome:
        level = _parse_level(value)
        if level is None:
            return ToolOutcome(result={"error": "no value"}, speak="To what level?")
        entity = targets[0]
        if entity.domain == "climate":
            reply = await self._intent("HassClimateSetTemperature", {"name": entity.name, "temperature": level})
            speak = f"Set {_the(entity.name)} to {level} degrees."
        elif entity.domain == "light":
            level = max(0, min(100, level))
            replies = [await self._intent("HassLightSet", {"name": e.name, "brightness": level}) for e in targets]
            reply = replies[0] if all(r is not None and not _failed(r) for r in replies) else None
            speak = f"Set {_the(_names(targets))} to {level} percent."
        else:
            return ToolOutcome(result={"error": "can't set"}, speak=f"I can only turn {_the(entity.name)} on or off.")
        if reply is None or _failed(reply):
            return ToolOutcome(result={"error": "Home Assistant refused"}, speak="Home Assistant couldn't do that.", error=True)
        logger.info("Home control", extra={"event": "ha_control", "action": "set", "entities": [e.entity_id for e in targets]})
        return ToolOutcome(result={"set": [e.entity_id for e in targets], "value": level}, speak=speak)

    async def _intent(self, name: str, arguments: dict[str, Any]) -> str | None:
        try:
            return await self.home.intent(name, arguments)
        except LookupError:
            return None
        except Exception as exc:  # noqa: BLE001 - reported to the user, never raised into the session
            logger.warning("Home Assistant intent failed", extra={"event": "ha_intent_failed", "tool": name, "reason": str(exc)})
            return None


class Confirmations:
    """One-time codes for confirmed actions: only the yes to our own question can run them."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._codes: dict[str, tuple[str, float]] = {}
        self._clock = clock

    def issue(self, tool: str) -> str:
        code = secrets.token_hex(8)
        self._codes[code] = (tool, self._clock() + CONFIRM_TTL_S)
        return code

    def redeem(self, tool: str, code: object) -> bool:
        entry = self._codes.pop(str(code), None) if code else None
        return entry is not None and entry[0] == tool and entry[1] > self._clock()


class ServiceTool:
    """An `ha_service` tool: one Home Assistant service on a labelled entity, spoken confirmation if `confirm`."""

    def __init__(self, tool: ToolConfig, home: HomeAssistant, confirmations: Confirmations) -> None:
        self.tool = tool
        self.home = home
        self.confirmations = confirmations
        domain, _, service = (tool.service or "").partition(".")
        if not domain or not service:
            raise ValueError("service must look like domain.service (for example lock.lock)")
        self.domain, self.service = domain, service
        scope = tool.entities
        self.domains = scope.domains if scope and scope.domains else [domain]

    async def handle(self, arguments: dict[str, Any]) -> ToolOutcome:
        target = str(arguments.get("target") or arguments.get("name") or "").strip()
        try:
            entities = await self.home.entities()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Home Assistant unreachable", extra={"event": "ha_unreachable", "reason": str(exc)})
            return ToolOutcome(result={"error": "Home Assistant unreachable"}, speak="I can't reach Home Assistant right now.", error=True)
        candidates = [e for e in entities if e.domain in self.domains]
        labelled, unlabelled = resolve(target, candidates) if target else ([e for e in candidates if e.labelled], [])
        if not labelled:
            if unlabelled:
                return ToolOutcome(
                    result={"error": "not labelled for voice"},
                    speak=f"{_cap(_the(unlabelled[0].name))} isn't set up for voice control.",
                )
            return ToolOutcome(result={"error": "no match"}, speak=f"I couldn't find {target or 'one'}.")
        if len(labelled) > 1:
            question = f"Which one: {_names(labelled[:4])}?"
            return ToolOutcome(
                result={"choices": [e.entity_id for e in labelled]},
                speak=question,
                pending=PendingQuestion(kind="clarify", question=question, options=[e.name for e in labelled[:4]]),
            )
        entity = labelled[0]
        if self.tool.confirm and not self.confirmations.redeem(self.tool.name, arguments.get("_confirm")):
            # Architecture §2: confirm before risky actions. The code goes only into our own pending yes.
            question = f"{_cap(self.service)} {_the(entity.name)}?"
            code = self.confirmations.issue(self.tool.name)
            return ToolOutcome(
                result={"asked": question},
                speak=question,
                pending=PendingQuestion(
                    kind="yes_no", question=question, on_yes=(self.tool.name, {"target": entity.name, "_confirm": code})
                ),
            )
        if entity.state == "unavailable":
            return ToolOutcome(result={"error": "unavailable"}, speak=f"{_cap(_the(entity.name))} isn't responding.")
        try:
            await self.home.call_service(self.domain, self.service, {"entity_id": entity.entity_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Home Assistant service failed", extra={"event": "ha_service_failed", "tool": self.tool.name, "reason": str(exc)})
            return ToolOutcome(result={"error": str(exc)}, speak="Home Assistant couldn't do that.", error=True)
        logger.info("Home Assistant service", extra={"event": "ha_service", "tool": self.tool.name, "entities": [entity.entity_id]})
        template = self.tool.reply.template if self.tool.reply and isinstance(self.tool.reply.template, str) else "Done."
        return ToolOutcome(result={"done": entity.entity_id}, speak=template.format(name=_cap(_the(entity.name))))


class HomeAssistantHub:
    """One HomeAssistant per configured server, shared by every session (cache and MCP connection)."""

    def __init__(self) -> None:
        self.instances: dict[tuple[str, str, str], HomeAssistant] = {}
        self.confirmations = Confirmations()

    def get(self, server: McpServerConfig, label: str) -> HomeAssistant:
        key = (str(server.url), server.token or "", label)
        if key not in self.instances:
            self.instances[key] = HomeAssistant(server, label)
        return self.instances[key]

    async def close(self) -> None:
        for instance in self.instances.values():
            await instance.close()
