"""端到端回归：errors 结构化查询 + daemon logs（issue #103）。

验证点（headless 全可跑——Logger 捕获不依赖渲染）：
  - call 触发游戏侧 push_error → errors 增量查询拿到结构化条目
    （message / type / source 指回 GDScript 调用位置）
  - marker 游标：--since 只看新增（「本用例期间」语义的原语）
  - push_warning → type=warning
  - daemon logs --tail 输出 godot.log 尾部（JSON 信封 + 路径）

no_push_errors fixture 的行为由 test_pytest_plugin.py（pytester 假桩）覆盖；
这里覆盖真实引擎链路。

需要真实 Godot 4.5+（Logger API；CI/本机均 4.6.2）：PATH 里有 godot
（或设 GODOT_BIN），否则整文件 skip。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from godot_cli_control.daemon import find_godot_binary

_GODOT_BIN = find_godot_binary()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADDON_SRC = _REPO_ROOT / "addons" / "godot_cli_control"

pytestmark = pytest.mark.skipif(
    not _GODOT_BIN,
    reason="需要真实 Godot 4：把 godot 装进 PATH 或设 GODOT_BIN，否则整文件 skip",
)

_CLI = [sys.executable, "-m", "godot_cli_control"]

_PROJECT_GODOT = """\
config_version=5

[application]
config/name="gcc-diag-e2e"
run/main_scene="res://main.tscn"
config/features=PackedStringArray("4.4")

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"

[autoload]

GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"

[editor_plugins]

enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")
"""

_MAIN_TSCN = """\
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://main.gd" id="1_main"]

[node name="Main" type="Node2D"]
script = ExtResource("1_main")
"""

_MAIN_GD = """\
extends Node2D


func boom() -> void:
	push_error("e2e boom")


func warn() -> void:
	push_warning("e2e warn")
"""


def _run_cli(project: Path, *args: str, timeout: float = 60.0) -> dict[str, Any]:
    """跑一条 CLI 子命令（独立进程 = 独立连接），解析最后一行 JSON 信封。"""
    proc = subprocess.run(
        _CLI + list(args),
        cwd=project,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    json_lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert json_lines, f"无 JSON 输出：args={args} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return json.loads(json_lines[-1])


@pytest.fixture(scope="module")
def diag_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    proj = tmp_path_factory.mktemp("gcc_diag")
    (proj / "project.godot").write_text(_PROJECT_GODOT, encoding="utf-8")
    (proj / "main.tscn").write_text(_MAIN_TSCN, encoding="utf-8")
    (proj / "main.gd").write_text(_MAIN_GD, encoding="utf-8")
    (proj / "addons").mkdir()
    shutil.copytree(_ADDON_SRC, proj / "addons" / "godot_cli_control")

    imp = subprocess.run(
        [_GODOT_BIN, "--headless", "--editor", "--quit", "--path", str(proj)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert imp.returncode == 0, f"Godot 导入失败：{imp.stdout}\n{imp.stderr}"
    return proj


@pytest.fixture(scope="module")
def daemon(diag_project: Path) -> Any:
    """module 级 daemon：用例间靠 --since 游标隔离，无需重启。"""
    start = _run_cli(diag_project, "daemon", "start", "--headless", timeout=90)
    assert start["ok"] is True and start["result"]["started"], start
    assert _run_cli(diag_project, "wait-node", "/root/Main")["result"]["found"]
    try:
        yield diag_project
    finally:
        _run_cli(diag_project, "daemon", "stop", timeout=30)


def _baseline(project: Path) -> int:
    env = _run_cli(project, "errors", "--limit", "0")
    assert env["ok"] is True, env
    return int(env["result"]["marker"])


def test_push_error_roundtrip_with_source(daemon: Any) -> None:
    """call 触发 push_error → errors 增量拿到结构化条目，source 指回 main.gd。"""
    marker = _baseline(daemon)
    assert _run_cli(daemon, "call", "/root/Main", "boom")["ok"]
    env = _run_cli(daemon, "errors", "--since", str(marker))
    assert env["ok"] is True, env
    entries = env["result"]["errors"]
    booms = [e for e in entries if e["message"] == "e2e boom"]
    assert booms, f"应捕获 e2e boom：{entries}"
    entry = booms[0]
    assert entry["type"] == "error"
    assert "main.gd" in str(entry["source"]), f"source 应指回 GDScript：{entry}"
    assert int(env["result"]["marker"]) > marker


def test_warning_captured_as_warning(daemon: Any) -> None:
    marker = _baseline(daemon)
    assert _run_cli(daemon, "call", "/root/Main", "warn")["ok"]
    env = _run_cli(daemon, "errors", "--since", str(marker))
    warns = [e for e in env["result"]["errors"] if e["message"] == "e2e warn"]
    assert warns and warns[0]["type"] == "warning", env


def test_since_cursor_isolates_increments(daemon: Any) -> None:
    """第一轮 boom 后取 marker，第二轮查询不应再看到第一轮的条目。"""
    marker0 = _baseline(daemon)
    assert _run_cli(daemon, "call", "/root/Main", "boom")["ok"]
    env1 = _run_cli(daemon, "errors", "--since", str(marker0))
    marker1 = int(env1["result"]["marker"])
    assert any(e["message"] == "e2e boom" for e in env1["result"]["errors"])
    env2 = _run_cli(daemon, "errors", "--since", str(marker1))
    assert env2["result"]["errors"] == [], f"marker 之后无新增应为空：{env2}"


def test_daemon_logs_tail(daemon: Any) -> None:
    env = _run_cli(daemon, "daemon", "logs", "--tail", "20")
    assert env["ok"] is True, env
    result = env["result"]
    assert result["path"].endswith("godot.log")
    assert result["returned"] == len(result["lines"]) > 0
    # Godot 启动横幅必然在日志里（取尾 20 行可能截掉，放宽为「有内容」即可）
    assert any(line.strip() for line in result["lines"])
