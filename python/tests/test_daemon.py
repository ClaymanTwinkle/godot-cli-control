"""daemon 单元测试 —— 不实际启动 Godot，专攻 PID 校验、port 探活、godot_bin 解析。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from godot_cli_control.daemon import (
    Daemon,
    DaemonError,
    _allocate_port,
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


def test_start_detaches_subprocess_signal_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon Popen 必须把 Godot 隔离到独立 signal group。

    否则 Ctrl+C 时 SIGINT 同时打到 Godot —— daemon 比 Python 父进程先死，
    finally 路径再 stop 已死的 PID 日志混乱。POSIX 用 start_new_session，
    Windows 用 CREATE_NEW_PROCESS_GROUP。
    """
    import sys as _sys

    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 999_999_997
        returncode = None

        def poll(self) -> None:
            return None

    def _record_popen(args: list[str], **kwargs: Any) -> _FakeProc:
        captured.update(kwargs)
        return _FakeProc()

    daemon = Daemon(tmp_path)
    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", _record_popen
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True
    )
    # 隔离全局注册表
    from godot_cli_control import registry as _reg
    monkeypatch.setattr(_reg, "_REGISTRY_DIR", tmp_path / "reg")

    daemon.start(port=29998)

    if _sys.platform == "win32":
        # Windows：CREATE_NEW_PROCESS_GROUP（值 0x200）必须在 creationflags
        import subprocess as _sp

        assert captured.get("creationflags", 0) & _sp.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured.get("start_new_session") is True, \
            "POSIX 必须 start_new_session 把 SIGINT 与父 group 隔离"


def test_ensure_imported_rebuilds_when_cache_missing_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已 import 项目场景：cache 在但不含 GameBridge → 必须重建。

    回归：fix #N（init 不刷新 global_script_class_cache 导致 autoload parse 失败）。
    """
    from godot_cli_control.daemon import _ensure_imported

    cache_dir = tmp_path / ".godot"
    cache_dir.mkdir()
    cache = cache_dir / "global_script_class_cache.cfg"
    # 模拟 init 之前编辑器生成的 cache：含别的 class，没有 GameBridge
    cache.write_text(
        'list=[{\n"base": &"Node",\n"class": &"SomeOtherClass",\n}]\n'
    )

    called: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        "godot_cli_control.daemon.reimport_project",
        lambda root, bin_: called.append((root, bin_)),
    )
    _ensure_imported(tmp_path, "/fake/godot")
    assert called == [(tmp_path, "/fake/godot")], \
        "cache 不含 GameBridge 时必须重新导入"


def test_ensure_imported_skips_when_cache_has_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from godot_cli_control.daemon import _ensure_imported

    cache_dir = tmp_path / ".godot"
    cache_dir.mkdir()
    cache = cache_dir / "global_script_class_cache.cfg"
    cache.write_text(
        'list=[{\n"base": &"Node",\n"class": &"GameBridge",\n}]\n'
    )

    called: list[tuple] = []
    monkeypatch.setattr(
        "godot_cli_control.daemon.reimport_project",
        lambda *a, **k: called.append(a),
    )
    _ensure_imported(tmp_path, "/fake/godot")
    assert called == [], "cache 含 GameBridge 时不该再重新导入"


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


# ---------------------------------------------------------------------------
# 以下补强：错误路径 / 跨平台 / 录制转码 —— 覆盖 daemon.py 错误分支
# ---------------------------------------------------------------------------


# ── 工具：构造一个能跑 start() happy path 的桩环境 ──


def _setup_start_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, port_ready: bool = True
) -> tuple[Daemon, Path]:
    """返回 (daemon, fake_bin)。已 patch find_godot_binary、_ensure_imported、Popen、sleep、_wait_port_ready。

    同时把 registry._REGISTRY_DIR 重定向到 tmp_path/reg，避免成功 start()
    的测试污染真实 ~/.local/state/godot-cli-control/daemons/。
    """
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    class _FakeProc:
        pid = 999_999_900
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
        "godot_cli_control.daemon._wait_port_ready",
        lambda *a, **k: port_ready,
    )
    # 隔离全局注册表：把写入重定向到临时目录，避免污染 ~/.local/state/…
    from godot_cli_control import registry as _reg
    monkeypatch.setattr(_reg, "_REGISTRY_DIR", tmp_path / "reg")
    return Daemon(tmp_path), fake_bin


# ── stale PID 恢复 ──


def test_start_recovers_from_stale_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上次崩溃残留的 PID（已死）不应阻塞 start —— is_running 必须返回 False。

    场景：用户上一次 daemon 被 kill -9，PID 文件留着；本次再 start，daemon 应
    照常启动，不抛 "already running"。
    """
    daemon, _ = _setup_start_env(tmp_path, monkeypatch)
    daemon.control_dir.mkdir(parents=True, exist_ok=True)
    daemon.pid_file.write_text("999999999")  # 死 PID

    pid = daemon.start(port=29991)
    assert pid == 999_999_900
    # 新 PID 已写入，覆盖了死 PID
    assert daemon.read_pid() == 999_999_900


