"""Log rotation (Stage 11) for the files launchd writes each service's output to.

launchd holds those files open for appending, so a log is copied to `<name>.1.gz` and then emptied in
place ("copy and truncate"); the service keeps writing to the same file. Older copies shift up
(`.1.gz` -> `.2.gz`) and only the newest `keep` are kept. Run daily by `assistant daily`.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
KEEP = 5


def rotate(log_dir: Path, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> list[Path]:
    """Rotate every `*.log` in `log_dir` larger than `max_bytes`. Returns the logs rotated."""
    rotated = []
    if not log_dir.is_dir():
        return rotated
    for log in sorted(log_dir.glob("*.log")):
        if not log.is_file() or log.stat().st_size <= max_bytes:
            continue
        for n in range(keep, 0, -1):
            older = log.with_name(f"{log.name}.{n}.gz")
            if not older.exists():
                continue
            if n == keep:
                older.unlink()
            else:
                older.replace(log.with_name(f"{log.name}.{n + 1}.gz"))
        with log.open("rb") as source, gzip.open(log.with_name(f"{log.name}.1.gz"), "wb") as target:
            shutil.copyfileobj(source, target)
        with log.open("r+b") as stream:
            stream.truncate(0)
        rotated.append(log)
    return rotated
