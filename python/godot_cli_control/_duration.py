"""Parse human-friendly duration strings (e.g. "30m", "2h", "90s") to seconds."""
from __future__ import annotations

import re

# 不允许数字与单位之间留空格 —— 与帮助文本「30m / 2h / 90s」保持一致，避免
# 既文档教 30m 又静默接受 "30 m" 这种半通不通的输入。两端 \s* 仍保留，方便
# 用户从 shell 复制带尾换行的字串。
_PATTERN = re.compile(r"^\s*(\d+)([smh]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> int:
    """Return total seconds. Bare integer = seconds. 0 means disabled."""
    m = _PATTERN.match(text)
    if not m:
        raise ValueError(f"invalid duration {text!r} (use 30s / 30m / 2h / 0)")
    return int(m.group(1)) * _UNITS[m.group(2)]
