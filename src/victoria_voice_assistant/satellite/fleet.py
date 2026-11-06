"""Every satellite's firmware at once (Stage 13): `assistant satellites status` and `update`.

`update` builds our firmware once (`services/satellite1/build.sh`), uploads it to each satellite in
`tools.yaml` over Wi-Fi in turn (`build.sh --upload HOST`), then reads back each one's version. A
satellite that is offline or fails is reported and the rest go ahead. Reading a version opens a second
API connection beside the assistant's; ESPHome allows several.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from victoria_voice_assistant.config.schema import SatelliteConfig

BUILD = Path(__file__).resolve().parents[3] / "services" / "satellite1" / "build.sh"


@dataclass
class DeviceVersion:
    name: str
    host: str
    online: bool
    firmware: str = ""  # the project version FutureProofHomes' build reports
    esphome: str = ""
    built: str = ""  # when our build was compiled
    error: str = ""


async def read_version(name: str, settings: SatelliteConfig, timeout: float = 10.0) -> DeviceVersion:
    from aioesphomeapi import APIClient

    client = APIClient(settings.host, settings.port, None, noise_psk=settings.key or None, client_info="victoria-fleet")
    try:
        await asyncio.wait_for(client.connect(login=True), timeout)
        info = await client.device_info()
        return DeviceVersion(name, settings.host, True, info.project_version, info.esphome_version, info.compilation_time)
    except Exception as exc:  # noqa: BLE001 - offline, wrong key: reported, not raised
        return DeviceVersion(name, settings.host, False, error=str(exc) or type(exc).__name__)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def versions(
    satellites: dict[str, SatelliteConfig],
    read: Callable[[str, SatelliteConfig], Awaitable[DeviceVersion]] = read_version,
) -> list[DeviceVersion]:
    return list(await asyncio.gather(*(read(name, settings) for name, settings in satellites.items())))


def update(
    satellites: dict[str, SatelliteConfig],
    run: Callable[[list[str]], int] = lambda command: subprocess.run(command, check=False).returncode,
) -> dict[str, bool]:
    """Build once, then upload to each satellite. Returns name -> uploaded; empty if the build failed."""
    if run(["sh", str(BUILD)]) != 0:
        return {}
    return {name: run(["sh", str(BUILD), "--upload", settings.host]) == 0 for name, settings in satellites.items()}


def report(found: list[DeviceVersion]) -> str:
    lines = []
    for device in found:
        if device.online:
            lines.append(f"{device.name} ({device.host}): firmware {device.firmware}, ESPHome {device.esphome}, built {device.built}")
        else:
            lines.append(f"{device.name} ({device.host}): unreachable ({device.error})")
    return "\n".join(lines) or "No satellites in tools.yaml."


def as_dicts(found: list[DeviceVersion]) -> list[dict[str, Any]]:
    from dataclasses import asdict

    return [asdict(device) for device in found]
