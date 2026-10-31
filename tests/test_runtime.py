from __future__ import annotations

import asyncio
import logging
import plistlib
import time
from pathlib import Path

import httpx

from victoria_voice_assistant.app import create_app
from victoria_voice_assistant.cli import main
from victoria_voice_assistant.config.manager import ConfigManager
from victoria_voice_assistant.launchd import render_launch_agent
from victoria_voice_assistant.storage.db import migrate


def test_invalid_reload_keeps_previous_config(
    example_config_dir: Path,
    caplog,
) -> None:
    manager = ConfigManager(example_config_dir)
    original = manager.current
    revision = manager.revision
    (example_config_dir / "tools.yaml").write_text("tools: [broken", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        assert manager.reload() is False

    assert manager.current is original
    assert manager.revision == revision
    record = caplog.records[-1]
    assert record.event == "config_reload_rejected"
    assert "tools.yaml" in record.error


def test_health_endpoint_and_config_file_watcher(
    example_config_dir: Path,
    tmp_path: Path,
) -> None:
    app = create_app(example_config_dir, tmp_path / "assistant.sqlite3")

    async def exercise_app() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/health")
                assert response.json() == {"status": "ok", "config_revision": 1, "speech": "off"}
                manager: ConfigManager = app.state.config_manager
                tools_file = example_config_dir / "tools.yaml"
                tools_file.write_text(
                    tools_file.read_text(encoding="utf-8") + "\n# valid edit\n",
                    encoding="utf-8",
                )
                deadline = time.monotonic() + 3
                while manager.revision == 1 and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)

                assert manager.revision == 2
                response = await client.get("/health")
                assert response.json()["config_revision"] == 2

    asyncio.run(exercise_app())


def test_database_migrations_are_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "state" / "assistant.sqlite3"

    migrate(database)
    migrate(database)

    import sqlite3

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        migrations = connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    assert {"service_state", "timers"} <= tables
    assert sorted(migrations) == [("0001_initial.sql",), ("0002_timers.sql",), ("0003_tasks.sql",), ("0004_spend.sql",), ("0005_notifications.sql",), ("0006_task_questions.sql",), ("0007_task_progress.sql",)]


def test_launch_agent_template_renders_a_valid_plist(tmp_path: Path) -> None:
    document = render_launch_agent(
        Path("/tmp/venv/bin/python"),
        tmp_path,
        tmp_path / "config",
        tmp_path / "logs",
        tmp_path / "data",
    )
    encoded = plistlib.dumps(document)
    parsed = plistlib.loads(encoded)

    assert parsed["KeepAlive"] is True
    assert parsed["ProgramArguments"] == [
        "/tmp/venv/bin/python",
        "-m",
        "victoria_voice_assistant",
        "serve",
    ]
    assert parsed["EnvironmentVariables"]["ASSISTANT_CONFIG_DIR"] == str(
        tmp_path / "config"
    )
    assert parsed["EnvironmentVariables"]["ASSISTANT_DATA_DIR"] == str(
        tmp_path / "data"
    )
    assert parsed["EnvironmentVariables"]["ASSISTANT_LOG_DIR"] == str(
        tmp_path / "logs"
    )


def test_config_check_cli_accepts_example_files(
    example_config_dir: Path,
    capsys,
) -> None:
    assert main(["config", "check", "--config-dir", str(example_config_dir)]) == 0
    assert "Configuration valid" in capsys.readouterr().out


def test_config_init_copies_examples_without_overwriting(
    tmp_path: Path,
    capsys,
) -> None:
    config_dir = tmp_path / "new-config"

    assert main(["config", "init", "--config-dir", str(config_dir)]) == 0
    assert all(
        (config_dir / filename).is_file()
        for filename in ("models.yaml", "tools.yaml", "agents.yaml")
    )
    assert main(["config", "init", "--config-dir", str(config_dir)]) == 1
    assert "Refusing to overwrite" in capsys.readouterr().err