# ── _terminate SIGTERM → SIGKILL 升级 ──


def test_terminate_escalates_to_sigkill_when_sigterm_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM 后进程不退出 → 必须升级到 SIGKILL，不能死等。"""
    import signal as _signal

    sent_signals: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        sent_signals.append((pid, sig))

    # 进程「永远活着」—— 模拟无视 SIGTERM 的卡死 Godot
    monkeypatch.setattr(
        "godot_cli_control.daemon._process_alive", lambda pid: True
    )
    monkeypatch.setattr("godot_cli_control.daemon.os.kill", _fake_kill)
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    # 让 deadline 一次循环就过 —— time.time 走两次（deadline 计算 + 第一次循环）
    times = iter([0.0, 100.0, 200.0, 300.0])
    monkeypatch.setattr(
        "godot_cli_control.daemon.time.time", lambda: next(times)
    )

    daemon = Daemon(tmp_path)
    daemon._terminate(pid=12345, timeout=10.0)

    # 至少一次 SIGTERM + 一次 SIGKILL（POSIX）/ SIGTERM-fallback（Windows）
    assert sent_signals[0] == (12345, _signal.SIGTERM)
    final_sig = sent_signals[-1][1]
    expected = getattr(_signal, "SIGKILL", _signal.SIGTERM)
    assert final_sig == expected, (
        f"卡死路径必须发 {expected!r}，实际 {final_sig!r}；信号序列={sent_signals}"
    )


def test_terminate_returns_early_when_process_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM 抛 ProcessLookupError → _terminate 直接返回，不再 SIGKILL。"""
    sent: list[int] = []

    def _fake_kill(pid: int, sig: int) -> None:
        sent.append(sig)
        raise ProcessLookupError

    monkeypatch.setattr("godot_cli_control.daemon.os.kill", _fake_kill)
    monkeypatch.setattr(
        "godot_cli_control.daemon._process_alive", lambda pid: False
    )

    Daemon(tmp_path)._terminate(pid=123, timeout=1.0)
    assert len(sent) == 1, "进程已不存在时不应再尝试第二次 kill"


# ── _process_is_godot 多平台矩阵 ──


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only ps 路径")
def test_process_is_godot_posix_match(monkeypatch: pytest.MonkeyPatch) -> None:
    from godot_cli_control.daemon import _process_is_godot

    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.check_output",
        lambda *a, **k: "Godot_v4.4-stable\n",
    )
    assert _process_is_godot(123) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only ps 路径")
def test_process_is_godot_posix_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    from godot_cli_control.daemon import _process_is_godot

    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.check_output",
        lambda *a, **k: "bash\n",
    )
    assert _process_is_godot(123) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only ps 路径")
