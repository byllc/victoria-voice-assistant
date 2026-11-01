from __future__ import annotations

import io
from pathlib import Path

import pytest

import victoria_voice_assistant.cli as cli
from victoria_voice_assistant.cli import main
from victoria_voice_assistant.config.loader import ConfigError, load_config, substitute_environment
from victoria_voice_assistant.secret_store import SecretsError, get_secret, set_secret


class FakeKeyring:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.items[(service, name)] = value


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    import keyring

    fake = FakeKeyring()
    monkeypatch.setenv("ASSISTANT_KEYCHAIN", "1")
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    return fake


def test_secrets_come_from_the_environment_then_the_keychain(keychain: FakeKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert get_secret("HA_TOKEN") is None
    set_secret("HA_TOKEN", "from-keychain")
    assert keychain.items[("victoria-voice-assistant", "HA_TOKEN")] == "from-keychain"
    assert get_secret("HA_TOKEN") == "from-keychain"
    monkeypatch.setenv("HA_TOKEN", "from-environment")
    assert get_secret("HA_TOKEN") == "from-environment"  # an explicit environment value wins (tests, one-off runs)


def test_keychain_off_uses_the_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_KEYCHAIN", "0")
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert get_secret("HA_TOKEN") is None
    with pytest.raises(SecretsError):
        set_secret("HA_TOKEN", "x")


def test_config_reads_keychain_secrets_and_no_env_file(keychain: FakeKeyring, example_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LMS_API_KEY", raising=False)
    (example_config_dir / ".env").write_text("LMS_API_KEY=from-a-file\n")
    with pytest.raises(ConfigError, match="assistant secrets set LMS_API_KEY"):
        load_config(example_config_dir)  # a .env file in config/ is not read any more
    set_secret("LMS_API_KEY", "from-keychain")
    config = load_config(example_config_dir)
    assert config.models.servers["lmstudio"].api_key == "from-keychain"
    assert substitute_environment("${NOT_SET_ANYWHERE:-}", "x.yaml") == ""


def test_secrets_set_reads_a_hidden_prompt_or_stdin(keychain: FakeKeyring, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("tok-123\n"))
    assert main(["secrets", "set", "HA_TOKEN"]) == 0
    assert keychain.items[("victoria-voice-assistant", "HA_TOKEN")] == "tok-123"
    assert "tok-123" not in capsys.readouterr().out
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert main(["secrets", "list"]) == 0
    listing = capsys.readouterr().out
    assert "set      HA_TOKEN" in listing and "tok-123" not in listing


def test_secrets_get_is_for_scripts_only(keychain: FakeKeyring, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    set_secret("HERMES_API_KEY", "abc")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert main(["secrets", "get", "HERMES_API_KEY"]) == 1
    assert "abc" not in capsys.readouterr().out
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert main(["secrets", "get", "HERMES_API_KEY"]) == 0
    assert capsys.readouterr().out == "abc"


def test_import_env_moves_secrets_and_deletes_the_file(keychain: FakeKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    for name in ("LMS_API_KEY", "HERMES_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("# keys\nLMS_API_KEY=lm-1\nHERMES_API_KEY='h-2'\nEMPTY=\n")
    assert main(["secrets", "import-env", str(env)]) == 0
    assert get_secret("LMS_API_KEY") == "lm-1" and get_secret("HERMES_API_KEY") == "h-2"
    assert not env.exists()
    out = capsys.readouterr().out
    assert "lm-1" not in out and "h-2" not in out
