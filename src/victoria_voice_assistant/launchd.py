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
TEMPLATE = Path(__file__).with_name("launchd_template.plist")


def render_launch_agent(
    python_executable: Path,
    project_dir: Path,
    config_dir: Path,
    log_dir: Path,
    data_dir: Path | None = None,
    port: int = 8765,
) -> dict[str, object]:
    template = TEMPLATE.read_text(encoding="utf-8")
    substitutions = {
        "@PYTHON_EXECUTABLE@": escape(str(python_executable)),
        "@PROJECT_DIR@": escape(str(project_dir)),
        "@CONFIG_DIR@": escape(str(config_dir)),
        "@LOG_DIR@": escape(str(log_dir)),
        "@DATA_DIR@": escape(str(data_dir or default_database_path().parent)),
        "@PORT@": str(port),
    }
    for token, value in substitutions.items():
        template = template.replace(token, value)
    return plistlib.loads(template.encode("utf-8"))


def install_launch_agent(output: Path | None = None, load: bool = False) -> Path:
    project_dir = Path(__file__).resolve().parents[2]
    config_dir = Path(os.environ.get("ASSISTANT_CONFIG_DIR", project_dir / "config")).expanduser()
    config_dir = config_dir.resolve()
    load_config(config_dir)
    log_dir = Path(
        os.environ.get(
            "ASSISTANT_LOG_DIR",
            Path.home() / "Library" / "Logs" / "Victoria Voice Assistant",
        )
    ).expanduser()
    data_dir = Path(os.environ.get("ASSISTANT_DATA_DIR", default_database_path().parent)).expanduser()
    port = int(os.environ.get("ASSISTANT_PORT", "8765"))
    destination = output or (
        Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    )
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    document = render_launch_agent(
        Path(sys.executable),
        project_dir,
        config_dir,
        log_dir,
        data_dir.resolve(),
        port,
    )
    with destination.open("wb") as stream:
        plistlib.dump(document, stream, sort_keys=True)
    if load:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)],
            check=True,
        )
    return destination
