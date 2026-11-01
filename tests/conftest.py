from __future__ import annotations

import shutil
from pathlib import Path

import pytest


EXAMPLES = Path(__file__).resolve().parents[1] / "config" / "examples"


@pytest.fixture(autouse=True)
def disable_model_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_PIN_MODELS", "0")
    monkeypatch.setenv("ASSISTANT_SPEECH", "0")
    monkeypatch.setenv("ASSISTANT_BROWSER", "0")
    monkeypatch.setenv("ASSISTANT_KEYCHAIN", "0")  # tests never read or write the real Keychain


@pytest.fixture
def example_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(EXAMPLES, config_dir, ignore=shutil.ignore_patterns(".env.example"))
    monkeypatch.setenv("LMS_API_KEY", "test-api-key")
    return config_dir


@pytest.fixture
def live_api_key(example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live tests talk to the real LM Studio, which may require its key from the Keychain."""
    monkeypatch.setenv("ASSISTANT_KEYCHAIN", "1")
    monkeypatch.delenv("LMS_API_KEY", raising=False)
    from victoria_voice_assistant.secret_store import get_secret

    key = get_secret("LMS_API_KEY")
    if key:
        monkeypatch.setenv("LMS_API_KEY", key)
