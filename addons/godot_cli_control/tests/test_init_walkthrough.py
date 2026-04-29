#!/usr/bin/env python3
"""Init walkthrough（跨平台）：验证 ``godot-cli-control init`` 一键接入 +
daemon 启停的纯 Python 路径。等价于 test_init_walkthrough.sh，但在 Linux /
macOS / Windows 上都能直接跑（CI matrix 用它复用 Windows runner）。

步骤：mktemp 项目（不带 plugin、不写 [autoload]/[editor_plugins]）→
      pip install → init → daemon start → tree → daemon stop

Usage:
    python test_init_walkthrough.py                  # 自动找 Godot
    GODOT_BIN=/path/to/godot python test_init_walkthrough.py
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows Python 默认 stdout 走 cp1252，print 中文路径 / 提示会触发
# UnicodeEncodeError。Linux / macOS 默认 utf-8，reconfigure 是 no-op。
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
        stream.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[3]

PROJECT_GODOT = """\
config_version=5

[application]
config/name="init-walkthrough"
config/features=PackedStringArray("4.4", "GL Compatibility")
run/main_scene="res://main.tscn"

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
"""

MAIN_TSCN = """\
[gd_scene format=3 uid="uid://initwalk_main"]

[node name="Main" type="Node2D"]
"""


def _die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def find_godot() -> Path:
    env_bin = os.environ.get("GODOT_BIN")
    if env_bin:
        p = Path(env_bin)
        if p.is_file():
            return p
        _die(f"GODOT_BIN={env_bin} not a file")
    macos_default = Path("/Applications/Godot.app/Contents/MacOS/Godot")
    if macos_default.exists():
        return macos_default
    on_path = shutil.which("godot")
    if on_path:
        return Path(on_path)
    _die("找不到 Godot 二进制（设置 GODOT_BIN 或加入 PATH）")


def venv_bin(venv: Path, name: str) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    print(f"==> {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def best_effort_kill(pidfile: Path) -> None:
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def main() -> int:
    godot = find_godot()
    print(f"==> 使用 Godot: {godot}", flush=True)
    run([godot, "--version"])

    project = Path(tempfile.mkdtemp(prefix="godot-cli-control-init-"))
    print(f"==> 临时项目: {project}", flush=True)
    pidfile = project / ".cli_control" / "godot.pid"
    try:
        # 1) minimal Godot project（无 plugin / autoload / editor_plugins）
        (project / "project.godot").write_text(PROJECT_GODOT)
        (project / "main.tscn").write_text(MAIN_TSCN)

        # 2) venv + install
        venv = project / ".venv"
        run([sys.executable, "-m", "venv", venv])
        py = venv_bin(venv, "python")
        run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        run([py, "-m", "pip", "install", "--quiet", REPO_ROOT])

        gcc = venv_bin(venv, "godot-cli-control")
        env = {
            **os.environ,
            "GODOT_BIN": str(godot),
            # 子进程（godot-cli-control / godot）打印中文路径 / 提示，
            # Windows 默认 cp1252 stdout 会触发 UnicodeEncodeError。
            "PYTHONIOENCODING": "utf-8",
        }

        # 3) init —— 一键接入
        run([gcc, "init"], cwd=project, env=env)

        # 验证 init 产物
        if not (project / "addons/godot_cli_control/plugin.cfg").is_file():
            _die("插件未复制")
        pg_text = (project / "project.godot").read_text()
        if "[autoload]" not in pg_text:
            _die("[autoload] 段缺失")
        if "GameBridgeNode=" not in pg_text:
            _die("GameBridgeNode 未注入")
        if "enabled=PackedStringArray" not in pg_text:
            _die("editor_plugins 未注入")
        if not (project / ".cli_control/godot_bin").is_file():
            _die("godot_bin 未写入")

        # 4) daemon start（headless，用一个不冲突的端口）
        run(
            [gcc, "daemon", "start", "--headless", "--port", "9897"],
            cwd=project,
            env=env,
        )

        # 5) tree —— 验证 RPC 可达
        result = subprocess.run(
            [str(gcc), "--port", "9897", "tree", "1"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        print(result.stdout)
        if '"name"' not in result.stdout:
            subprocess.run(
                [str(gcc), "daemon", "stop"],
                cwd=project,
                env=env,
                check=False,
            )
            _die("tree 输出不含 name 字段")

        # 6) daemon stop
        run([gcc, "daemon", "stop"], cwd=project, env=env)

        print("==> init walkthrough PASS")
        return 0
    finally:
        best_effort_kill(pidfile)
        shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
