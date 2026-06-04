"""端到端回归：时间控制全链路（pause / step-frames / time-scale / 启动倍速）。

验证点：
  - pause 冻结物理帧 → step-frames --physics 确定性推进 → unpause
  - step-frames 未 pause 前置 → 信封 ok=false error.code==1009
  - time-scale 读写往返正确
  - daemon start --time-scale 2 → 连上即读到 2.0（第 0 帧生效）

需要真实 Godot 4：PATH 里有 godot（或设 GODOT_BIN），否则整文件 skip。
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

# 与生产 daemon 用同一套 godot 检测（GODOT_BIN > macOS .app > PATH > Windows）。
_GODOT_BIN = find_godot_binary()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_SRC = _REPO_ROOT / "examples" / "platformer-demo"
_ADDON_SRC = _REPO_ROOT / "addons" / "godot_cli_control"

pytestmark = pytest.mark.skipif(
    not _GODOT_BIN,
    reason="需要真实 Godot 4：把 godot 装进 PATH 或设 GODOT_BIN，否则整文件 skip",
)

# 用 `python -m godot_cli_control` 调 CLI：与 PATH 无关，always 命中当前环境。
_CLI = [sys.executable, "-m", "godot_cli_control"]

# demo 的 project.godot 故意不含这两段（留给 init）。测试里直接 append，等价于
# init 的 patch，但不依赖 init 的 godot 探测 / 重导入，更可控、与其它 e2e 一致。
_BRIDGE_SECTIONS = """
[autoload]

GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"

[editor_plugins]

enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")
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
def demo_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把 examples/platformer-demo 拷到 tmp，接上 addon + bridge 段，导入一次。"""
    proj = tmp_path_factory.mktemp("gcc_time")
    for name in ("project.godot", "main.tscn", "main.gd", "player.gd"):
        shutil.copy(_DEMO_SRC / name, proj / name)
    (proj / "addons").mkdir()
    shutil.copytree(_ADDON_SRC, proj / "addons" / "godot_cli_control")
    with (proj / "project.godot").open("a", encoding="utf-8") as f:
        f.write(_BRIDGE_SECTIONS)

    # 先跑一次编辑器导入，注册全局 class + 资源 .import（headless 导入即可）。
    imp = subprocess.run(
        [_GODOT_BIN, "--headless", "--editor", "--quit", "--path", str(proj)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert imp.returncode == 0, f"Godot 导入失败：{imp.stdout}\n{imp.stderr}"
    return proj


@pytest.fixture
def daemon(demo_project: Path) -> Any:
    """每个用例起 / 停一个真实 headless daemon。"""
    start = _run_cli(demo_project, "daemon", "start", "--headless", timeout=90)
    assert start["ok"] is True and start["result"]["started"], start
    try:
        yield demo_project
    finally:
        _run_cli(demo_project, "daemon", "stop", timeout=30)


def test_pause_freezes_and_step_frames_advances_physics(daemon: Any) -> None:
    """pause 冻结物理 → 空中传送 → step-frames --physics 确定性下落 → unpause。

    去竞态设计：不依赖「pause 抢在落地前」——pause 后用 set 把 player 传送回
    空中（paused 下属性写照常生效），step 的重力下落就与机器速度无关。
    """
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main/UI/StartButton")["result"]["found"]
    assert _run_cli(project, "click", "/root/Main/UI/StartButton")["ok"]
    assert _run_cli(project, "wait-node", "/root/Main/World/Player")["result"]["found"]
    assert _run_cli(project, "pause")["result"]["paused"] is True
    # 传送回空中（y=80 远离地面 y≈280），paused 下 set 直接写属性
    assert _run_cli(project, "set", "/root/Main/World/Player", "position", "[320, 80]")["ok"]
    y1 = _run_cli(project, "get", "/root/Main/World/Player", "position:y")["result"]["value"]
    assert y1 == 80.0, f"传送后应在空中：{y1}"
    # paused 下墙钟流逝，位置必须冻结
    _run_cli(project, "wait-time", "0.3")
    y2 = _run_cli(project, "get", "/root/Main/World/Player", "position:y")["result"]["value"]
    assert y2 == y1, f"paused 下位置不得变化：{y1} -> {y2}"
    # 确定性推进 10 个物理帧：重力必须使 y 增大
    r = _run_cli(project, "step-frames", "10", "--physics")
    assert r["ok"] is True, r
    assert r["result"]["stepped"] == 10
    assert r["result"]["paused"] is True
    y3 = _run_cli(project, "get", "/root/Main/World/Player", "position:y")["result"]["value"]
    assert y3 > y2, f"10 个物理帧后应下落：{y2} -> {y3}"
    assert _run_cli(project, "unpause")["result"]["paused"] is False


def test_step_frames_without_pause_returns_1009(daemon: Any) -> None:
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main")["result"]["found"]
    r = _run_cli(project, "step-frames", "3")
    assert r["ok"] is False
    assert r["error"]["code"] == 1009


def test_time_scale_roundtrip(daemon: Any) -> None:
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main")["result"]["found"]
    assert _run_cli(project, "time-scale")["result"]["time_scale"] == 1.0
    assert _run_cli(project, "time-scale", "5")["result"]["time_scale"] == 5.0
    assert _run_cli(project, "time-scale")["result"]["time_scale"] == 5.0
    # 还原，避免影响同 daemon 的后续操作（虽然 daemon 是 function-scope，防御性）
    assert _run_cli(project, "time-scale", "1")["ok"]


def test_daemon_start_time_scale_applies_at_startup(demo_project: Path) -> None:
    """daemon start --time-scale 2：连上即读到 2.0（第 0 帧生效路径）。"""
    project = demo_project
    start = _run_cli(project, "daemon", "start", "--headless", "--time-scale", "2", timeout=90)
    assert start["ok"] is True and start["result"]["started"], start
    try:
        r = _run_cli(project, "time-scale")
        assert r["result"]["time_scale"] == 2.0
    finally:
        _run_cli(project, "daemon", "stop", timeout=30)
