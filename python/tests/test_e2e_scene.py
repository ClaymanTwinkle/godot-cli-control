"""端到端回归：scene-reload / scene-change 全链路。

验证点：
  - scene-reload 让已污染的属性归位（重载即重置）
  - scene-change 切换到 second.tscn，旧场景根不再存在
  - scene-change 指向不存在的场景 → 信封 ok=false error.code==1008
  - GameBridge.scene_reload() 直连链路（fresh_scene fixture 的核心调用）

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
    proj = tmp_path_factory.mktemp("gcc_scene")
    for name in ("project.godot", "main.tscn", "main.gd", "player.gd", "second.tscn"):
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


def test_scene_reload_resets_mutated_state(daemon: Any) -> None:
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main/UI/StartButton")["result"]["found"]
    # 污染场景状态
    assert _run_cli(project, "set", "/root/Main/UI/StartButton", "text", '"DIRTY"')["ok"]
    dirty = _run_cli(project, "get", "/root/Main/UI/StartButton", "text")
    assert dirty["result"]["value"] == "DIRTY"
    # reload → 状态归位
    r = _run_cli(project, "scene-reload")
    assert r["ok"] is True, r
    assert r["result"]["name"] == "Main"
    assert r["result"]["scene_path"] == "res://main.tscn"
    clean = _run_cli(project, "get", "/root/Main/UI/StartButton", "text")
    assert clean["result"]["value"] == "Start", "reload 后属性应回到场景文件初值"


def test_scene_change_switches_to_second(daemon: Any) -> None:
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main")["result"]["found"]
    r = _run_cli(project, "scene-change", "res://second.tscn")
    assert r["ok"] is True, r
    assert r["result"]["name"] == "Second"
    assert _run_cli(project, "exists", "/root/Second")["result"] is True
    # 旧场景根应已不在
    assert _run_cli(project, "exists", "/root/Main")["result"] is False


def test_scene_change_missing_scene_returns_1008(daemon: Any) -> None:
    project = daemon
    r = _run_cli(project, "scene-change", "res://__missing__.tscn")
    assert r["ok"] is False
    assert r["error"]["code"] == 1008
    # 路径校验在碰场景之前（ResourceLoader.exists 先行）：
    # 1008 后 daemon 仍存活、当前场景不变
    assert _run_cli(project, "exists", "/root/Main")["result"] is True, (
        "scene-change 失败不应破坏当前场景"
    )


def test_bridge_scene_reload_roundtrip(daemon: Any) -> None:
    """fresh_scene fixture 的核心调用（bridge.scene_reload）真链路验证。"""
    from godot_cli_control.bridge import GameBridge

    project = daemon
    port = int((project / ".cli_control" / "port").read_text().strip())
    b = GameBridge(port=port)
    try:
        result = b.scene_reload()
        assert result["name"] == "Main"
    finally:
        b.close()