def test_process_is_godot_returns_true_when_ps_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ps 不存在时放行（与原 bash 版宽松行为对齐）—— 不能因为查不到就拒杀。"""
    from godot_cli_control.daemon import _process_is_godot

    def _raise(*a: Any, **k: Any) -> str:
        raise FileNotFoundError("no ps")

    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.check_output", _raise
    )
    assert _process_is_godot(123) is True


def test_stop_refuses_when_pid_not_godot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PID 复用情景：现役进程名不像 Godot → 拒绝 SIGTERM 以免误杀别人的进程。"""
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.pid_file.write_text(str(os.getpid()))  # 当前 python 进程，肯定 alive
    monkeypatch.setattr(
        "godot_cli_control.daemon._process_alive", lambda pid: True
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._process_is_godot", lambda pid: False
    )
    with pytest.raises(DaemonError, match="不像 Godot"):
        daemon.stop()
    # PID 文件保留，让用户手动处理
    assert daemon.pid_file.exists()


# ── _transcode_movie 三态 ──


def test_transcode_skips_when_movie_missing(tmp_path: Path) -> None:
    """录制文件不存在 → 视为成功（没东西可转）。"""
    from godot_cli_control.daemon import _transcode_movie

    assert _transcode_movie(tmp_path / "nope.avi", tmp_path) is True


def test_transcode_keeps_original_when_ffmpeg_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg 不在 PATH → 保留原文件，返回 True（用户手动处理）。"""
    from godot_cli_control.daemon import _transcode_movie

    movie = tmp_path / "out.avi"
    movie.write_bytes(b"fake video data")
    monkeypatch.setattr(
        "godot_cli_control.daemon.shutil.which", lambda name: None
    )
    assert _transcode_movie(movie, tmp_path) is True
    assert movie.exists(), "ffmpeg 缺失时不能删原文件"


def test_transcode_success_deletes_original_returns_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg returncode 0 → 原 .avi 删除，.mp4 存在，返回 True。"""
    from godot_cli_control.daemon import _transcode_movie

    movie = tmp_path / "out.avi"
    movie.write_bytes(b"fake")
    monkeypatch.setattr(
        "godot_cli_control.daemon.shutil.which", lambda name: "/fake/ffmpeg"
    )

    class _Proc:
        returncode = 0

    def _fake_run(args: list[str], **kwargs: Any) -> _Proc:
        # ffmpeg 「成功」 —— 我们手动写出 mp4 模拟产物
        out_path = Path(args[args.index("-y") + 1])
        out_path.write_bytes(b"fake mp4")
        return _Proc()

    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.run", _fake_run
    )
    assert _transcode_movie(movie, tmp_path) is True
    assert not movie.exists(), "成功转码后应删原文件"
    assert (tmp_path / "out.mp4").exists()


def test_transcode_failure_keeps_original_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg returncode 非 0 → 原文件保留，log 写入，返回 False。"""
    from godot_cli_control.daemon import _transcode_movie

    movie = tmp_path / "out.avi"
    movie.write_bytes(b"corrupted")
    monkeypatch.setattr(
        "godot_cli_control.daemon.shutil.which", lambda name: "/fake/ffmpeg"
    )

    class _Proc:
        returncode = 1

    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.run", lambda *a, **k: _Proc()
    )
    assert _transcode_movie(movie, tmp_path) is False
    assert movie.exists(), "失败时必须保留原文件供调试"
    assert (tmp_path / "ffmpeg.log").exists(), "必须留 log 让用户看 ffmpeg 报错"


# ── start 录制路径（happy） ──


def test_start_record_writes_movie_path_file_and_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """record=True + movie_path → Popen args 含 --write-movie / --fixed-fps；
    movie_path_file 被写入；env GODOT_MOVIE_MAKER=1 注入。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    captured_args: dict[str, Any] = {}

    class _FakeProc:
        pid = 999_999_800
        returncode = None

        def poll(self) -> None:
            return None

    def _record_popen(args: list[str], **kwargs: Any) -> _FakeProc:
        captured_args["args"] = args
        captured_args["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", _record_popen
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True
    )
    # 隔离全局注册表
    from godot_cli_control import registry as _reg
    monkeypatch.setattr(_reg, "_REGISTRY_DIR", tmp_path / "reg")

    daemon = Daemon(tmp_path)
    movie = tmp_path / "rec.avi"
    daemon.start(record=True, movie_path=str(movie), fps=60, port=29994)

    assert "--write-movie" in captured_args["args"]
    idx = captured_args["args"].index("--write-movie")
    assert captured_args["args"][idx + 1] == str(movie)
    assert "--fixed-fps" in captured_args["args"]
    fps_idx = captured_args["args"].index("--fixed-fps")
    assert captured_args["args"][fps_idx + 1] == "60"
    assert captured_args["env"].get("GODOT_MOVIE_MAKER") == "1"
    assert daemon.movie_path_file.read_text().strip() == str(movie)


