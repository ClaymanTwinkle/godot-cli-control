"""端到端回归：examples/platformer-demo 这个「仓库自带 demo」必须始终能被驱动。

demo 是 README 的门面 + clone-and-run 示例，最怕悄悄腐烂（场景改了节点路径、
CLI 改了子命令、player.gd 改了属性名）却没人发现。本测试在真实 headless Godot
下把 demo 的核心驱动序列跑一遍，断言两个黑盒属性：

  - 点 Start → 等落地 → tap jump   →  jump_count == 1
  - hold move_right                →  moved_right == true

不依赖截图（属性断言即可），所以走普通 headless e2e，不需要 GUI / xvfb。
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
    proj = tmp_path_factory.mktemp("gcc_demo")
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


def test_demo_drives_jump_and_move(daemon: Any) -> None:
    project = daemon

    # 点 Start，让小人入场（main.gd: pressed → active=true + visible）。
    assert _run_cli(project, "wait-node", "/root/Main/UI/StartButton")["result"]["found"]
    assert _run_cli(project, "click", "/root/Main/UI/StartButton")["ok"]

    # 等小人落地（重力从 y=80 落到地面约 0.6s），再起跳。
    assert _run_cli(project, "wait-node", "/root/Main/World/Player")["result"]["found"]
    _run_cli(project, "wait-time", "1.0")
    assert _run_cli(project, "tap", "jump")["ok"]
    _run_cli(project, "wait-time", "0.3")

    jump = _run_cli(project, "get", "/root/Main/World/Player", "jump_count")
    assert jump["ok"] is True, jump
    # PR2 后 get result 透传 RPC shape: {"value": ..., "type": ...}
    assert jump["result"]["value"] == 1, f"期望恰好跳 1 次，实际 {jump['result']}"

    # 向右跑 1s，moved_right 应被置真。
    assert _run_cli(project, "hold", "move_right", "1.0")["ok"]
    _run_cli(project, "wait-time", "0.2")
    moved = _run_cli(project, "get", "/root/Main/World/Player", "moved_right")
    assert moved["ok"] is True, moved
    assert moved["result"]["value"] is True, f"期望 moved_right=true，实际 {moved['result']}"
