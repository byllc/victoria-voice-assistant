from __future__ import annotations

import json
import sys
import os
import logging
from datetime import datetime, timezone
from typing import Any


LOGGED_FIELDS = (
    "config_revision",
    "config_dir",
    "database_path",
    "model",
    "llm_call",
    "ttft_ms",
    "total_ms",
    "tool",
    "kind",
    "reply",
    "spoken_directly",
    "second_llm_call",
    "duration_ms",
    "arguments",
    "reason",
    "status",
    "handled_by",
    "agent",
    "timer",
    "timers",
    "count",
    "duration_s",
    "delta_s",
    "cert",
    "names",
    "expires",
    "days_left",
    "session",
    "text",
    "parts",
    "labels",
    "ttfb",
    "tools",
    "measured_from",
    "rss_mb",
    "cached",
    "choice",
    "confidence",
    "task",
    "cost_usd",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        item: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event is not None:
            item["event"] = event
        error = getattr(record, "error", None)
        if error is not None:
            item["error"] = error
        for key in LOGGED_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                item[key] = value
        if record.exc_info:
            item["exception"] = self.formatException(record.exc_info)
        return json.dumps(item, ensure_ascii=True)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs every request at INFO; the health checks alone are four every 30 s. Warnings still show.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _quiet_pipecat()


def _quiet_pipecat() -> None:
    """Pipecat logs through loguru at DEBUG by default: every frame link, every model call's full prompt
    (about 18 MB a day under launchd). Keep INFO and up; ASSISTANT_PIPECAT_LOG_LEVEL=DEBUG brings it back."""
    try:
        from loguru import logger as pipecat_logger
    except ImportError:
        return
    pipecat_logger.remove()
    pipecat_logger.add(sys.stderr, level=os.environ.get("ASSISTANT_PIPECAT_LOG_LEVEL", "INFO").upper())