# ── start 立即崩溃捕获 ──


def test_start_detects_immediate_godot_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Godot launch 后 1s 内已退出（poll 返回非 None）→ DaemonError + 状态文件清理。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    class _DeadProc:
        pid = 999_999_700
        returncode = 137  # 模拟被 OOM kill

        def poll(self) -> int:
            return 137

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", lambda *a, **k: _DeadProc()
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)

    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="exited immediately"):
        daemon.start(port=29993)
    # 状态文件必须清理 —— 否则下次 start 会误以为有活的 daemon
    assert not daemon.pid_file.exists()
    assert not daemon.port_file.exists()


# ── _ensure_imported cache 不存在 ──


def test_ensure_imported_rebuilds_when_cache_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.godot/global_script_class_cache.cfg 完全不存在 → 必须重新导入。"""
    from godot_cli_control.daemon import _ensure_imported

    called: list = []
    monkeypatch.setattr(
        "godot_cli_control.daemon.reimport_project",
        lambda root, bin_: called.append((root, bin_)),
    )
    _ensure_imported(tmp_path, "/fake/godot")
    assert called == [(tmp_path, "/fake/godot")]


# ── _allocate_port ──


def test_allocate_port_zero_returns_os_assigned() -> None:
    port = _allocate_port(0)
    assert 1024 < port < 65536


def test_allocate_port_specific_returns_same() -> None:
    # Get a free port from the OS, release it, then ask for that exact port
    free = _allocate_port(0)
    assert _allocate_port(free) == free


def test_allocate_port_raises_when_occupied() -> None:
    import socket as _s
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    occupied = sock.getsockname()[1]
    try:
        with pytest.raises(DaemonError, match="already in use"):
            _allocate_port(occupied)
    finally:
        sock.close()


# ── stop 后录制转码失败 → 返回 2 ──


def test_stop_returns_2_when_transcode_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """录制转码失败但进程已停 → stop 返回 2，让 CI 能感知录制问题。"""
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.pid_file.write_text(str(os.getpid()))
    daemon.movie_path_file.write_text(str(tmp_path / "rec.avi"))
    (tmp_path / "rec.avi").write_bytes(b"data")

    monkeypatch.setattr(
        "godot_cli_control.daemon._process_alive", lambda pid: True
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._process_is_godot", lambda pid: True
    )
    monkeypatch.setattr(Daemon, "_terminate", lambda self, pid, **kw: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._transcode_movie", lambda *a, **k: False
    )

    assert daemon.stop() == 2
    # movie_path_file 已清理，下次 stop 不重复尝试
    assert not daemon.movie_path_file.exists()


def test_stop_returns_0_when_no_movie_to_transcode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """常规 stop（没开录制）→ 不调 _transcode_movie，返回 0。"""
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.pid_file.write_text(str(os.getpid()))

    transcode_called: list = []
    monkeypatch.setattr(
        "godot_cli_control.daemon._process_alive", lambda pid: True
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._process_is_godot", lambda pid: True
    )
    monkeypatch.setattr(Daemon, "_terminate", lambda self, pid, **kw: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._transcode_movie",
        lambda *a, **k: transcode_called.append(a) or True,
    )

    assert daemon.stop() == 0
    assert transcode_called == [], "无 movie_path_file 时不该调转码"


# ── _process_alive 边界 ──


def test_process_alive_zero_pid_is_dead() -> None:
    """PID 0 不是合法进程，必须返回 False（防 os.kill(0, 0) 误广播信号给整组）。"""
    assert _process_alive(0) is False


def test_process_alive_negative_pid_is_dead() -> None:
    assert _process_alive(-1) is False


# ── start 校验 godot_bin 可执行 ──


def test_start_rejects_when_port_already_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口已被别的进程监听 → daemon.start 立刻报错，不 spawn Godot。

    场景（issue #?）：旧 daemon 没 stop 干净 / 别的服务占了同端口。GameBridge
    listen 失败时只 printerr 不退进程，daemon 这头 create_connection 又会
    握手到错的进程上误报启动成功，后续 RPC 全打错。spawn 前先 bind 校验
    端口空闲，被占就直接报「port N already in use」。
    """
    import socket as _socket

    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    popen_called: list = []
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen",
        lambda *a, **k: popen_called.append(a) or (_ for _ in ()).throw(
            AssertionError("Popen 不该被调用 —— 端口校验应在此之前失败")
        ),
    )

    sock = _socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]
    try:
        daemon = Daemon(tmp_path)
        with pytest.raises(DaemonError, match="in use"):
            daemon.start(port=busy_port)
    finally:
        sock.close()

    assert popen_called == [], "端口被占时不应 spawn Godot"
    # 失败时不该污染 .cli_control 状态
    assert not daemon.pid_file.exists()
    assert not daemon.port_file.exists()


