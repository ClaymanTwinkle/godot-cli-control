"""端到端回归：真实 Godot daemon 下的输入持续性（issue #70）及事件回调链路（issue #97）。

拦的回归：CLI 每条子命令都是独立连接、跑完即「干净关闭」。曾经 GameBridge
在断连时无条件 release_all，导致 ``hold`` 的定时器没倒计时就被清掉（只生效
一帧）、sticky ``press`` 也无法跨命令存活。这个 bug 当时 GUT（mock socket）
和 pytest（mock subprocess）都没逮到 —— 只有真实 daemon 端到端能复现。

覆盖三条链路：
  1. 干净关闭后 ``hold`` 仍持续，duration 到点由定时器自动释放（不是断连释放）。
  2. sticky ``press`` 跨命令存活，直到 ``release-all``。
  3. 异常掉线（无 WebSocket close frame，close_code == -1）触发 release_all 兜底。

需要真实 Godot 4：PATH 里有 ``godot``（或设 ``GODOT_BIN``），否则整文件 skip。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from godot_cli_control.daemon import find_godot_binary

# 与生产 daemon 用同一套 godot 检测（GODOT_BIN > macOS .app > PATH > Windows），
# 不再硬依赖 GODOT_BIN env：本机装了 godot 即自动跑这条 e2e。
_GODOT_BIN = find_godot_binary()
_ADDON_SRC = Path(__file__).resolve().parents[2] / "addons" / "godot_cli_control"

pytestmark = pytest.mark.skipif(
    not _GODOT_BIN,
    reason="需要真实 Godot 4：把 godot 装进 PATH 或设 GODOT_BIN，否则整文件 skip",
)

# 用 `python -m godot_cli_control` 调 CLI：与 PATH 无关，always 命中当前环境。
_CLI = [sys.executable, "-m", "godot_cli_control"]

_PROJECT_GODOT = """\
config_version=5

[application]
config/name="gcc_e2e"
run/main_scene="res://main.tscn"

[autoload]
GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"

[debug]
settings/stdout/print_fps=false

[editor_plugins]
enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")

[input]
move_right={"deadzone":0.5,"events":[]}
jump={"deadzone":0.5,"events":[]}
"""

_MAIN_TSCN = """\
[gd_scene format=3]

[node name="Main" type="Node"]
"""


# ── issue #97：事件回调式游戏（_unhandled_input）能看到 press 注入的事件 ──────────

_EVENT_PROBE_GD = """\
extends Node

var saw_jump_event: bool = false

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventAction and event.action == "jump" and event.is_pressed():
		saw_jump_event = true
"""

_MAIN_WITH_PROBE_TSCN = """\
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://probe.gd" id="1"]

[node name="Main" type="Node"]
script = ExtResource("1")
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
    # 信封是 stdout 单行 JSON；daemon start 等可能先打 stderr 进度，取最后的 JSON 行。
    json_lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert json_lines, f"无 JSON 输出：args={args} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return json.loads(json_lines[-1])


def _pressed(project: Path) -> list[str]:
    payload = _run_cli(project, "pressed")
    assert payload["ok"] is True, payload
    return payload["result"]


