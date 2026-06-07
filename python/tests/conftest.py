"""pytest 共享 fixtures —— 跨测试模块复用（DRY）。"""
from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.fixture
def dead_pid() -> int:
    """一个 100% 已死、跨平台稳定的 PID。

    起一个立即退出的子进程并 reap（wait）掉，其 PID 此后保证不存活——
    比硬编码 magic PID（如 2_000_000）稳：后者依赖 OS 的 PID 上限，
    Linux pid_max=4194304 下长跑系统理论上可能真有进程占到该 PID 而 flaky。
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid
