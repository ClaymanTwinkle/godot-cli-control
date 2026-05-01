"""Parse human-friendly duration strings (e.g. "30m", "2h", "90s") to seconds."""
from __future__ import annotations

import re

_PATTERN = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> int:
    """Return total seconds. Bare integer = seconds. 0 means disabled."""
    m = _PATTERN.match(text)
    if not m:
        raise ValueError(f"invalid duration {text!r} (use 30s / 30m / 2h / 0)")
    return int(m.group(1)) * _UNITS[m.group(2)]
