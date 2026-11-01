"""`relay` agents (architecture §9, relaying to Alexa): hand the words to another voice assistant.

"Ask Alexa to play some jazz" sends "play some jazz" to an Echo through Home Assistant's
Alexa Media Player integration (`media_player.play_media` with `media_content_type: custom`, as
if spoken to it). The Echo answers or plays on its own; nothing comes back, so the reply is just
"Asking Alexa." The Echo is the one whose room the request names, the device's own room, or
the only one configured.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from victoria_voice_assistant.config.schema import AgentConfig, AppConfig
from victoria_voice_assistant.tools.outcome import PendingQuestion, ToolOutcome

if TYPE_CHECKING:
    from victoria_voice_assistant.tools.registry import Services

logger = logging.getLogger(__name__)

SETUP = (
    "Install the Alexa Media Player integration in Home Assistant (through HACS; it needs an Amazon login), "
    "then list each Echo under `targets` in this agent's entry in agents.yaml, for example "
    "`targets: {{kitchen: media_player.kitchen_echo}}`. The Home Assistant connection is mcp_servers.homeassistant "
    "in tools.yaml (docs/music.md)."
)


def _label(name: str) -> str:
    return name[:1].upper() + name[1:]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _strip_room(request: str, room: str) -> str:
    """'play jazz in the kitchen' -> 'play jazz' once the kitchen Echo is chosen."""
    return re.sub(rf"\s+(?:in|on)\s+(?:the\s+)?{re.escape(room)}\b", "", request, flags=re.IGNORECASE).strip() or request


async def relay(agent: AgentConfig, request: str, device: str, config: AppConfig, services: Services) -> ToolOutcome:
    name = _label(agent.name)
    not_set_up = ToolOutcome(
        result={"error": "relay not set up"},
        speak=f"{name} isn't set up yet.",
        chat=SETUP.replace("{{", "{").replace("}}", "}"),
        chat_title=f"{agent.name}: setup",
    )
    if not agent.targets:
        return not_set_up
    server = config.tools.mcp_servers.get(agent.via if agent.via and agent.via != "ha_service" else "homeassistant")
    if server is None or not server.token:
        return not_set_up
    # Which Echo: a room named in the request, the device's own room, or the only one.
    named = [room for room in agent.targets if _words(room) and _words(room) <= _words(request)]
    rooms = {r: room for r, room in ((config.tools.music.rooms if config.tools.music else {}).items())}
    own = next((r for r, room in rooms.items() if room.satellite == device and r in agent.targets), None)
    room = named[0] if named else own or (next(iter(agent.targets)) if len(agent.targets) == 1 else None)
    if room is None:
        question = f"Which {name}: {', '.join(list(agent.targets)[:-1])} or {list(agent.targets)[-1]}?"
        return ToolOutcome(
            result={"choices": list(agent.targets)},
            speak=question,
            pending=PendingQuestion(kind="clarify", question=question, options=list(agent.targets)),
        )
    entity_id = agent.targets[room]
    if services.home is None:
        from victoria_voice_assistant.tools.home import HomeAssistantHub

        services.home = HomeAssistantHub()
    home = services.home.get(server, "voice")
    try:
        known = {e.entity_id for e in await home.entities()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Home Assistant unreachable", extra={"event": "ha_unreachable", "reason": str(exc)})
        return ToolOutcome(result={"error": "Home Assistant unreachable"}, speak="I can't reach Home Assistant right now.", error=True)
    if entity_id not in known:
        logger.info("Relay target missing", extra={"event": "relay_unavailable", "agent": agent.name, "tool": entity_id})
        return not_set_up
    domain, _, service = (agent.service or "media_player.play_media").partition(".")
    text = _strip_room(request, room) if named else request
    try:
        await home.call_service(
            domain,
            service,
            {"entity_id": entity_id, "media_content_type": agent.content_type or "custom", "media_content_id": text},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Relay failed", extra={"event": "relay_failed", "agent": agent.name, "reason": str(exc)})
        return ToolOutcome(result={"error": str(exc)}, speak=f"I couldn't reach {name} right now.", error=True)
    logger.info("Relayed", extra={"event": "relay_sent", "agent": agent.name, "tool": entity_id})
    return ToolOutcome(result={"relayed": text, "to": entity_id}, speak=agent.reply or f"Asking {name}.")
