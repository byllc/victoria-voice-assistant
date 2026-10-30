from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from victoria_voice_assistant.config.schema import (
    AgentsConfig,
    AppConfig,
    ModelsConfig,
    ReferenceValidationError,
    ToolsConfig,
    validate_references,
)

T = TypeVar("T", bound=BaseModel)
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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


def substitute_environment(value: Any, filename: str, field: str = "") -> Any:
    if isinstance(value, dict):
        return {
            key: substitute_environment(
                child,
                filename,
                f"{field}.{key}" if field else str(key),
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            substitute_environment(
                child,
                filename,
                f"{field}.{index}" if field else str(index),
            )
            for index, child in enumerate(value)
        ]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        result = os.environ.get(name)
        if not result:
            raise ConfigError(
                filename,
                field or "$",
                f"environment variable {name} is not set or is empty",
            )
        return result

    return ENV_PATTERN.sub(replace, value)


def _read_yaml(config_dir: Path, filename: str, model: type[T]) -> T:
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
    resolved = substitute_environment(raw, filename)
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

    load_dotenv(directory / ".env", override=False)
    models = _read_yaml(directory, "models.yaml", ModelsConfig)
    tools = _read_yaml(directory, "tools.yaml", ToolsConfig)
    agents = _read_yaml(directory, "agents.yaml", AgentsConfig)

    try:
        validate_references(models, tools, agents)
    except ReferenceValidationError as exc:
        raise ConfigError(exc.filename, exc.field, exc.message) from exc

    return AppConfig(models=models, tools=tools, agents=agents)
