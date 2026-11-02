from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.storage.db import default_database_path

LABEL = "com.victoria.voice-assistant"
WEB_LABEL = "com.victoria.voice-assistant-web"
SEARXNG_LABEL = "com.victoria.searxng"
DECIDER_LABEL = "com.victoria.decider"
BACKUP_LABEL = "com.victoria.backup"
TEMPLATE = Path(__file__).with_name("launchd_template.plist")
WEB_TEMPLATE = Path(__file__).with_name("launchd_web_template.plist")
SEARXNG_TEMPLATE = Path(__file__).with_name("launchd_searxng_template.plist")
DECIDER_TEMPLATE = Path(__file__).with_name("launchd_decider_template.plist")
BACKUP_TEMPLATE = Path(__file__).with_name("launchd_backup_template.plist")
BACKUP_TIME = (3, 30)  # daily, while the house sleeps


def _render(template_path: Path, substitutions: dict[str, str]) -> dict[str, object]:
    template = template_path.read_text(encoding="utf-8")
    for token, value in substitutions.items():
        template = template.replace(token, value)
    return plistlib.loads(template.encode("utf-8"))


def render_launch_agent(
    python_executable: Path,
    project_dir: Path,
    config_dir: Path,
    log_dir: Path,
    data_dir: Path | None = None,
    port: int = 8765,
) -> dict[str, object]:
    return _render(
        TEMPLATE,
        {
            "@PYTHON_EXECUTABLE@": escape(str(python_executable)),
            "@PROJECT_DIR@": escape(str(project_dir)),
            "@CONFIG_DIR@": escape(str(config_dir)),
            "@LOG_DIR@": escape(str(log_dir)),
            "@DATA_DIR@": escape(str(data_dir or default_database_path().parent)),
            "@PORT@": str(port),
        },
    )


def render_web_launch_agent(
    python_executable: Path,
    project_dir: Path,
    config_dir: Path,
    log_dir: Path,
    web_port: int = 8766,
) -> dict[str, object]:
    return _render(
        WEB_TEMPLATE,
        {
            "@PYTHON_EXECUTABLE@": escape(str(python_executable)),
            "@PROJECT_DIR@": escape(str(project_dir)),
            "@CONFIG_DIR@": escape(str(config_dir)),
            "@LOG_DIR@": escape(str(log_dir)),
            "@WEB_PORT@": str(web_port),
        },
    )


def render_searxng_launch_agent(
    python_executable: Path,
    project_dir: Path,
    config_dir: Path,
    log_dir: Path,
    searxng_dir: Path,
) -> dict[str, object]:
    return _render(
        SEARXNG_TEMPLATE,
        {
            "@PYTHON_EXECUTABLE@": escape(str(python_executable)),
            "@PROJECT_DIR@": escape(str(project_dir)),
            "@CONFIG_DIR@": escape(str(config_dir)),
            "@LOG_DIR@": escape(str(log_dir)),
            "@SEARXNG_DIR@": escape(str(searxng_dir)),
        },
    )


def render_decider_launch_agent(
    python_executable: Path,
    project_dir: Path,
    config_dir: Path,
    log_dir: Path,
) -> dict[str, object]:
    return _render(
        DECIDER_TEMPLATE,
        {
            "@PYTHON_EXECUTABLE@": escape(str(python_executable)),
            "@PROJECT_DIR@": escape(str(project_dir)),
            "@CONFIG_DIR@": escape(str(config_dir)),
            "@LOG_DIR@": escape(str(log_dir)),
        },
    )


def _write_plist(document: dict[str, object], destination: Path, load: bool) -> Path:
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as stream:
        plistlib.dump(document, stream, sort_keys=True)
    if load:
        # Reinstalling replaces the running agent: unload the old one first (fine if it wasn't loaded).
        label = str(document["Label"])
        target = f"gui/{os.getuid()}/{label}"
        subprocess.run(["launchctl", "bootout", target], capture_output=True, check=False)
        # bootout returns before the old service has stopped; loading again until then fails (error 5).
        for _ in range(100):
            if subprocess.run(["launchctl", "print", target], capture_output=True, check=False).returncode != 0:
                break
            time.sleep(0.1)
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)],
            check=True,
        )
    return destination


