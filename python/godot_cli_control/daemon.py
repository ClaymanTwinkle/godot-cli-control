"""Daemon 进程管理 —— 移植自 addons/.../bin/run_cli_control.sh。

提供跨平台的 Godot 进程启停、端口探活、PID 校验、录制转码。
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .client import DEFAULT_PORT


class DaemonError(RuntimeError):
    """Daemon 启停过程中可恢复的错误（用户应看到 message，不必看 traceback）。"""


class Daemon:
    """管理本地 Godot 实例的状态文件 + 进程。

    所有状态写到 ``project_root/.cli_control/``：``godot.pid`` ``port``
    ``movie_path``。文件名/语义与原 bash 版一致，使两者可互换。
    """

    def __init__(self, project_root: Path, control_dir: Path | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.control_dir = control_dir or (self.project_root / ".cli_control")
        self.pid_file = self.control_dir / "godot.pid"
        self.port_file = self.control_dir / "port"
        self.movie_path_file = self.control_dir / "movie_path"
        # Godot 进程的 stdout+stderr。每次 start 时 truncate 重写，stop 后保留
        # 让用户回溯最近一次启动失败的根因（issue #38）。
        self.log_file = self.control_dir / "godot.log"

    # ── 状态查询 ──

    def read_pid(self) -> int | None:
        if not self.pid_file.exists():
            return None
        try:
            return int(self.pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def is_running(self) -> bool:
        pid = self.read_pid()
        return pid is not None and _process_alive(pid)

    def current_port(self) -> int | None:
        if not self.port_file.exists():
            return None
        try:
            return int(self.port_file.read_text().strip())
        except (ValueError, OSError):
            return None

    # ── 启动 ──

    def start(
        self,
        *,
        godot_bin: str | None = None,
        record: bool = False,
        movie_path: str | None = None,
        headless: bool = False,
        fps: int = 30,
        port: int = DEFAULT_PORT,
        wait_seconds: int = 30,
    ) -> int:
        """启动 Godot daemon，等端口就绪后返回 PID。"""
        # 项目根校验：拒绝在非 Godot 项目目录跑，避免 Godot 用 --path .
        # 静默创建假项目 + 30s port 探活 timeout 这种神秘失败。
        if not (self.project_root / "project.godot").is_file():
            raise DaemonError(
                f"{self.project_root} 不是 Godot 项目根（缺 project.godot）。"
                f" 请 cd 到 Godot 项目根后再跑，或先 `godot-cli-control init`。"
            )
        if self.is_running():
            raise DaemonError(
                f"Godot already running (PID {self.read_pid()}); stop first"
            )
        if record and not movie_path:
            raise DaemonError("--record requires --movie-path")

        bin_path = (
            godot_bin
            or self.read_godot_bin_pref()
            or find_godot_binary()
        )
        if not bin_path:
            raise DaemonError(
                "Godot binary not found. Set GODOT_BIN env var or run "
                "'godot-cli-control init' to detect & save it."
            )
        if not os.access(bin_path, os.X_OK):
            raise DaemonError(f"Godot binary not executable: {bin_path}")

        # 先确保 import 缓存就位（避免首次启动 GameBridge 来不及绑端口）
        _ensure_imported(self.project_root, bin_path)

        self.control_dir.mkdir(parents=True, exist_ok=True)
        # PID/port 仅本用户可读（Windows 上 chmod 是 no-op）
        try:
            os.chmod(self.control_dir, 0o700)
        except OSError:
            pass

        # 清掉上次崩溃残留的 movie_path —— 否则若用户上次 daemon 被 kill -9
        # 没走 stop，旧路径会留在文件里，本次 stop 流程触发针对错误文件的
        # ffmpeg 转码（多半失败但也可能转个旧 .avi 误导用户）。
        self.movie_path_file.unlink(missing_ok=True)

        self.port_file.write_text(str(port))

        args: list[str] = [
            bin_path,
            "--path",
            str(self.project_root),
            "--cli-control",
            f"--game-bridge-port={port}",
        ]
        if headless:
            args.append("--headless")

        env = os.environ.copy()
        if record:
            args += ["--write-movie", movie_path, "--fixed-fps", str(fps)]
            env["GODOT_MOVIE_MAKER"] = "1"
            self.movie_path_file.write_text(movie_path)
            print(f"录制模式：{movie_path} ({fps}fps)", file=sys.stderr)

        # detach 子进程：避免 Ctrl+C 时 SIGINT 同时打到 Godot（父进程会先死，
        # finally 路径再 stop 已死的 PID 日志混乱）。POSIX 用新 session 隔离
        # signal group，Windows 用 CREATE_NEW_PROCESS_GROUP。
        popen_kwargs: dict[str, Any] = {"env": env}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        # 把 Godot 的 stdout+stderr 落到 .cli_control/godot.log。truncate 重写
        # 让 log 始终对应最近一次启动；子进程 dup 之后这边的 fh 可以立即关闭，
        # daemon 不必长期持有 fd。issue #38。
        log_fh = open(self.log_file, "wb")
        try:
            popen_kwargs["stdout"] = log_fh
            popen_kwargs["stderr"] = subprocess.STDOUT
            proc = subprocess.Popen(args, **popen_kwargs)
        finally:
            log_fh.close()
        self.pid_file.write_text(str(proc.pid))

        # 启动后 1s 仍存活才认为 launch 成功（捕获 --cli-control 拒识/参数错误）
        time.sleep(1)
        if proc.poll() is not None:
            self._cleanup_state_files()
            raise DaemonError(
                f"Godot exited immediately after launch (returncode={proc.returncode}).\n"
                f"{self._format_log_tail()}"
            )

        print(f"等待 GameBridge 就绪 (port {port})...", file=sys.stderr)
        if not _wait_port_ready(port, wait_seconds, proc=proc):
            crashed_rc = proc.poll()
            self._terminate(proc.pid)
            self._cleanup_state_files()
            if crashed_rc is not None:
                # 端口探活期间 Godot 自己挂了（autoload 报错 / scene load 失败 等）。
                # 这是 issue #38 报告的"daemon start 报成功后 RPC 全失败"主线场景。
                raise DaemonError(
                    f"Godot exited during launch (returncode={crashed_rc}).\n"
                    f"{self._format_log_tail()}"
                )
            raise DaemonError(
                f"GameBridge not ready on port {port} within {wait_seconds}s.\n"
                f"{self._format_log_tail()}"
            )

        print(f"Godot 已启动 (PID {proc.pid})", file=sys.stderr)
        return proc.pid

    # ── 停止 ──

    def stop(self) -> int:
        """停止 daemon。返回 exit code（0 成功；2 转码失败但进程已停）。"""
        pid = self.read_pid()
        if pid is None:
            print("没有 PID 文件，无需停止", file=sys.stderr)
            return 0
        if not _process_alive(pid):
            print(f"Godot (PID {pid}) 已不在运行，清理 PID 文件", file=sys.stderr)
            self._cleanup_state_files()
            return 0
        if not _process_is_godot(pid):
            raise DaemonError(
                f"PID {pid} 进程名不像 Godot；拒绝 SIGTERM 以免误杀。"
                f" 如需清理，手动 kill 该 PID 并删 {self.pid_file}"
            )

        print(f"关闭 Godot (PID {pid})...", file=sys.stderr)
        self._terminate(pid)
        self._cleanup_state_files()
        print("Godot 已停止", file=sys.stderr)

        # 录制转码（即使失败也认为 stop 成功；返回非 0 让 CI 感知）
        if self.movie_path_file.exists():
            movie_path = Path(self.movie_path_file.read_text().strip())
            self.movie_path_file.unlink(missing_ok=True)
            if not _transcode_movie(movie_path, self.control_dir):
                return 2
        return 0

    # ── 内部 ──

    def _cleanup_state_files(self) -> None:
        """清理 daemon 启停产生的 pid/port 临时文件。

        port_file 跟随 pid_file 一起清，避免后续 ``current_port()`` 读到 stale
        值把 RPC 请求发到已不存在的端口。``movie_path`` 在 stop 流程中独立处理。
        """
        self.pid_file.unlink(missing_ok=True)
        self.port_file.unlink(missing_ok=True)

    def read_godot_bin_pref(self) -> str | None:
        """读取 init 命令写入的 ``.cli_control/godot_bin`` 路径偏好。"""
        pref = self.control_dir / "godot_bin"
        if not pref.exists():
            return None
        try:
            value = pref.read_text().strip()
        except OSError:
            return None
        if value and Path(value).is_file() and os.access(value, os.X_OK):
            return value
        return None

    def _format_log_tail(self, n: int = 30) -> str:
        """渲染 godot.log 末尾若干行，给启动失败的 DaemonError 拼可读上下文。

        失败兜底成 ``(log unavailable)`` 而不是抛异常 —— 启动错误本身比 log
        读取错误更重要，不能让 IO 失败盖掉真正的根因。
        """
        try:
            text = self.log_file.read_text(errors="replace")
        except OSError:
            return f"(log unavailable: {self.log_file})"
        lines = text.splitlines()
        header = f"Godot 日志（{self.log_file}）末尾 {n} 行："
        if not lines:
            return f"{header}\n  (空 — Godot 进程未输出任何内容)"
        if len(lines) <= n:
            body = text.rstrip()
        else:
            body = "...\n" + "\n".join(lines[-n:])
        return f"{header}\n{body}"

    def _terminate(self, pid: int, timeout: float = 10.0) -> None:
        """SIGTERM → wait → SIGKILL。"""
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _process_alive(pid):
                return
            time.sleep(0.5)
        print("SIGTERM 超时，强制终止", file=sys.stderr)
        try:
            os.kill(pid, _force_kill_signal())
        except (ProcessLookupError, OSError):
            pass


# ── Godot 二进制发现 ──


def find_godot_binary() -> str | None:
    """查找可用的 Godot 二进制。

    优先级：``GODOT_BIN`` env > macOS /Applications > PATH > Windows Program Files。
    """
    env_bin = os.environ.get("GODOT_BIN")
    if env_bin and Path(env_bin).is_file() and os.access(env_bin, os.X_OK):
        return env_bin

    if sys.platform == "darwin":
        # macOS 默认安装位置 + 同目录所有 Godot*.app
        for app in sorted(Path("/Applications").glob("Godot*.app")):
            candidate = app / "Contents" / "MacOS" / "Godot"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

    for name in ("godot4", "godot", "Godot"):
        p = shutil.which(name)
        if p:
            return p

    if sys.platform == "win32":
        for base in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        ):
            if base.exists():
                for exe in base.glob("Godot*/Godot*.exe"):
                    return str(exe)
    return None


# ── 进程辅助 ──


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_is_godot(pid: int) -> bool:
    """软校验：进程名包含 'godot'（防 PID 复用误杀）。

    无法检查（找不到 ps/tasklist）时**放行**，与原 bash 版宽松行为一致。
    """
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["tasklist", "/fi", f"PID eq {pid}", "/nh"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return True
        return "godot" in out.lower()

    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "comm="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return True
    return "godot" in out.lower()


def _force_kill_signal() -> int:
    return getattr(signal, "SIGKILL", signal.SIGTERM)


def _wait_port_ready(
    port: int,
    max_seconds: int,
    proc: subprocess.Popen[Any] | None = None,
) -> bool:
    """轮询直到端口可连接或超时。

    若传入 ``proc``，每轮先检查它是否已退出 —— Godot 在 GameBridge 起来前自己
    挂了（autoload 报错、--cli-control 接错……）就立刻返回 False，不让用户干等
    剩下的 wait_seconds。issue #38。
    """
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(1)
    return False


_CLASS_CACHE_PATH = Path(".godot") / "global_script_class_cache.cfg"
# autoload 入口 class_name；只检它一个，新增内部 class 不必改这里
_REQUIRED_CLASS_MARKER = b'&"GameBridge"'


def reimport_project(project_root: Path, godot_bin: str) -> None:
    """跑一次 ``--headless --editor --quit`` 重建 ``.godot/`` 缓存。

    stderr 透传到当前进程而非吞掉：项目损坏 / Godot 版本不兼容时用户能看到
    Godot 自己的报错，比之后 GameBridge 30s 端口探活 timeout 容易诊断。
    """
    print("导入项目资源（--editor --quit）...", file=sys.stderr)
    proc = subprocess.run(
        [
            godot_bin,
            "--headless",
            "--editor",
            "--quit",
            "--path",
            str(project_root),
        ],
        stdout=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"警告：Godot 资源导入返回 {proc.returncode}，"
            f"daemon 启动可能因此失败（看上方 Godot 错误输出）",
            file=sys.stderr,
        )


def _ensure_imported(project_root: Path, godot_bin: str) -> None:
    """daemon 启动兜底：cache 不存在 **或** 不含 GameBridge 时重建。

    后者覆盖：用户没跑 init 直接复制 addon、init 之后又被外部工具改坏 cache、
    或将来 init 自身的重新导入步骤失败。只检 GameBridge 一个 class_name，
    它是 autoload 入口，缺它必挂；其它内部 class 不必感知。
    """
    cache = project_root / _CLASS_CACHE_PATH
    if cache.exists() and _REQUIRED_CLASS_MARKER in cache.read_bytes():
        return
    reimport_project(project_root, godot_bin)


def _transcode_movie(movie_path: Path, control_dir: Path) -> bool:
    """录制结束后把 Godot 输出（avi/png）转成 mp4。失败保留原文件。"""
    if not movie_path.exists():
        return True
    if not shutil.which("ffmpeg"):
        print(
            f"未找到 ffmpeg，保留原始文件：{movie_path}",
            file=sys.stderr,
        )
        return True
    mp4 = movie_path.with_suffix(".mp4")
    log = control_dir / "ffmpeg.log"
    print(f"正在转码：{movie_path} → {mp4} ...", file=sys.stderr)
    with open(log, "wb") as fh:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(movie_path),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(mp4),
            ],
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    if proc.returncode == 0:
        movie_path.unlink(missing_ok=True)
        print(
            f"转码完成：{mp4} ({mp4.stat().st_size} bytes)",
            file=sys.stderr,
        )
        return True
    print(
        f"转码失败，保留原始文件：{movie_path}（日志：{log}）",
        file=sys.stderr,
    )
    return False
