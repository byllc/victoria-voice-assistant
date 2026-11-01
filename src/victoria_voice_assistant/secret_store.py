"""Secrets (Bill, 2026-11-01): kept in the macOS Keychain, run from `config/.env`.

- On a Mac, `assistant secrets set NAME` stores the secret in the login Keychain (service
  `victoria-voice-assistant`, the secret's name as the account) and writes it to `config/.env`.
- Installing a service (`assistant launchd install...`) or `assistant secrets sync` rewrites
  `config/.env` from the Keychain, so the services can start after a reboot, before anyone logs in
  and unlocks the Keychain. The running services read only the process environment and `config/.env`.
- `config/.env` is never committed: it's git-ignored, a pre-commit hook rejects it, and it is only
  written after `git check-ignore` confirms it's ignored. It's kept at mode 600.
- Without a Keychain (another OS, or `ASSISTANT_KEYCHAIN=0`), `set` writes `config/.env` only.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

SERVICE = "victoria-voice-assistant"
ENV_FILE = ".env"

# Secrets the assistant knows about: what `list` shows and `sync` copies from the Keychain.
KNOWN = {
    "LMS_API_KEY": "LM Studio's API key (LM Studio > Developer)",
    "ASSISTANT_WEB_PASSWORD": "household web login (optional; open without it)",
    "HERMES_API_KEY": "the Hermes gateway's key (docs/hermes.md)",
    "ANTHROPIC_API_KEY": "Claude, when turned on (docs/claude-setup.md)",
    "HA_TOKEN": "Home Assistant long-lived access token",
    "VAPID_PRIVATE_KEY": "Web Push signing key (`assistant push keys`)",
    "MUSIC_ASSISTANT_TOKEN": "Music Assistant token",
}


class SecretsError(Exception):
    pass


def default_env_file() -> Path:
    from victoria_voice_assistant.config.loader import default_config_dir

    return default_config_dir() / ENV_FILE


def read_env_file(path: Path) -> dict[str, str]:
    """NAME=value pairs from a .env file, read into memory only (never exported to the environment)."""
    if not path.is_file():
        return {}
    return {name: value for name, value in dotenv_values(path).items() if value}


def get_secret(name: str, env_file: Path | None = None) -> str | None:
    """A secret's value: the process environment first, then `config/.env`. Never logged."""
    return os.environ.get(name) or read_env_file(env_file or default_env_file()).get(name) or None


# The Keychain -----------------------------------------------------------------


def keychain_available() -> bool:
    return sys.platform == "darwin" and os.environ.get("ASSISTANT_KEYCHAIN", "1") != "0"


def keychain_get(name: str) -> str | None:
    if not keychain_available():
        return None
    import keyring
    from keyring.errors import KeyringError

    try:
        return keyring.get_password(SERVICE, name) or None
    except KeyringError as exc:
        logger.warning("Keychain unavailable", extra={"event": "keychain_unavailable", "reason": type(exc).__name__})
        return None


# The .env file ------------------------------------------------------------------


def _ensure_ignored(path: Path) -> None:
    """Refuse to write a secrets file that git would track."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)], cwd=path.parent, capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return  # no git here: nothing to commit it to
    if result.returncode == 1:
        raise SecretsError(f"{path} is not git-ignored; refusing to write secrets there")
    # 0: ignored. 128: not inside a git repository (an installed config directory).


def write_env_file(path: Path, updates: dict[str, str], remove: set[str] = frozenset()) -> None:
    """Set or remove NAME=value lines, keeping the rest of the file; mode 600, written atomically."""
    _ensure_ignored(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else ["# Secrets: written by `assistant secrets`. Never commit this file (docs/secrets.md)."]
    pending = dict(updates)
    kept = []
    for line in lines:
        name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if name in remove:
            continue
        if name in pending:
            kept.append(f"{name}={pending.pop(name)}")
        else:
            kept.append(line)
    kept.extend(f"{name}={value}" for name, value in pending.items())
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(kept) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


# Operations used by the CLI -------------------------------------------------------


def set_secret(name: str, value: str, env_file: Path | None = None) -> str:
    """Store a secret: in the Keychain when there is one, and in `config/.env`. Returns where it went."""
    if not value:
        raise SecretsError("empty value")
    path = env_file or default_env_file()
    where = "config/.env"
    if keychain_available():
        import keyring

        keyring.set_password(SERVICE, name, value)
        where = "the Keychain and config/.env"
    write_env_file(path, {name: value})
    logger.info("Secret stored", extra={"event": "secret_stored"})
    return where


def delete_secret(name: str, env_file: Path | None = None) -> None:
    if keychain_available():
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(SERVICE, name)
        except PasswordDeleteError:
            pass
    path = env_file or default_env_file()
    if path.is_file():
        write_env_file(path, {}, remove={name})


def sync_env_file(env_file: Path | None = None) -> list[str]:
    """Write `config/.env` from the Keychain (the known secrets that are set there). Returns their names."""
    if not keychain_available():
        return []
    found = {name: value for name in KNOWN if (value := keychain_get(name))}
    if found:
        write_env_file(env_file or default_env_file(), found)
    return sorted(found)
