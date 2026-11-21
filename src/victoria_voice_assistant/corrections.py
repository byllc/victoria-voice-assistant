"""Corrections: words speech recognition mishears, and what they mean (feature 20261114-162903).

Kept in `config/corrections.yaml`, one section per use (only `wine` so far), so Bill can read and edit
them:

    wine:
      tempra nioh: Tempranillo

The file is read on every use, so an edit applies at once. Victoria writes it when it learns a correction
(told one, or a close spelling found the wine); comments in the file aren't kept when it does.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

FILENAME = "corrections.yaml"
HEADER = (
    "# Words speech recognition mishears, and what they mean: `heard: meant`, one section per use.\n"
    "# Victoria adds to this file when it learns one; edit or delete lines freely (comments aren't kept).\n"
)


def plain(text: str) -> str:
    """Lower case, no accents, words separated by single spaces: how heard phrases are stored and matched."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return " ".join(re.findall(r"[\w'-]+", text))


def default_path() -> Path:
    from victoria_voice_assistant.config.loader import default_config_dir

    return default_config_dir() / FILENAME


class Corrections:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Corrections file unreadable", extra={"event": "corrections_unreadable", "reason": type(exc).__name__})
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(scope): {plain(str(heard)): str(meant).strip() for heard, meant in entries.items() if plain(str(heard)) and str(meant).strip()}
            for scope, entries in data.items()
            if isinstance(entries, dict)
        }

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        data = {scope: dict(sorted(entries.items())) for scope, entries in sorted(data.items()) if entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False) if data else ""
        temporary.write_text(HEADER + body, encoding="utf-8")
        os.replace(temporary, self.path)

    def entries(self, scope: str) -> dict[str, str]:
        return self._read().get(scope, {})

    def learn(self, scope: str, heard: str, meant: str) -> bool:
        """Save `heard` → `meant`; False when there's nothing to save (empty, or the same words)."""
        key, meant = plain(heard), meant.strip()
        if not key or not meant or key == plain(meant):
            return False
        with self._lock:
            data = self._read()
            if data.get(scope, {}).get(key) == meant:
                return True
            data.setdefault(scope, {})[key] = meant
            self._write(data)
        logger.info("Correction learned", extra={"event": "correction_learned", "scope": scope, "heard": key, "meant": meant})
        return True

    def forget(self, scope: str, heard: str) -> str | None:
        """Remove the correction for `heard`; what it meant, or None when there wasn't one."""
        key = plain(heard)
        with self._lock:
            data = self._read()
            meant = data.get(scope, {}).pop(key, None)
            if meant is not None:
                self._write(data)
        return meant

    def apply(self, scope: str, text: str) -> tuple[str, list[tuple[str, str]]]:
        """`text` with each known mishearing replaced (whole words, longest first), and the ones used."""
        result, used = plain(text), []
        for heard, meant in sorted(self.entries(scope).items(), key=lambda item: -len(item[0])):
            pattern = rf"(?<![\w'-]){re.escape(heard)}(?![\w'-])"
            if re.search(pattern, result):
                result = re.sub(pattern, meant, result)
                used.append((heard, meant))
        return (result if used else text), used
