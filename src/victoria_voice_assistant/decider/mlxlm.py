"""Install and run the mlx-lm server that hosts the decision model (services/mlx-lm/README.md)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from victoria_voice_assistant.config.schema import ModelsConfig

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_DIR = REPO_ROOT / "services" / "mlx-lm"


def install() -> Path:
    """Build the pinned mlx-lm environment."""
    subprocess.run(["uv", "sync", "--locked"], cwd=RUNTIME_DIR, check=True)
    return RUNTIME_DIR


def weights_dir(models: ModelsConfig) -> Path:
    """The folder of MLX weights the decider serves: the `model` of the decider's models.yaml entry."""
    decider = models.decider
    if decider.type != "decision_model" or decider.model is None:
        raise ValueError("decider.type is not decision_model in models.yaml")
    return Path(models.models[decider.model].model).expanduser()


def run_command(models: ModelsConfig) -> list[str]:
    """The mlx_lm.server command for the decider configured in models.yaml."""
    weights = weights_dir(models)
    assert models.decider.server is not None
    url = urlparse(models.servers[models.decider.server].base_url or "")
    if url.hostname not in ("127.0.0.1", "localhost") or url.port is None:
        raise ValueError(f"the decider server must be http://127.0.0.1:<port>/v1, not {url.geturl()!r}")
    if not (weights / "config.json").is_file():
        raise FileNotFoundError(f"no MLX model in {weights}; check the decider entry in models.yaml")
    server = RUNTIME_DIR / ".venv" / "bin" / "mlx_lm.server"
    if not server.is_file():
        raise FileNotFoundError(f"{server} not found; run `assistant decider install`")
    return [str(server), "--model", str(weights), "--host", "127.0.0.1", "--port", str(url.port)]


def run(models: ModelsConfig) -> None:
    command = run_command(models)
    logger.info("Starting decider", extra={"event": "decider_start", "model": command[2]})
    os.execv(command[0], command)
