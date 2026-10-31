"""Install and run the local SearXNG instance (services/searxng/README.md)."""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SEARXNG_REPO = "https://github.com/searxng/searxng.git"
SEARXNG_COMMIT = "ffa88e3"
REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_DIR = REPO_ROOT / "services" / "searxng"


def source_dir() -> Path:
    return Path(os.environ.get("ASSISTANT_SEARXNG_DIR", Path.home() / "Services" / "searxng")).expanduser()


def install() -> Path:
    """Check out the pinned SearXNG commit and build its uv environment."""
    source = source_dir()
    if not (source / ".git").is_dir():
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "50", SEARXNG_REPO, str(source)], check=True)
    current = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--short=7", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if current != SEARXNG_COMMIT:
        subprocess.run(["git", "-C", str(source), "fetch", "--depth", "50", "origin"], check=True)
        subprocess.run(["git", "-C", str(source), "checkout", "--detach", SEARXNG_COMMIT], check=True)
    subprocess.run(["uv", "sync", "--locked"], cwd=RUNTIME_DIR, check=True)
    return source


def run_command(config_dir: Path) -> tuple[list[str], dict[str, str], Path]:
    """The granian command, environment and working directory that run SearXNG."""
    source = source_dir()
    settings = config_dir / "searxng" / "settings.yml"
    if not (source / "searx").is_dir():
        raise FileNotFoundError(f"SearXNG source not found in {source}; run `assistant searxng install`")
    if not settings.is_file():
        raise FileNotFoundError(f"{settings} not found; copy config/examples/searxng/settings.yml there")
    granian = RUNTIME_DIR / ".venv" / "bin" / "granian"
    if not granian.is_file():
        raise FileNotFoundError(f"{granian} not found; run `assistant searxng install`")
    env = {
        **os.environ,
        "SEARXNG_SETTINGS_PATH": str(settings),
        # Only used to sign SearXNG's own cookies and image-proxy links, neither of
        # which the assistant uses, so a fresh value per start is fine.
        "SEARXNG_SECRET": os.environ.get("SEARXNG_SECRET") or secrets.token_hex(32),
        "PYTHONPATH": str(source),
    }
    command = [
        str(granian),
        "--interface", "wsgi",
        "--host", "127.0.0.1",
        "--port", "8888",
        "--blocking-threads", "4",
        "searx.webapp:app",
    ]
    return command, env, source


def run(config_dir: Path) -> None:
    command, env, cwd = run_command(config_dir)
    logger.info("Starting SearXNG", extra={"event": "searxng_start", "config_dir": str(config_dir)})
    os.chdir(cwd)
    os.execve(command[0], command, env)
