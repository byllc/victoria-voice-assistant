from __future__ import annotations

import shutil
from pathlib import Path

import pytest


EXAMPLES = Path(__file__).resolve().parents[1] / "config" / "examples"


@pytest.fixture
def example_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(EXAMPLES, config_dir, ignore=shutil.ignore_patterns(".env.example"))
    monkeypatch.setenv("LMS_API_KEY", "test-api-key")
    return config_dir
