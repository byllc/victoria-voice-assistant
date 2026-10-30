from __future__ import annotations

import sqlite3
import os
import sys
from pathlib import Path


def default_database_path() -> Path:
    configured = os.environ.get("ASSISTANT_DATA_DIR")
    if configured:
        directory = Path(configured).expanduser()
    elif sys.platform == "darwin":
        directory = Path.home() / "Library" / "Application Support" / "Victoria Voice Assistant"
    else:
        directory = Path.home() / ".local" / "share" / "victoria-voice-assistant"
    return directory / "assistant.sqlite3"


def migrate(database_path: Path | None = None) -> Path:
    path = (database_path or default_database_path()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    migration_dir = Path(__file__).with_name("migrations")

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for migration in sorted(migration_dir.glob("*.sql")):
            if migration.name in applied:
                continue
            script = migration.read_text(encoding="utf-8")
            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + "\nINSERT INTO schema_migrations (version) VALUES ("
                    + "'" + migration.name + "');\nCOMMIT;"
                )
            except sqlite3.Error:
                connection.rollback()
                raise
    return path