def test_start_rejects_non_executable_godot_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """find_godot_binary 返回的路径不可执行 → 立刻报错。"""
    _touch_godot_project(tmp_path)
    not_exec = tmp_path / "godot_text"
    not_exec.write_text("")  # 默认 0o644，无 X 位
    not_exec.chmod(0o644)
    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(not_exec)
    )
    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="not executable"):
        daemon.start()


# ---------------------------------------------------------------------------
# issue #38：godot stdout/stderr 捕获 + 启动失败时输出 log tail
# ---------------------------------------------------------------------------


def test_start_redirects_godot_output_to_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Popen 必须把 stdout 指向 godot.log、stderr 合并 —— 用户事后能 cat 日志。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 999_999_500
        returncode = None

        def poll(self) -> None:
            return None

    def _record_popen(args: list[str], **kwargs: Any) -> _FakeProc:
        captured["kwargs"] = kwargs
        # 模拟 Godot 写一行到日志再继续 —— 验证 fh 是真的 writable。
        kwargs["stdout"].write(b"hello from godot\n")
        return _FakeProc()

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", _record_popen
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True
    )
    # 隔离全局注册表
    from godot_cli_control import registry as _reg
    monkeypatch.setattr(_reg, "_REGISTRY_DIR", tmp_path / "reg")

    daemon = Daemon(tmp_path)
    daemon.start(port=29900)

    # 1) Popen 拿到的 stdout 是文件、stderr 合并到 stdout
    assert captured["kwargs"]["stderr"] == subprocess.STDOUT
    # 2) 文件写在约定位置
    assert daemon.log_file.exists()
    assert daemon.log_file.read_bytes() == b"hello from godot\n"


