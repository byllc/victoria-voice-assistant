from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from victoria_voice_assistant.cli import main
from victoria_voice_assistant.storage import backup


def make_setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    config = tmp_path / "config"
    (config / "tls").mkdir(parents=True)
    (config / "examples").mkdir()
    (config / "tools.yaml").write_text("tools: []\n")
    (config / ".env").write_text("HA_TOKEN=secret\n")
    (config / "tls" / "cert.pem").write_text("cert")
    (config / "tls" / "key.pem").write_text("private")
    (config / "examples" / "tools.yaml").write_text("example")
    database = tmp_path / "data" / "assistant.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE timers (name TEXT)")
        db.execute("INSERT INTO timers VALUES ('pasta')")
    return config, database, tmp_path / "backups"


def test_a_backup_holds_the_database_and_config_but_never_secrets(tmp_path: Path) -> None:
    config, database, dest = make_setup(tmp_path)
    archive = backup.create(config, database, dest)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert names == {"database/assistant.sqlite3", "config/tools.yaml", "config/tls/cert.pem"}


def test_old_backups_are_pruned(tmp_path: Path) -> None:
    config, database, dest = make_setup(tmp_path)
    dest.mkdir()
    for day in range(1, 6):
        (dest / f"victoria-2026010{day}-000000.tar.gz").write_bytes(b"x")
    backup.create(config, database, dest, keep=3)
    kept = [p.name for p in backup.list_backups(dest)]
    assert len(kept) == 3 and kept[1:] == ["victoria-20260105-000000.tar.gz", "victoria-20260104-000000.tar.gz"]


def test_restore_brings_back_the_data_and_saves_what_it_replaced(tmp_path: Path) -> None:
    config, database, dest = make_setup(tmp_path)
    archive = backup.create(config, database, dest)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM timers")
    (config / "tools.yaml").write_text("broken: [\n")
    saved = backup.restore(archive, config, database, dest)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT name FROM timers").fetchall() == [("pasta",)]
    assert (config / "tools.yaml").read_text() == "tools: []\n"
    assert (config / ".env").read_text() == "HA_TOKEN=secret\n"  # untouched
    assert (config / "tls" / "key.pem").read_text() == "private"
    assert "pre-restore" in saved.name and saved.is_file()


def test_restore_refuses_an_archive_with_unexpected_paths(tmp_path: Path) -> None:
    config, database, dest = make_setup(tmp_path)
    evil = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="config/../../outside.txt")
    with pytest.raises(backup.BackupError):
        backup.restore(evil, config, database, dest)
    assert not (tmp_path / "outside.txt").exists()


def test_the_backup_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config, database, dest = make_setup(tmp_path)
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(database.parent))
    assert main(["backup", "--config-dir", str(config), "create"]) == 0
    assert main(["backup", "--config-dir", str(config), "list"]) == 0
    out = capsys.readouterr().out
    name = next(line.split()[0] for line in out.splitlines() if line.startswith("victoria-"))
    assert main(["backup", "--config-dir", str(config), "restore", name]) == 0
    assert main(["backup", "--config-dir", str(config), "restore", "victoria-nope.tar.gz"]) == 1
    assert (database.parent / "backups").is_dir()


def test_the_daily_backup_launch_agent(tmp_path: Path) -> None:
    import plistlib

    from victoria_voice_assistant.launchd import install_backup_launch_agent, render_backup_launch_agent

    document = render_backup_launch_agent(Path("/py"), Path("/proj"), Path("/cfg"), Path("/logs"), Path("/data"))
    assert document["Label"] == "com.victoria.backup"
    assert document["ProgramArguments"][-1] == "daily"
    assert document["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert "KeepAlive" not in document  # runs once a day, not kept alive
    written = install_backup_launch_agent(tmp_path / "backup.plist")
    assert plistlib.loads(written.read_bytes())["EnvironmentVariables"]["ASSISTANT_DATA_DIR"]


def test_logs_over_the_limit_are_compressed_and_emptied(tmp_path: Path) -> None:
    import gzip

    from victoria_voice_assistant.log_rotation import rotate

    big, small = tmp_path / "assistant-error.log", tmp_path / "web.log"
    big.write_text("x" * 200)
    small.write_text("y" * 10)
    for n in range(1, 4):
        (tmp_path / f"assistant-error.log.{n}.gz").write_bytes(gzip.compress(f"old {n}".encode()))
    assert rotate(tmp_path, max_bytes=100, keep=3) == [big]
    assert big.read_text() == "" and small.read_text() == "y" * 10
    assert gzip.decompress((tmp_path / "assistant-error.log.1.gz").read_bytes()) == b"x" * 200
    assert gzip.decompress((tmp_path / "assistant-error.log.2.gz").read_bytes()) == b"old 1"
    assert gzip.decompress((tmp_path / "assistant-error.log.3.gz").read_bytes()) == b"old 2"
    assert not (tmp_path / "assistant-error.log.4.gz").exists()  # "old 3" dropped


def test_the_daily_job_backs_up_and_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, database, _ = make_setup(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "assistant-error.log").write_bytes(b"z" * (6 * 1024 * 1024))
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(database.parent))
    monkeypatch.setenv("ASSISTANT_LOG_DIR", str(logs))
    assert main(["daily", "--config-dir", str(config)]) == 0
    assert backup.list_backups(database.parent / "backups")
    assert (logs / "assistant-error.log").stat().st_size == 0 and (logs / "assistant-error.log.1.gz").is_file()
