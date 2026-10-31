from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from dotenv import dotenv_values


EXAMPLES = Path(__file__).resolve().parents[1] / "config" / "examples"
LOCAL_ENV = Path(__file__).resolve().parents[1] / "config" / ".env"


@pytest.fixture(autouse=True)
def disable_model_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_PIN_MODELS", "0")
    monkeypatch.setenv("ASSISTANT_SPEECH", "0")
    monkeypatch.setenv("ASSISTANT_BROWSER", "0")


@pytest.fixture
def example_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(EXAMPLES, config_dir, ignore=shutil.ignore_patterns(".env.example"))
    monkeypatch.setenv("LMS_API_KEY", "test-api-key")
    return config_dir


@pytest.fixture
def live_api_key(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live tests talk to the real LM Studio, which may require the key from config/.env."""
    key = dotenv_values(LOCAL_ENV).get("LMS_API_KEY") if LOCAL_ENV.is_file() else None
    if key:
        monkeypatch.setenv("LMS_API_KEY", key)