def test_start_immediate_crash_includes_log_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1s poll 抓到立即崩溃 → DaemonError 必须把 godot.log 末尾贴出来。

    issue #38：之前只报 "exited immediately (returncode=N)"，用户还得手动
    `cat .cli_control/godot.log`。现在直接拼到 message 里。
    """
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    class _DeadProc:
        pid = 999_999_400
        returncode = 1

        def poll(self) -> int:
            return 1

    def _spew_log(args: list[str], **kwargs: Any) -> _DeadProc:
        kwargs["stdout"].write(
            b"GODOT FATAL: autoload script not found at res://main.gd\n"
        )
        return _DeadProc()

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", _spew_log
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)

    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="autoload script not found") as ei:
        daemon.start(port=29901)
    assert "exited immediately" in str(ei.value)
    # log 文件保留供事后查阅
    assert daemon.log_file.exists()


def test_start_detects_crash_during_port_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口探活期间 Godot 自死 → 报「exited during launch」+ log tail，
    不再误报「GameBridge not ready within 30s」让用户怀疑端口冲突。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    # 首次 poll() 返回 None（1s 存活检查通过），之后变非 None（探活时崩溃）。
    class _LateCrashProc:
        pid = 999_999_300
        returncode = 11
        _polls = 0

        def poll(self) -> int | None:
            self._polls += 1
            return None if self._polls == 1 else 11

    def _make_proc(args: list[str], **kwargs: Any) -> _LateCrashProc:
        kwargs["stdout"].write(b"ERROR: scene main.tscn referenced missing res\n")
        return _LateCrashProc()

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", _make_proc
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(Daemon, "_terminate", lambda self, pid, **kw: None)

    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="exited during launch") as ei:
        daemon.start(port=29902, wait_seconds=2)
    assert "scene main.tscn" in str(ei.value)


def test_start_port_timeout_includes_log_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Godot 仍在跑但 GameBridge 超时未就绪 → message 也带 log tail，便于诊断
    （比如 GameBridge autoload 没启用、端口被占等情况）。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    class _LiveProc:
        pid = 999_999_200
        returncode = None

        def poll(self) -> None:
            return None

    def _make_proc(args: list[str], **kwargs: Any) -> _LiveProc:
        # 通过子进程的 stdout fh 写日志：这样可以躲过 start() 自己的 truncate。
        kwargs["stdout"].write(b"WARN: no GameBridge autoload registered\n")
        return _LiveProc()

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", _make_proc
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._wait_port_ready", lambda *a, **k: False
    )
    monkeypatch.setattr(Daemon, "_terminate", lambda self, pid, **kw: None)

    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="not ready") as ei:
        daemon.start(port=29903, wait_seconds=1)
    assert "no GameBridge autoload registered" in str(ei.value)


def test_format_log_tail_truncates_long_logs(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir(parents=True, exist_ok=True)
    daemon.log_file.write_text("\n".join(f"line{i}" for i in range(100)))
    out = daemon._format_log_tail(n=5)
    assert "line99" in out
    assert "line95" in out
    assert "line0" not in out, "末尾 5 行不应该包含开头"
    assert out.startswith("Godot 日志（")


def test_format_log_tail_handles_missing_log(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    out = daemon._format_log_tail()
    assert "log unavailable" in out


def test_wait_port_ready_returns_false_when_proc_died(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口未开 + proc.poll() 已返回非 None → 立刻 False，不再等到 deadline。"""
    class _DeadProc:
        def poll(self) -> int:
            return 137

    monkeypatch.setattr(
        "godot_cli_control.daemon.time.sleep", lambda *_: None
    )
    # 必须秒返：max_seconds=10 但因 proc 死了第一轮就跳出
    assert _wait_port_ready(port=1, max_seconds=10, proc=_DeadProc()) is False


# ── last_exit_code 持久化 ──


def test_start_records_exit_code_on_immediate_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1s poll 抓到崩溃 → returncode 必须落盘到 last_exit_code 文件。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    class _DeadProc:
        pid = 999_999_100
        returncode = 137

        def poll(self) -> int:
            return 137

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen",
        lambda *a, **k: _DeadProc(),
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)

    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="exited immediately"):
        daemon.start(port=29904)
    assert daemon.read_last_exit_code() == 137


