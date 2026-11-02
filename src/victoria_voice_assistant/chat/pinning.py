from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from victoria_voice_assistant.config.schema import ModelsConfig

logger = logging.getLogger(__name__)

DEFAULT_LMS = Path.home() / ".lmstudio" / "bin" / "lms"
LOAD_TIMEOUT_S = 900


def lms_executable() -> str:
    return shutil.which("lms") or str(DEFAULT_LMS)


def _loaded(executable: str) -> dict[str, int]:
    """Models LM Studio already has loaded, by model key and identifier, with the largest context loaded."""
    try:
        result = subprocess.run(
            [executable, "ps", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        instances = json.loads(result.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    loaded: dict[str, int] = {}
    for item in instances:
        context = int(item.get("contextLength") or 0)
        for name in {item.get("modelKey"), item.get("identifier")} - {None}:
            loaded[name] = max(loaded.get(name, 0), context)
    return loaded


def pin_models(models: ModelsConfig, lms: str | None = None) -> list[str]:
    """Make sure every ``pin: true`` model is loaded in LM Studio: one copy per model, never a second.

    A model that is already loaded (pinned or not, under any identifier) is used as it is. One that
    isn't is loaded once, without an idle TTL, under its own name, with the largest `context_length`
    any entry naming it asks for. Failures are logged and skipped; the assistant still starts and the
    router falls back to JIT loading or the fallback model.
    """
    executable = lms or lms_executable()
    contexts: dict[str, int] = {}
    for config in models.models.values():
        if config.pin:
            contexts[config.model] = max(contexts.get(config.model, 0), config.context_length or 0)
    loaded = _loaded(executable)
    pinned: list[str] = []
    for model in sorted(contexts):
        wanted = contexts[model]
        if model in loaded:
            pinned.append(model)
            logger.info("Model already loaded", extra={"event": "model_already_pinned", "model": model})
            if wanted and loaded[model] and loaded[model] < wanted:
                logger.warning(
                    "Model loaded with less context than configured; reload it in LM Studio to change that",
                    extra={"event": "model_context_short", "model": model, "count": loaded[model]},
                )
            continue
        command = [executable, "load", model, "-y", "--identifier", model]
        if wanted:
            command += ["--context-length", str(wanted)]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=LOAD_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error(
                "Model pin failed",
                extra={"event": "model_pin_failed", "model": model, "error": str(exc)},
            )
            continue
        pinned.append(model)
        logger.info("Model pinned", extra={"event": "model_pinned", "model": model})
    return pinned
