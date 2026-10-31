from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from victoria_voice_assistant.config.loader import load_config
from victoria_voice_assistant.storage.db import default_database_path

LABEL = "com.victoria.voice-assistant"
WEB_LABEL = "com.victoria.voice-assistant-web"
SEARXNG_LABEL = "com.victoria.searxng"
TEMPLATE = Path(__file__).with_name("launchd_template.plist")
WEB_TEMPLATE = Path(__file__).with_name("launchd_web_template.plist")
SEARXNG_TEMPLATE = Path(__file__).with_name("launchd_searxng_template.plist")


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


def _write_plist(document: dict[str, object], destination: Path, load: bool) -> Path:
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as stream:
        plistlib.dump(document, stream, sort_keys=True)
    if load:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)],
            check=True,
        )
    return destination


def _shared_paths() -> tuple[Path, Path]:
    project_dir = Path(__file__).resolve().parents[2]
    config_dir = Path(os.environ.get("ASSISTANT_CONFIG_DIR", project_dir / "config")).expanduser()
    log_dir = Path(
        os.environ.get(
            "ASSISTANT_LOG_DIR",
            Path.home() / "Library" / "Logs" / "Victoria Voice Assistant",
        )
    ).expanduser()
    return project_dir, config_dir.resolve(), log_dir


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