def log_directory() -> Path:
    """Where the LaunchAgents write each service's output."""
    return Path(os.environ.get("ASSISTANT_LOG_DIR", Path.home() / "Library" / "Logs" / "Victoria Voice Assistant")).expanduser()


def _shared_paths() -> tuple[Path, Path, Path]:
    project_dir = Path(__file__).resolve().parents[2]
    config_dir = Path(os.environ.get("ASSISTANT_CONFIG_DIR", project_dir / "config")).expanduser()
    return project_dir, config_dir.resolve(), log_directory()


def install_launch_agent(output: Path | None = None, load: bool = False) -> Path:
    project_dir, config_dir, log_dir = _shared_paths()
    load_config(config_dir)
    data_dir = Path(os.environ.get("ASSISTANT_DATA_DIR", default_database_path().parent)).expanduser()
    port = int(os.environ.get("ASSISTANT_PORT", "8765"))
    destination = output or (Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist")
    log_dir.mkdir(parents=True, exist_ok=True)
    document = render_launch_agent(
        Path(sys.executable),
        project_dir,
        config_dir,
        log_dir,
        data_dir.resolve(),
        port,
    )
    return _write_plist(document, destination, load)


def install_web_launch_agent(output: Path | None = None, load: bool = False) -> Path:
    project_dir, config_dir, log_dir = _shared_paths()
    load_config(config_dir)
    web_port = int(os.environ.get("ASSISTANT_WEB_PORT", "8766"))
    destination = output or (Path.home() / "Library" / "LaunchAgents" / f"{WEB_LABEL}.plist")
    log_dir.mkdir(parents=True, exist_ok=True)
    document = render_web_launch_agent(Path(sys.executable), project_dir, config_dir, log_dir, web_port)
    return _write_plist(document, destination, load)


def install_searxng_launch_agent(output: Path | None = None, load: bool = False) -> Path:
    from victoria_voice_assistant.search.searxng import source_dir

    project_dir, config_dir, log_dir = _shared_paths()
    destination = output or (Path.home() / "Library" / "LaunchAgents" / f"{SEARXNG_LABEL}.plist")
    log_dir.mkdir(parents=True, exist_ok=True)
    document = render_searxng_launch_agent(Path(sys.executable), project_dir, config_dir, log_dir, source_dir())
    return _write_plist(document, destination, load)


def install_decider_launch_agent(output: Path | None = None, load: bool = False) -> Path:
    project_dir, config_dir, log_dir = _shared_paths()
    load_config(config_dir)
    destination = output or (Path.home() / "Library" / "LaunchAgents" / f"{DECIDER_LABEL}.plist")
    log_dir.mkdir(parents=True, exist_ok=True)
    document = render_decider_launch_agent(Path(sys.executable), project_dir, config_dir, log_dir)
    return _write_plist(document, destination, load)


def render_backup_launch_agent(
    python_executable: Path,
    project_dir: Path,
    config_dir: Path,
    log_dir: Path,
    data_dir: Path,
    at: tuple[int, int] = BACKUP_TIME,
) -> dict[str, object]:
    return _render(
        BACKUP_TEMPLATE,
        {
            "@PYTHON_EXECUTABLE@": escape(str(python_executable)),
            "@PROJECT_DIR@": escape(str(project_dir)),
            "@CONFIG_DIR@": escape(str(config_dir)),
            "@LOG_DIR@": escape(str(log_dir)),
            "@DATA_DIR@": escape(str(data_dir)),
            "@HOUR@": str(at[0]),
            "@MINUTE@": str(at[1]),
        },
    )


def install_backup_launch_agent(output: Path | None = None, load: bool = False) -> Path:
    """The daily job, `assistant daily`: a backup, then log rotation (Stage 11)."""
    project_dir, config_dir, log_dir = _shared_paths()
    data_dir = Path(os.environ.get("ASSISTANT_DATA_DIR", default_database_path().parent)).expanduser()
    destination = output or (Path.home() / "Library" / "LaunchAgents" / f"{BACKUP_LABEL}.plist")
    log_dir.mkdir(parents=True, exist_ok=True)
    document = render_backup_launch_agent(Path(sys.executable), project_dir, config_dir, log_dir, data_dir.resolve())
    return _write_plist(document, destination, load)
