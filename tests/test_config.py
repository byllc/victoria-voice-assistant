from __future__ import annotations

from pathlib import Path

import pytest

from victoria_voice_assistant.config.loader import (
    ConfigError,
    default_config_dir,
    load_config,
)


def test_example_config_validates_and_uses_same_model_for_roles(
    example_config_dir: Path,
) -> None:
    config = load_config(example_config_dir)

    assert config.models.models["fast"].model == "google/gemma-4-12b"
    assert config.models.models["fast-small"].model == "google/gemma-4-12b"
    assert config.models.roles.router == "fast"
    assert config.tools.tools[0].kind == "stub"
    assert config.agents.default == "helper"
    assert config.agents.agents[0].model == "fast"


def test_missing_environment_variable_names_file_and_field(
    example_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LMS_API_KEY")

    with pytest.raises(ConfigError, match=r"models\.yaml: servers\.lmstudio\.api_key"):
        load_config(example_config_dir)


def test_reference_error_names_config_file_and_field(example_config_dir: Path) -> None:
    models_file = example_config_dir / "models.yaml"
    models_file.write_text(
        models_file.read_text(encoding="utf-8").replace("router: fast", "router: unknown"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"models\.yaml: roles\.router"):
        load_config(example_config_dir)


def test_architecture_unquoted_off_value_is_accepted(example_config_dir: Path) -> None:
    models_file = example_config_dir / "models.yaml"
    models_file.write_text(
        models_file.read_text(encoding="utf-8").replace(
            'pre_router: "off"', "pre_router: off"
        ),
        encoding="utf-8",
    )

    assert load_config(example_config_dir).models.roles.pre_router == "off"


def test_default_config_dir_obeys_environment(
    example_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSISTANT_CONFIG_DIR", str(example_config_dir))

    assert default_config_dir() == example_config_dir.resolve()


def test_default_config_dir_uses_project_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASSISTANT_CONFIG_DIR", raising=False)

    assert default_config_dir() == Path(__file__).resolve().parents[1] / "config"
