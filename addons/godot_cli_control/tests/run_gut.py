#!/usr/bin/env python3
"""跨平台 GUT runner —— run_gut.sh 的 Python 等价物（issue #36）。

run_gut.sh 是 bash，Windows runner 用不了；macOS 上 GUT cmdln 的 stdout buffering
历史上偶发丢字。改用 Python stdlib（tempfile / shutil / subprocess）抹平
mktemp / cp -r / 路径分隔符这些平台差异，让 CI 能在 ubuntu / macOS / windows
三格统一跑 GDScript 单测。

逻辑与 run_gut.sh 一致：
  1. 找 Godot 二进制（GODOT_BIN > macOS 默认 .app > PATH 中的 godot[.exe]）。
  2. 临时目录搭最小 Godot 工程 + 复制本 plugin。
  3. clone GUT 固定 tag，搬运 addons/gut。
  4. headless 预热 import（填 .godot/ 缓存）。
  5. headless 跑 gut_cmdln.gd，实时透传输出并缓冲。
  6. 断言 GUT 的 "All tests passed!" marker（cmdln 加载失败时 Godot 仍可能 exit 0，
     所以不能只看 returncode）。

用法：
    GODOT_BIN=/path/to/godot python3 run_gut.py
    python3 run_gut.py            # 未设 GODOT_BIN 时尝试 macOS 默认 / PATH
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows 默认 stdout/stderr 编码是 cp1252，编不了中文日志或 Godot 输出里的非 ASCII
# 字符（issue #36：Windows CI 格曾因 `_log("使用 Godot…")` 在第一行就 UnicodeEncodeError
# 崩掉，GUT 根本没跑起来）。强制 UTF-8，macOS/Linux 本就默认 UTF-8 不受影响。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # 非 TextIOWrapper（被重定向）时静默跳过
        pass

GUT_REF = "v9.4.0"  # bumping：检查 https://github.com/bitwes/Gut/releases

# 仓库根：本脚本在 addons/godot_cli_control/tests/，往上 3 级。
REPO_ROOT = Path(__file__).resolve().parents[3]

_PROJECT_GODOT = """\
config_version=5

