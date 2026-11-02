"""Backups (Stage 11): the SQLite database and the configuration in one archive.

`assistant backup create` writes `victoria-<UTC time>.tar.gz` into the `backups` folder beside the
database (or `--dest`): a consistent copy of the database (SQLite's online backup, safe while the
service runs) and `config/` (the YAML files, SearXNG settings, TLS certificates). Never secrets:
`config/.env` and the TLS private keys stay out; secrets come back from the Keychain
(`assistant secrets sync`). The newest `keep` archives are kept. `restore` puts an archive back after
saving what it replaces as a `pre-restore` archive; stop the services first.
"""

from __future__ import annotations

import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PREFIX = "victoria-"
SUFFIX = ".tar.gz"
KEEP = 14
# Never archived: secrets live in the Keychain and config/.env; private keys stay on the machine.
SECRET_NAMES = {".env", "key.pem", "ca-key.pem"}


class BackupError(Exception):
    pass


def default_backup_dir(database: Path) -> Path:
    return database.parent / "backups"


def _config_files(config_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in config_dir.rglob("*")
        if path.is_file()
        and path.name not in SECRET_NAMES
        and not path.name.startswith(".")
        and "examples" not in path.relative_to(config_dir).parts
    )


def create(config_dir: Path, database: Path, dest: Path, keep: int = KEEP, label: str = "") -> Path:
    """Write one archive and prune the oldest beyond `keep`. Returns the archive's path."""
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive = dest / f"{PREFIX}{stamp}{'-' + label if label else ''}{SUFFIX}"
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / database.name
        if database.is_file():
            with sqlite3.connect(database) as source, sqlite3.connect(copy) as target:
                source.backup(target)  # consistent even while the service writes
        partial = archive.with_name(archive.name + ".partial")
        with tarfile.open(partial, "w:gz") as tar:
            if copy.is_file():
                tar.add(copy, arcname=f"database/{database.name}")
            for path in _config_files(config_dir):
                tar.add(path, arcname=f"config/{path.relative_to(config_dir).as_posix()}")
        partial.rename(archive)
    for old in list_backups(dest)[keep:]:
        old.unlink(missing_ok=True)
    return archive


def list_backups(dest: Path) -> list[Path]:
    """Newest first."""
    if not dest.is_dir():
        return []
    return sorted((p for p in dest.glob(f"{PREFIX}*{SUFFIX}") if p.is_file()), key=lambda p: p.name, reverse=True)


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = []
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if not member.isfile() or not parts or parts[0] not in ("database", "config") or ".." in parts or member.name.startswith("/"):
            raise BackupError(f"unexpected entry in the archive: {member.name}")
        if parts[-1] in SECRET_NAMES:
            raise BackupError(f"the archive holds a secret file: {member.name}")
        members.append(member)
    return members


def restore(archive: Path, config_dir: Path, database: Path, dest: Path) -> Path:
    """Put an archive back. What it replaces is saved first; returns that `pre-restore` archive."""
    if not archive.is_file():
        raise BackupError(f"no such backup: {archive}")
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(root, members=_safe_members(tar), filter="data")
        saved = create(config_dir, database, dest, keep=10_000, label="pre-restore")
        restored_db = next((root / "database").glob("*"), None) if (root / "database").is_dir() else None
        if restored_db is not None:
            database.parent.mkdir(parents=True, exist_ok=True)
            staged = database.with_name(database.name + ".restoring")
            shutil.copy2(restored_db, staged)
            for side in ("-wal", "-shm"):
                database.with_name(database.name + side).unlink(missing_ok=True)
            staged.replace(database)
        config_root = root / "config"
        if config_root.is_dir():
            for path in config_root.rglob("*"):
                if path.is_file():
                    target = config_dir / path.relative_to(config_root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
    return saved
