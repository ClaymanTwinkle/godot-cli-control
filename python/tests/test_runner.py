"""单元测试：godot_cli_control.runner 入口。

策略：monkeypatch ``sys.argv`` + 替换 ``runner._exec_user_script`` 为记录探针，
测 argv 解析、错误退出、port 透传，不真起 daemon。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from godot_cli_control import runner
from godot_cli_control.client import DEFAULT_PORT


class _RecordingExec:
    """记录 _exec_user_script 调用：(script_path, port) → 预置返回码。"""

    def __init__(self, return_code: int = 0) -> None:
        self.calls: list[tuple[Path, int]] = []
        self.return_code = return_code

    def __call__(self, script_path: Path, port: int) -> int:
        self.calls.append((script_path, port))
        return self.return_code


def _patch_argv_and_exec(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    return_code: int = 0,
) -> _RecordingExec:
    monkeypatch.setattr("sys.argv", argv)
    rec = _RecordingExec(return_code=return_code)
    monkeypatch.setattr(runner, "_exec_user_script", rec)
    return rec


# ── 错误路径 ──


def test_no_script_arg_prints_usage_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_argv_and_exec(monkeypatch, ["runner"])
    with pytest.raises(SystemExit) as exc_info:
        runner.main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "用法" in captured.err


def test_missing_script_file_prints_error_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    bad = tmp_path / "nonexistent.py"
    _patch_argv_and_exec(monkeypatch, ["runner", str(bad)])
    with pytest.raises(SystemExit) as exc_info:
        runner.main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "找不到" in captured.err


def test_port_flag_without_value_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    script = tmp_path / "s.py"
    script.write_text("def run(b): pass\n")
    _patch_argv_and_exec(monkeypatch, ["runner", str(script), "--port"])
    with pytest.raises(SystemExit) as exc_info:
        runner.main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "需要一个值" in captured.err


def test_port_flag_with_non_int_value_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    script = tmp_path / "s.py"
    script.write_text("def run(b): pass\n")
    _patch_argv_and_exec(
        monkeypatch, ["runner", str(script), "--port", "abc"]
    )
    with pytest.raises(SystemExit) as exc_info:
        runner.main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "必须是整数" in captured.err
    # 错误消息应包含原始非法值，便于调试
    assert "'abc'" in captured.err or "abc" in captured.err


# ── 正常路径 ──


def test_default_port_used_when_no_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "s.py"
    script.write_text("def run(b): pass\n")
    rec = _patch_argv_and_exec(monkeypatch, ["runner", str(script)])
    with pytest.raises(SystemExit) as exc_info:
        runner.main()
    assert exc_info.value.code == 0
    assert len(rec.calls) == 1
    called_path, called_port = rec.calls[0]
    assert called_path == script
    assert called_port == DEFAULT_PORT


def test_explicit_port_passed_to_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "s.py"
    script.write_text("def run(b): pass\n")
    rec = _patch_argv_and_exec(
        monkeypatch, ["runner", str(script), "--port", "12345"]
    )
    with pytest.raises(SystemExit) as exc_info:
        runner.main()
    assert exc_info.value.code == 0
    assert rec.calls[0][1] == 12345


def test_exec_nonzero_return_propagates_as_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_exec_user_script 返回非 0 → sys.exit 透传该码（用户脚本失败信号）。"""
    script = tmp_path / "s.py"
    script.write_text("def run(b): pass\n")
    _patch_argv_and_exec(monkeypatch, ["runner", str(script)], return_code=7)
    with pytest.raises(SystemExit) as exc_info:
        runner.main()
    assert exc_info.value.code == 7


def test_port_flag_can_appear_before_or_after_other_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--port 出现位置不影响解析（只要在 script 之后）。"""
    script = tmp_path / "s.py"
    script.write_text("def run(b): pass\n")
    rec = _patch_argv_and_exec(
        monkeypatch, ["runner", str(script), "--port", "9999"]
    )
    with pytest.raises(SystemExit):
        runner.main()
    assert rec.calls[0][1] == 9999
