from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from victoria_voice_assistant.config.schema import (
    AgentsConfig,
    AppConfig,
    ModelsConfig,
    ReferenceValidationError,
    ToolsConfig,
    validate_references,
)
from victoria_voice_assistant.secret_store import ENV_FILE, read_env_file

T = TypeVar("T", bound=BaseModel)
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")  # ${NAME} or ${NAME:-default}


class ConfigError(ValueError):
    def __init__(self, filename: str, field: str, message: str) -> None:
        self.filename = filename
        self.field = field
        self.message = message
        super().__init__(f"{filename}: {field}: {message}")


def default_config_dir() -> Path:
    configured = os.environ.get("ASSISTANT_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    project_dir = Path(__file__).resolve().parents[3]
    if (project_dir / "pyproject.toml").is_file():
        return project_dir / "config"
    return Path.cwd() / "config"


def substitute_environment(value: Any, filename: str, field: str = "", secrets: dict[str, str] | None = None) -> Any:
    """Replace ${NAME} with the process environment's value, else the one from `config/.env` (`secrets`)."""
    if isinstance(value, dict):
        return {
            key: substitute_environment(
                child,
                filename,
                f"{field}.{key}" if field else str(key),
                secrets,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            substitute_environment(
                child,
                filename,
                f"{field}.{index}" if field else str(index),
                secrets,
            )
            for index, child in enumerate(value)
        ]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        result = os.environ.get(name) or (secrets or {}).get(name)
        if not result and default is not None:
            return default  # optional, e.g. a cloud provider's key that may not be set up yet
        if not result:
            raise ConfigError(
                filename,
                field or "$",
                f"{name} is not set (store it with `uv run assistant secrets set {name}`; docs/secrets.md)",
            )
        return result

    return ENV_PATTERN.sub(replace, value)


def _read_yaml(config_dir: Path, filename: str, model: type[T], secrets: dict[str, str] | None = None) -> T:
    path = config_dir / filename
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(filename, "$", "file not found") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "$"
        problem = getattr(exc, "problem", None) or "parse error"
        raise ConfigError(filename, location, f"invalid YAML: {problem}") from exc
    except OSError as exc:
        raise ConfigError(filename, "$", f"cannot read file: {exc}") from exc

    if raw is None:
        raw = {}
    resolved = substitute_environment(raw, filename, secrets=secrets)
    try:
        return model.model_validate(resolved)
    except ValidationError as exc:
        errors = [
            (
                ".".join(str(part) for part in error["loc"]) or "$",
                error["msg"],
            )
            for error in exc.errors()
        ]
        field, message = errors[0]
        remaining = "; ".join(
            f"{error_field}: {error_message}"
            for error_field, error_message in errors[1:]
        )
        if remaining:
            message = f"{message}; {remaining}"
        raise ConfigError(filename, field, message) from exc


def load_config(config_dir: Path | None = None) -> AppConfig:
    directory = (config_dir or default_config_dir()).expanduser().resolve()
    if not directory.is_dir():
        raise ConfigError(str(directory), "$", "configuration directory does not exist")

    # Secrets from config/.env are read into memory for ${NAME}, not exported to the process environment.
    secrets = read_env_file(directory / ENV_FILE)
    models = _read_yaml(directory, "models.yaml", ModelsConfig, secrets)
    tools = _read_yaml(directory, "tools.yaml", ToolsConfig, secrets)
    agents = _read_yaml(directory, "agents.yaml", AgentsConfig, secrets)

    try:
        validate_references(models, tools, agents)
    except ReferenceValidationError as exc:
        raise ConfigError(exc.filename, exc.field, exc.message) from exc

    return AppConfig(models=models, tools=tools, agents=agents)
