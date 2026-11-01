"""Secrets live in the macOS login Keychain and are read into memory when needed (Bill, 2026-11-01).

Nothing secret is kept in the project or in `config/`. The YAML names a secret (`${HA_TOKEN}`) and
this module supplies its value: from the process environment if set there (tests, one-off runs),
otherwise from the Keychain item with service `victoria-voice-assistant` and the secret's name as
the account. `assistant secrets set NAME` stores one.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SERVICE = "victoria-voice-assistant"

# Secrets the assistant knows about, for `assistant secrets list` (others can be stored too).
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


def _keychain_enabled() -> bool:
    # Tests and machines without a Keychain turn it off and use the environment only.
    return os.environ.get("ASSISTANT_KEYCHAIN", "1") != "0"


def get_secret(name: str) -> str | None:
    """The secret's value, or None if it isn't set anywhere. Never logged."""
    value = os.environ.get(name)
    if value:
        return value
    if not _keychain_enabled():
        return None
    import keyring
    from keyring.errors import KeyringError

    try:
        return keyring.get_password(SERVICE, name) or None
    except KeyringError as exc:
        # A locked keychain (no one logged in) or a refused prompt.
        logger.warning("Keychain unavailable", extra={"event": "keychain_unavailable", "secret": name, "reason": type(exc).__name__})
        return None


def set_secret(name: str, value: str) -> None:
    if not value:
        raise SecretsError("empty value")
    if not _keychain_enabled():
        raise SecretsError("the Keychain is turned off (ASSISTANT_KEYCHAIN=0)")
    import keyring

    keyring.set_password(SERVICE, name, value)
    logger.info("Secret stored", extra={"event": "secret_stored", "secret": name})


def delete_secret(name: str) -> bool:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        return False
    logger.info("Secret deleted", extra={"event": "secret_deleted", "secret": name})
    return True
