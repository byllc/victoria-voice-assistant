"""`show_page`: open a web page in the document panel of the browser that asked (feature web-document-panel).

The page goes only to the screen the request came from (the browser's `client_id`, the same rule as "play it
here"). A satellite, or a page that has since closed, has no screen: the tool says so instead.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable
from urllib.parse import urlparse

from victoria_voice_assistant.tools.outcome import ToolOutcome

logger = logging.getLogger(__name__)

NO_SCREEN = "I can only show pages on the web interface."


def page_address(value: str) -> str | None:
    """A web address from what the model (or the address bar) gave: `bbc.co.uk/weather` becomes https://…
    Anything that isn't http or https, or has no host, is refused (None)."""
    value = (value or "").strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or "." not in parsed.hostname:
        return None
    return value


async def show_page(arguments: dict[str, Any], client_id: str | None, sessions: Iterable[Any]) -> ToolOutcome:
    url = page_address(str(arguments.get("url") or ""))
    if url is None:
        return ToolOutcome(result={"error": "not a web address", "url": arguments.get("url")}, error=True)
    screens = [s for s in sessions if client_id and getattr(s.state, "client_id", None) == client_id]
    if not screens:
        logger.info("No screen for a page", extra={"event": "page_no_screen", "text": url})
        return ToolOutcome(result={"error": "no screen here", "url": url}, speak=NO_SCREEN, error=True)
    message = {"type": "show_page", "url": url, "title": str(arguments.get("title") or "") or None}
    # A browser with voice on has two sessions (chat and voice); the panel ignores the same page twice.
    for screen in screens:
        await screen.send(message)
    logger.info("Page sent to the screen", extra={"event": "page_shown", "text": url, "count": len(screens)})
    return ToolOutcome(result={"shown": url, "note": "The page is open on the right of the screen."})
