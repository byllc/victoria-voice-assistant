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


def _already_pinned(executable: str) -> set[str]:
    """Model keys already loaded with no idle TTL. Loading again would create a duplicate instance."""
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
        return set()
    return {item["modelKey"] for item in instances if item.get("ttlMs") is None and "modelKey" in item}


def pin_models(models: ModelsConfig, lms: str | None = None) -> list[str]:
    """Load every ``pin: true`` model into LM Studio without an idle TTL.

    Failures are logged and skipped; the assistant still starts and the router
    falls back to JIT loading or the fallback model.
    """
    executable = lms or lms_executable()
    targets = sorted({config.model for config in models.models.values() if config.pin})
    already = _already_pinned(executable)
    pinned: list[str] = []
    for model in targets:
        if model in already:
            pinned.append(model)
            logger.info("Model already pinned", extra={"event": "model_already_pinned", "model": model})
            continue
        try:
            subprocess.run(
                [executable, "load", model, "-y"],
                check=True,
                capture_output=True,
                text=True,
                timeout=LOAD_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error(
                "Model pin failed",
                extra={"event": "model_pin_failed", "model": model, "error": str(exc)},
            )
            continue
        pinned.append(model)
        logger.info("Model pinned", extra={"event": "model_pinned", "model": model})
    return pinned