@pytest.fixture(scope="module")
def godot_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """搭一个最小真实 Godot 工程（addon + autoload + 两个 InputMap 动作）并导入一次。"""
    proj = tmp_path_factory.mktemp("gcc_e2e")
    (proj / "addons").mkdir()
    shutil.copytree(_ADDON_SRC, proj / "addons" / "godot_cli_control")
    (proj / "project.godot").write_text(_PROJECT_GODOT)
    (proj / "main.tscn").write_text(_MAIN_TSCN)

    # 先跑一次编辑器导入，注册全局 class（CliControlErrorCodes）+ 资源 .import。
    imp = subprocess.run(
        [_GODOT_BIN, "--headless", "--editor", "--quit", "--path", str(proj)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert imp.returncode == 0, f"Godot 导入失败：{imp.stdout}\n{imp.stderr}"
    return proj


@pytest.fixture
def daemon(godot_project: Path) -> Any:
    """每个用例起/停一个真实 headless daemon；teardown 兜底 release-all + stop。"""
    start = _run_cli(godot_project, "daemon", "start", "--headless", timeout=90)
    assert start["ok"] is True and start["result"]["started"], start
    port = start["result"]["port"]
    try:
        yield godot_project, port
    finally:
        _run_cli(godot_project, "release-all")
        _run_cli(godot_project, "daemon", "stop", timeout=30)


def test_hold_persists_across_clean_disconnect_then_auto_releases(daemon: Any) -> None:
    project, _ = daemon
    # hold 1s：命令拿到响应后干净关闭连接 —— 断连不应清掉它。
    held = _run_cli(project, "hold", "move_right", "1.0")
    assert held["ok"] is True, held

    # 紧跟一条独立命令：若断连清了，这里就空了（旧 bug：只生效一帧）。
    assert "move_right" in _pressed(project), "干净关闭后 hold 应跨命令存活"

    # 等过 duration：应由 advance_timers 定时器自动释放（不是断连释放）。
    time.sleep(2.0)
    assert _pressed(project) == [], "duration 到点后 hold 应被定时器自动释放"


def test_press_persists_until_release_all(daemon: Any) -> None:
    project, _ = daemon
    pressed = _run_cli(project, "press", "jump")
    assert pressed["ok"] is True, pressed

    # sticky press 没有定时器：跨多条独立命令仍应保持按下。
    assert "jump" in _pressed(project), "sticky press 应跨命令存活"
    time.sleep(1.0)
    assert "jump" in _pressed(project), "press 不该自己释放"

    rel = _run_cli(project, "release-all")
    assert rel["ok"] is True, rel
    assert _pressed(project) == [], "release-all 后应清空"


def test_abnormal_disconnect_releases_held_inputs(daemon: Any) -> None:
    project, port = daemon
    # 子进程：连上 → 发 hold → 硬退出（不发 close frame）→ daemon 侧 close_code = -1。
    drop_script = (
        "import asyncio, json, os, websockets\n"
        "async def main():\n"
        f"    ws = await websockets.connect('ws://127.0.0.1:{port}')\n"
        "    await ws.send(json.dumps({'id':'1','method':'input_hold',"
        "'params':{'action':'move_right','duration':30.0}}))\n"
        "    await ws.recv()\n"
        "    os._exit(0)\n"
        "asyncio.run(main())\n"
    )
    drop = subprocess.run(
        [sys.executable, "-c", drop_script], capture_output=True, text=True, timeout=30
    )
    assert drop.returncode == 0, f"abrupt-drop 客户端异常：{drop.stderr}"

    # 异常掉线应触发 release_all 兜底（即便 hold 还剩 ~30s）。给 daemon 几帧检测断连。
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _pressed(project) == []:
            break
        time.sleep(0.2)
    assert _pressed(project) == [], "异常掉线应触发 release_all 清掉持有输入"


@pytest.fixture
def probe_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """带 probe.gd（_unhandled_input 探针）的独立 Godot 项目，用于 issue #97 e2e。"""
    proj = tmp_path_factory.mktemp("gcc_e2e_probe")
    (proj / "addons").mkdir()
    shutil.copytree(_ADDON_SRC, proj / "addons" / "godot_cli_control")
    (proj / "project.godot").write_text(_PROJECT_GODOT)
    (proj / "probe.gd").write_text(_EVENT_PROBE_GD)
    (proj / "main.tscn").write_text(_MAIN_WITH_PROBE_TSCN)

    imp = subprocess.run(
        [_GODOT_BIN, "--headless", "--editor", "--quit", "--path", str(proj)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert imp.returncode == 0, f"Godot 导入失败：{imp.stdout}\n{imp.stderr}"
    return proj


def test_press_reaches_unhandled_input_callback(probe_project: Path) -> None:
    """press 注入的事件必须经由事件管线送达 _unhandled_input（issue #97 核心链路）。"""
    start = _run_cli(probe_project, "daemon", "start", "--headless", timeout=90)
    assert start["ok"] is True and start["result"]["started"], start
    try:
        pressed = _run_cli(probe_project, "press", "jump")
        assert pressed["ok"] is True, pressed

        _run_cli(probe_project, "wait-time", "0.2")

        payload = _run_cli(probe_project, "get", "/root/Main", "saw_jump_event")
        assert payload["ok"] is True, payload
        # PR1 阶段 get 仍是旧编码（裸值）；PR2 落地后此断言改 result["value"]
        assert payload["result"] is True, (
            f"_unhandled_input 应收到 press 事件并置 saw_jump_event=true，实际 result={payload['result']!r}"
        )
    finally:
        _run_cli(probe_project, "release-all")
        _run_cli(probe_project, "daemon", "stop", timeout=30)
