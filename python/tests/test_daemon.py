"""daemon 单元测试 —— 不实际启动 Godot，专攻 PID 校验、port 探活、godot_bin 解析。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from godot_cli_control.daemon import (
    Daemon,
    DaemonError,
    _process_alive,
    _wait_port_ready,
)


def test_is_running_false_when_no_pid_file(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    assert daemon.is_running() is False
    assert daemon.read_pid() is None


def test_is_running_false_when_pid_dead(tmp_path: Path) -> None:
    """一个永远不会存在的 PID（极大数）应被识别为非存活。"""
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.pid_file.write_text("999999999")  # 极大概率不存在
    assert _process_alive(999999999) is False
    assert daemon.is_running() is False


def test_current_port_reads_file(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.port_file.write_text("12345\n")
    assert daemon.current_port() == 12345


def test_current_port_none_when_garbage(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.port_file.write_text("not a port\n")
    assert daemon.current_port() is None


def _touch_godot_project(root: Path) -> None:
    """让 daemon.start 的项目根校验通过 —— 测试不需要真 Godot 工程内容。"""
    (root / "project.godot").write_text("config_version=5\n")


def test_start_rejects_when_not_godot_project_dir(tmp_path: Path) -> None:
    """没有 project.godot 时直接报错，不尝试 spawn Godot 浪费 30s timeout。"""
    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="不是 Godot 项目根"):
        daemon.start()


def test_start_rejects_record_without_movie_path(tmp_path: Path) -> None:
    _touch_godot_project(tmp_path)
    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="movie-path"):
        daemon.start(record=True, movie_path=None)


def test_start_rejects_when_godot_bin_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: None
    )
    _touch_godot_project(tmp_path)
    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="not found"):
        daemon.start()


def test_read_godot_bin_pref_file(tmp_path: Path) -> None:
    """init 写的 .cli_control/godot_bin 优先于自动检测。"""
    fake_bin = tmp_path / "godot_fake"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    project = tmp_path / "proj"
    project.mkdir()
    daemon = Daemon(project)
    daemon.control_dir.mkdir()
    (daemon.control_dir / "godot_bin").write_text(str(fake_bin) + "\n")

    assert daemon.read_godot_bin_pref() == str(fake_bin)


def test_read_godot_bin_pref_ignores_missing_file(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    assert daemon.read_godot_bin_pref() is None


def test_read_godot_bin_pref_ignores_dead_path(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    (daemon.control_dir / "godot_bin").write_text("/not/real/godot\n")
    assert daemon.read_godot_bin_pref() is None


def test_stop_handles_missing_pid_file(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    assert daemon.stop() == 0  # noop


def test_stop_cleans_dead_pid_file(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.pid_file.write_text("999999999")
    assert daemon.stop() == 0
    assert not daemon.pid_file.exists()


def test_stop_cleans_port_file_when_pid_dead(tmp_path: Path) -> None:
    """死 PID 路径也清 port_file —— 避免 stale port 把 RPC 引向错误端点。"""
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.pid_file.write_text("999999999")
    daemon.port_file.write_text("12345")
    assert daemon.stop() == 0
    assert not daemon.pid_file.exists()
    assert not daemon.port_file.exists()


def test_start_clears_stale_movie_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上次 daemon 崩溃残留的 movie_path_file 必须在 start 时清掉。

    否则本轮（哪怕没开 --record）走 stop 流程会读到旧路径并尝试 ffmpeg 转码，
    多半失败但也可能误转个旧 .avi 让用户以为本次录制成功。
    """
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir(parents=True, exist_ok=True)
    daemon.movie_path_file.write_text("/old/run/leftover.avi")

    class _FakeProc:
        pid = 999_999_998
        returncode = None

        def poll(self) -> None:
            return None

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", lambda *a, **k: _FakeProc()
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._wait_port_ready", lambda *a, **k: False
    )
    monkeypatch.setattr(Daemon, "_terminate", lambda self, pid, **kw: None)

    with pytest.raises(DaemonError, match="not ready"):
        daemon.start(port=29999)

    assert not daemon.movie_path_file.exists(), \
        "stale movie_path 必须在 start 时被清掉"


def test_wait_port_ready_returns_false_for_unbound_port() -> None:
    # 一个几乎肯定没监听的端口；max_seconds=1 让测试快返回
    assert _wait_port_ready(port=1, max_seconds=1) is False


def test_wait_port_ready_returns_true_when_listening() -> None:
    """开一个 ephemeral 端口，daemon 探活应 1s 内返回。"""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert _wait_port_ready(port=port, max_seconds=2) is True
    finally:
        sock.close()
