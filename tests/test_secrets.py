from __future__ import annotations

import io
import stat
import subprocess
from pathlib import Path

import pytest

from victoria_voice_assistant.cli import main
from victoria_voice_assistant.config.loader import ConfigError, load_config, substitute_environment
from victoria_voice_assistant.secret_store import (
    SecretsError,
    get_secret,
    read_env_file,
    set_secret,
    sync_env_file,
    write_env_file,
)

REPO = Path(__file__).resolve().parents[1]


class FakeKeyring:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        self.items.pop((service, name), None)


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    import keyring

    fake = FakeKeyring()
    monkeypatch.setenv("ASSISTANT_KEYCHAIN", "1")
    monkeypatch.setattr("sys.platform", "darwin")
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, getattr(fake, name))
    return fake


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    return tmp_path / "config" / ".env"  # outside any git repository


def test_set_stores_in_the_keychain_and_env_file(keychain: FakeKeyring, env_file: Path, monkeypatch) -> None:
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert set_secret("HA_TOKEN", "tok-1", env_file) == "the Keychain and config/.env"
    assert keychain.items[("victoria-voice-assistant", "HA_TOKEN")] == "tok-1"
    assert read_env_file(env_file) == {"HA_TOKEN": "tok-1"}
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    set_secret("LMS_API_KEY", "lm-1", env_file)
    set_secret("HA_TOKEN", "tok-2", env_file)  # replaced in place, other lines kept
    assert read_env_file(env_file) == {"HA_TOKEN": "tok-2", "LMS_API_KEY": "lm-1"}
    assert get_secret("HA_TOKEN", env_file) == "tok-2"
    monkeypatch.setenv("HA_TOKEN", "from-environment")
    assert get_secret("HA_TOKEN", env_file) == "from-environment"  # the process environment wins


def test_without_a_keychain_set_writes_the_env_file_only(env_file: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASSISTANT_KEYCHAIN", "0")
    assert set_secret("HA_TOKEN", "tok", env_file) == "config/.env"
    assert read_env_file(env_file) == {"HA_TOKEN": "tok"}


def test_sync_rewrites_the_env_file_from_the_keychain(keychain: FakeKeyring, env_file: Path) -> None:
    keychain.items[("victoria-voice-assistant", "LMS_API_KEY")] = "lm-k"
    keychain.items[("victoria-voice-assistant", "HERMES_API_KEY")] = "h-k"
    write_env_file(env_file, {"LMS_API_KEY": "stale", "OTHER": "kept"})
    assert sync_env_file(env_file) == ["HERMES_API_KEY", "LMS_API_KEY"]
    assert read_env_file(env_file) == {"LMS_API_KEY": "lm-k", "OTHER": "kept", "HERMES_API_KEY": "h-k"}


def test_services_read_the_env_file_not_the_keychain(keychain: FakeKeyring, example_config_dir: Path, monkeypatch) -> None:
    monkeypatch.delenv("LMS_API_KEY", raising=False)
    keychain.items[("victoria-voice-assistant", "LMS_API_KEY")] = "only-in-keychain"
    with pytest.raises(ConfigError, match="assistant secrets set LMS_API_KEY"):
        load_config(example_config_dir)  # after a reboot the Keychain may be locked: services never rely on it
    write_env_file(example_config_dir / ".env", {"LMS_API_KEY": "from-file"})
    assert load_config(example_config_dir).models.servers["lmstudio"].api_key == "from-file"
    import os

    assert "LMS_API_KEY" not in os.environ  # read into memory, not exported to child processes
    assert substitute_environment("${NOT_SET_ANYWHERE:-}", "x.yaml") == ""


def test_a_tracked_env_file_is_refused(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(SecretsError, match="not git-ignored"):
        write_env_file(tmp_path / ".env", {"HA_TOKEN": "x"})
    (tmp_path / ".gitignore").write_text(".env\n")
    write_env_file(tmp_path / ".env", {"HA_TOKEN": "x"})
    assert read_env_file(tmp_path / ".env") == {"HA_TOKEN": "x"}


def test_the_repository_never_tracks_env_files() -> None:
    for path in ("config/.env", ".env", "config/sub/.env", "services/hermes/.env.local", "config/.env.tmp"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO).returncode == 0, path
    assert subprocess.run(["git", "check-ignore", "-q", "config/examples/.env.example"], cwd=REPO).returncode == 1


def test_the_pre_commit_hook_rejects_env_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / ".env").write_text("HA_TOKEN=x\n")
    subprocess.run(["git", "add", "-f", "config/.env"], cwd=tmp_path, check=True)  # forced past .gitignore
    result = subprocess.run(["sh", str(REPO / ".githooks" / "pre-commit")], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1 and "config/.env" in result.stderr


def test_secrets_set_list_and_get(keychain: FakeKeyring, tmp_path: Path, monkeypatch, capsys) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("tok-123\n"))
    assert main(["secrets", "--config-dir", str(config_dir), "set", "HA_TOKEN"]) == 0
    assert "tok-123" not in capsys.readouterr().out
    assert read_env_file(config_dir / ".env") == {"HA_TOKEN": "tok-123"}
    assert main(["secrets", "--config-dir", str(config_dir), "list"]) == 0
    listing = capsys.readouterr().out
    assert "Keychain, config/.env" in listing and "tok-123" not in listing
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert main(["secrets", "--config-dir", str(config_dir), "get", "HA_TOKEN"]) == 1  # never to a terminal
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert main(["secrets", "--config-dir", str(config_dir), "get", "HA_TOKEN"]) == 0
    assert capsys.readouterr().out.endswith("tok-123")
    assert main(["secrets", "--config-dir", str(config_dir), "delete", "HA_TOKEN"]) == 0
    assert read_env_file(config_dir / ".env") == {} and ("victoria-voice-assistant", "HA_TOKEN") not in keychain.items