[application]
config/name="gut-tests"
config/features=PackedStringArray("4.4", "GL Compatibility")

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
"""

_SUCCESS_MARKER = "All tests passed!"

# 假绿守卫（issue #188）：GUT 遇到解析失败的测试脚本只告警并跳过（不计入统计、
# 不判失败），_SUCCESS_MARKER 对整文件被跳过是盲区。引擎/GUT 在跳过脚本时固定
# 打这三类字样之一。
_SILENT_SKIP_RE = re.compile(r"Parse Error|Failed to load script|Ignoring script")

# GUT 汇总里 "Scripts" 一行：summary.gd 用 rpad(18) 对齐，形如 "Scripts           12"。
_SCRIPTS_TOTAL_RE = re.compile(r"^Scripts\s+(\d+)", re.MULTILINE)


def _log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def _fail(msg: str) -> "None":
    print(f"FAIL: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _find_godot() -> str:
    """GODOT_BIN > macOS 默认 .app > PATH 中的 godot/godot.exe。"""
    env_bin = os.environ.get("GODOT_BIN")
    if env_bin:
        if not (Path(env_bin).is_file() and os.access(env_bin, os.X_OK)):
            _fail(f"GODOT_BIN 指向的不是可执行文件：{env_bin}")
        return env_bin

    mac_default = Path("/Applications/Godot.app/Contents/MacOS/Godot")
    if mac_default.is_file() and os.access(mac_default, os.X_OK):
        return str(mac_default)

    for name in ("godot", "godot.exe", "Godot"):
        found = shutil.which(name)
        if found:
            return found

    _fail("找不到 Godot 二进制（设置 GODOT_BIN 或加入 PATH）")
    raise AssertionError("unreachable")  # 给类型检查器：_fail 永远 raise


def _run_godot(godot: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [godot, *args],
        capture_output=capture,
        text=True,
    )


def _clone_gut(dest_parent: Path) -> Path:
    """clone GUT 到临时目录，返回其 addons/gut 路径。"""
    _log(f"下载 GUT {GUT_REF}")
    gut_src = dest_parent / "gut-src"
    res = subprocess.run(
        [
            "git", "clone", "--depth", "1", "--branch", GUT_REF,
            "https://github.com/bitwes/Gut.git", str(gut_src),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        _fail(f"git clone GUT 失败：\n{res.stderr}")
    gut_addon = gut_src / "addons" / "gut"
    if not (gut_addon / "gut_cmdln.gd").is_file():
        _fail(f"GUT {GUT_REF} 内未找到 addons/gut/gut_cmdln.gd（GUT 目录结构变了？）")
    return gut_addon


def _run_gut_cmdln(godot: str, proj: Path) -> str:
    """实时透传 gut_cmdln 输出并缓冲返回（等价 bash 的 `| tee`）。"""
    _log("跑 GUT")
    proc = subprocess.Popen(
        [
            godot, "--headless", "--path", str(proj),
            "-s", "res://addons/gut/gut_cmdln.gd",
            "-gdir=res://addons/godot_cli_control/tests/gut",
            "-gexit",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    buf: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        buf.append(line)
    proc.wait()
    return "".join(buf)


def main() -> int:
    godot = _find_godot()
    _log(f"使用 Godot: {godot}")
    _run_godot(godot, "--version")

    # 一个临时父目录装 工程 + GUT clone；with 块退出自动清理（跨平台、无 trap）。
    with tempfile.TemporaryDirectory(prefix="godot-cli-control-gut-") as tmp:
        tmp_path = Path(tmp)
        proj = tmp_path / "proj"
        proj.mkdir()
        _log(f"临时项目: {proj}")

        # 1) 最小 Godot 工程
        (proj / "project.godot").write_text(_PROJECT_GODOT, encoding="utf-8")

        # 2) 被测 plugin
        (proj / "addons").mkdir()
        shutil.copytree(
            REPO_ROOT / "addons" / "godot_cli_control",
            proj / "addons" / "godot_cli_control",
        )

        # 3) GUT
        gut_addon = _clone_gut(tmp_path)
        shutil.copytree(gut_addon, proj / "addons" / "gut")

        # 4) headless 预热 import（失败不致命，与 .sh 一致：用 || true 语义）
        _log("import 资源")
        _run_godot(godot, "--headless", "--path", str(proj), "--editor", "--quit",
                   capture=True)

        # 5) 跑 GUT
        output = _run_gut_cmdln(godot, proj)

    # 6) 判 PASS 前两道独立守卫，任一命中即 FAIL（issue #188）。
    if _SILENT_SKIP_RE.search(output):
        _fail(
            "检测到脚本解析失败被 GUT 静默跳过"
            "（Parse Error / Failed to load script / Ignoring script）—— 看上面输出排查"
        )

    gut_dir = REPO_ROOT / "addons" / "godot_cli_control" / "tests" / "gut"
    expected_scripts = len(list(gut_dir.glob("test_*.gd")))
    match = _SCRIPTS_TOTAL_RE.search(output)
    if match is None:
        _fail("未能从 GUT 输出中解析出 'Scripts' 计数 —— 看上面输出排查")
    actual_scripts = int(match.group(1))
    if actual_scripts < expected_scripts:
        _fail(
            f"GUT 实际执行脚本数 ({actual_scripts}) 少于 tests/gut/test_*.gd "
            f"文件数 ({expected_scripts}) —— 有脚本被静默跳过"
        )

    # 7) Godot 在 cmdln 脚本加载失败时仍可能 exit 0 —— 额外断言 GUT 成功 marker。
    if _SUCCESS_MARKER in output:
        _log("GUT PASS")
        return 0
    _fail(f"没看到 GUT 的 '{_SUCCESS_MARKER}' marker —— 看上面输出排查")
    return 1  # unreachable（_fail raise）


if __name__ == "__main__":
    raise SystemExit(main())