def test_start_records_exit_code_on_port_wait_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探活期间 Godot 自死 → returncode 也必须落盘。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    class _LateCrashProc:
        pid = 999_999_050
        _polls = 0

        def poll(self) -> int | None:
            self._polls += 1
            return None if self._polls == 1 else 11

        @property
        def returncode(self) -> int | None:
            return None if self._polls == 0 else 11

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen",
        lambda *a, **k: _LateCrashProc(),
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(Daemon, "_terminate", lambda self, pid, **kw: None)

    daemon = Daemon(tmp_path)
    with pytest.raises(DaemonError, match="exited during launch"):
        daemon.start(port=29905, wait_seconds=2)
    assert daemon.read_last_exit_code() == 11


def test_start_clears_stale_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新一轮 start 必须清掉上次的 last_exit_code，否则 status 永远显示陈旧值。"""
    daemon, _ = _setup_start_env(tmp_path, monkeypatch)
    daemon.control_dir.mkdir(parents=True, exist_ok=True)
    daemon.exit_code_file.write_text("99")  # 上一轮残留

    daemon.start(port=29906)
    assert daemon.read_last_exit_code() is None, \
        "新一轮 start 必须 invalidate 旧 exit code"


def test_read_last_exit_code_handles_garbage(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    daemon.control_dir.mkdir()
    daemon.exit_code_file.write_text("not a number")
    assert daemon.read_last_exit_code() is None


def test_read_last_exit_code_none_when_missing(tmp_path: Path) -> None:
    daemon = Daemon(tmp_path)
    assert daemon.read_last_exit_code() is None


def test_start_with_port_zero_writes_actual_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """port=0 时 .cli_control/port 落盘的应是 OS 分配的实际端口，不是 0。"""
    daemon, _ = _setup_start_env(tmp_path, monkeypatch)
    daemon.start(port=0)
    written = int(daemon.port_file.read_text().strip())
    assert written != 0
    assert 1024 < written < 65536


def test_start_passes_idle_timeout_to_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon.start(idle_timeout=10) 必须把 --game-bridge-idle-timeout=10 加到 Popen argv。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 999_999_010
        returncode = None

        def poll(self) -> None:
            return None

    def _record_popen(args: list[str], **kwargs: Any) -> _FakeProc:
        captured["args"] = args
        return _FakeProc()

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", _record_popen
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True
    )
    from godot_cli_control import registry as _reg
    monkeypatch.setattr(_reg, "_REGISTRY_DIR", tmp_path / "reg")

    daemon = Daemon(tmp_path)
    daemon.start(port=0, idle_timeout=10)
    assert any(a == "--game-bridge-idle-timeout=10" for a in captured["args"])


def test_start_omits_idle_timeout_when_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """idle_timeout=0 (默认) 时 argv 里不该有该 flag —— 保持旧行为。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 999_999_020
        returncode = None

        def poll(self) -> None:
            return None

    def _record_popen(args: list[str], **kwargs: Any) -> _FakeProc:
        captured["args"] = args
        return _FakeProc()

    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin)
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon._ensure_imported", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "godot_cli_control.daemon.subprocess.Popen", _record_popen
    )
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True
    )
    from godot_cli_control import registry as _reg
    monkeypatch.setattr(_reg, "_REGISTRY_DIR", tmp_path / "reg")

    daemon = Daemon(tmp_path)
    daemon.start(port=0)  # 不传 idle_timeout 用默认
    assert not any(a.startswith("--game-bridge-idle-timeout=") for a in captured["args"])


def test_start_registers_in_global_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() 成功后必须写全局注册表 JSON；stop() 必须删除该 JSON。

    直接断言 registry 目录下的文件存在 / 不存在，比走 list_all() 更严：
    list_all() 自带死 PID 剪枝，会让"忘记 unregister"的 bug 偷偷过去。
    """
    from godot_cli_control import registry

    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    daemon, _ = _setup_start_env(tmp_path, monkeypatch)
    record_file = tmp_path / "reg" / f"{registry.project_hash(tmp_path)}.json"

    daemon.start(port=0)
    assert record_file.exists(), "start() 后注册表 JSON 应存在"

    daemon.stop()
    assert not record_file.exists(), "stop() 后注册表 JSON 应被 unregister 删掉"
