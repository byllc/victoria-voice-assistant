from __future__ import annotations

import re

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}
_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90, "couple": 2, "few": 3,
}
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-z]+")


def parse_duration(text: str | None) -> float | None:
    """Parse a spoken duration ("10 minutes", "an hour and a half", "1:30") into seconds.

    Returns None when no duration can be found. A leading minus or "minus" makes it negative.
    """
    if not text:
        return None
    lowered = text.strip().lower()
    sign = -1 if lowered.startswith(("-", "minus ")) else 1
    lowered = lowered.lstrip("-").removeprefix("minus ").replace("-", " ").strip()

    clock = re.fullmatch(r"(\d+):(\d{2})(?::(\d{2}))?", lowered)
    if clock:
        first, second, third = clock.groups()
        if third is not None:
            return sign * (int(first) * 3600 + int(second) * 60 + int(third))
        return sign * (int(first) * 60 + int(second))

    total = 0.0
    found = False
    pending: float | None = None  # number waiting for its unit
    article = False  # pending came from "a"/"an"
    last_unit = 0
    for token in _TOKEN.findall(lowered):
        if token == "half":
            if last_unit and (pending is None or article):  # "an hour and a half"
                total += 0.5 * last_unit
                found = True
                pending, article = None, False
            elif pending is not None:  # "one and a half hours"
                pending += 0.5
            else:  # "half an hour"
                pending = 0.5
            continue
        if token == "quarter":
            pending = 0.25
            continue
        if token in _UNITS:
            total += (1.0 if pending is None else pending) * _UNITS[token]
            last_unit = _UNITS[token]
            pending, article, found = None, False, True
            continue
        if token in ("a", "an"):
            if pending is None:  # "half an hour" keeps its 0.5
                pending, article = 1.0, True
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            value = float(token)
        elif token in _NUMBERS:
            value = float(_NUMBERS[token])
        else:
            continue
        if pending is not None and not article and pending >= 20 and value < 10:
            pending += value  # "twenty five"
        else:
            pending = value
        article = False
    if pending is not None and not found and not article:
        total += pending * 60  # a bare number means minutes
        found = True
    return sign * total if found and total > 0 else None


def speak_duration(seconds: float) -> str:
    """Short spoken form: "1 hour 5 minutes", "4 minutes 30 seconds", "45 seconds"."""
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts) if parts else "0 seconds"


def duration_label(seconds: float) -> str:
    """Name for an unnamed timer: "10-minute", "1-hour 30-minute", "45-second"."""
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}-hour")
    if minutes:
        parts.append(f"{minutes}-minute")
    if secs and not hours:
        parts.append(f"{secs}-second")
    return " ".join(parts) or "0-second"
