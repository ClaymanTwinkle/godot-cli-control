"""端到端回归：GUI（windowed）模式下的 screenshot 路径（issue #64）。

GUT 套件全跑在 ``--headless`` 下，``RenderingServer.get_rendering_device() ==
null``，所有 screenshot 用例都走 dummy 渲染路径。``frame_post_draw`` 这条
windowed 分支 —— 也就是真实用户用 screenshot 的路径 —— 长期没有任何自动化覆盖：
issue #61 本身就是这种 silent regression（GUT 全绿 + pytest 全绿，但 GUI 模式
screenshot 回归）的产物。

本测试真起一个 **GUI**（``--gui``，非 headless）daemon，**立刻**截图（不加任何
``wait(...)`` 魔法 —— #61 的 H + D 启动 gate 应保证首帧 viewport ready），断言：
  1. 返回 ``ok=true`` 且不报 1006（RESOURCE_UNAVAILABLE transient）。
  2. 落盘是非空、带 PNG magic 的真 PNG。
  3. 连续重复 N 次（覆盖动态 transient 兜底，验证不是只有第一帧侥幸成功）。

需要真实 Godot 4 **且真实显示**：
  - PATH 里有 ``godot``（或设 ``GODOT_BIN``）；
  - ``GCC_GUI_E2E=1`` 显式开启（本地默认 skip —— 多数开发机 / 无头 CI 没显示，
    误跑会卡在开窗或拿不到 RenderingDevice）。CI 专档在 Linux 套 xvfb 或用
    macOS runner，并设 ``GCC_GUI_E2E=1``。

设计讨论（issue #64 内）：是否给 ``client.screenshot`` 加 1006 retry 兜底走
「数据驱动」—— 先用本测试收集 windowed 路径是否真撞 1006，撞了再加 client retry
并把次数/间隔做成可配。本测试就是那个数据采集点。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from godot_cli_control.daemon import find_godot_binary

# 与生产 daemon 用同一套 godot 检测（GODOT_BIN > macOS .app > PATH > Windows），
# 不再硬依赖 GODOT_BIN env。注意本文件还有第二层 GCC_GUI_E2E gate（需真实显示）。
_GODOT_BIN = find_godot_binary()
_ADDON_SRC = Path(__file__).resolve().parents[2] / "addons" / "godot_cli_control"

# RESOURCE_UNAVAILABLE：screenshot 时 viewport texture 暂不可用的 transient 码。
# 见 addons/godot_cli_control/bridge/error_codes.gd。
_RESOURCE_UNAVAILABLE = 1006

# 重复次数：> 1 才能区分「首帧侥幸」与「稳定可用」。
_REPEAT = 5

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(
        not _GODOT_BIN,
        reason="需要真实 Godot 4：把 godot 装进 PATH 或设 GODOT_BIN",
    ),
    pytest.mark.skipif(
        os.environ.get("GCC_GUI_E2E") != "1",
        reason="GUI e2e 默认 skip（需真实显示 / xvfb）；CI 专档设 GCC_GUI_E2E=1 开启",
    ),
]

# 用 `python -m godot_cli_control` 调 CLI：与 PATH 无关，always 命中当前环境。
_CLI = [sys.executable, "-m", "godot_cli_control"]

# main 场景给个有尺寸的 ColorRect，让窗口真有可渲染内容（空 Node 也能截，但有内容
# 更贴近真实用法、也更容易暴露 viewport-not-ready 类问题）。
_PROJECT_GODOT = """\
config_version=5

[application]
config/name="gcc_e2e_screenshot"
run/main_scene="res://main.tscn"
config/features=PackedStringArray("4.2")

[autoload]
GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"

[debug]
settings/stdout/print_fps=false

[display]
window/size/viewport_width=320
window/size/viewport_height=240

[editor_plugins]
enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")
"""

_MAIN_TSCN = """\
[gd_scene format=3 uid="uid://gccscreenshot"]

[node name="Main" type="Control"]
layout_mode = 3
anchors_preset = 15

[node name="Bg" type="ColorRect" parent="."]
layout_mode = 1
anchors_preset = 15
color = Color(0.2, 0.4, 0.8, 1)

[node name="Patch" type="ColorRect" parent="."]
layout_mode = 0
offset_left = 40.0
offset_top = 30.0
offset_right = 140.0
offset_bottom = 110.0
color = Color(1, 0, 0, 1)
"""


def _run_cli(project: Path, *args: str, timeout: float = 90.0) -> dict[str, Any]:
    """跑一条 CLI 子命令（独立进程 = 独立连接），解析最后一行 JSON 信封。"""
    proc = subprocess.run(
        _CLI + list(args),
        cwd=project,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    json_lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert json_lines, (
        f"无 JSON 输出：args={args} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return json.loads(json_lines[-1])


@pytest.fixture(scope="module")
def godot_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """搭一个最小真实 Godot 工程（addon + autoload + 有内容的 main 场景）并导入一次。"""
    proj = tmp_path_factory.mktemp("gcc_e2e_screenshot")
    (proj / "addons").mkdir()
    shutil.copytree(_ADDON_SRC, proj / "addons" / "godot_cli_control")
    (proj / "project.godot").write_text(_PROJECT_GODOT)
    (proj / "main.tscn").write_text(_MAIN_TSCN)

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
def gui_daemon(godot_project: Path) -> Any:
    """起/停一个真实 **GUI**（开窗）daemon；teardown 兜底 stop。

    用 ``--gui`` 强制开窗，绕过 isatty 自动判 —— 必须开窗才会走 windowed
    ``frame_post_draw`` 渲染分支（这正是本测试要覆盖的路径）。
    """
    start = _run_cli(godot_project, "daemon", "start", "--gui", timeout=120)
    assert start["ok"] is True and start["result"]["started"], start
    try:
        yield godot_project
    finally:
        _run_cli(godot_project, "daemon", "stop", timeout=30)


def test_windowed_screenshot_returns_png_without_wait(
    gui_daemon: Any, tmp_path: Path
) -> None:
    """GUI 模式立刻截图（无 wait 魔法）应稳定拿到非空 PNG，且不撞 1006。"""
    project = gui_daemon

    for i in range(_REPEAT):
        out = tmp_path / f"shot_{i}.png"
        payload = _run_cli(project, "screenshot", str(out))

        # 不应撞 transient 1006：H + D 启动 gate 之后 windowed 路径应已稳定。
        # 若真撞到，这里给出明确信号 —— 即 issue #64 里「数据驱动决定是否加
        # client retry」的触发条件。
        if not payload["ok"]:
            assert payload["error"]["code"] != _RESOURCE_UNAVAILABLE, (
                f"第 {i} 次 windowed screenshot 撞到 1006 transient —— "
                f"H+D 兜底未覆盖此 case，应据此加 client retry：{payload}"
            )
            pytest.fail(f"第 {i} 次 screenshot 失败：{payload}")

        assert out.exists(), f"第 {i} 次 screenshot 未落盘：{payload}"
        raw = out.read_bytes()
        assert raw, f"第 {i} 次 screenshot PNG 为空"
        assert raw.startswith(b"\x89PNG\r\n\x1a\n"), (
            f"第 {i} 次 screenshot 不是合法 PNG（magic 不符）：{raw[:8]!r}"
        )
        assert payload["result"]["bytes"] == len(raw), (
            f"第 {i} 次 信封 bytes 与落盘大小不一致：{payload}"
        )


def test_windowed_screenshot_node_crop(gui_daemon: Any, tmp_path: Path) -> None:
    """--node 裁剪 happy path（issue #101）：region 信封与 PNG 实际尺寸一致。

    Patch 是 (40,30) 起、100x80 的 ColorRect，窗口 320x240 无 stretch ——
    裁剪 region 应逐像素等于其布局矩形；PNG IHDR 尺寸应等于 region 尺寸。
    """
    project = gui_daemon
    out = tmp_path / "crop.png"
    payload = _run_cli(project, "screenshot", str(out), "--node", "/root/Main/Patch")
    assert payload["ok"] is True, payload
    assert payload["result"]["node"] == "/root/Main/Patch"
    assert payload["result"]["region"] == [40, 30, 100, 80], payload

    raw = out.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", raw[16:24])  # IHDR 头的 w/h 字段
    assert (width, height) == (100, 80), f"PNG 尺寸应等于裁剪 region：{(width, height)}"

    # 对照组：全屏截图必须严格大于裁剪小图（证明真裁了，不是整屏改名）
    full = tmp_path / "full.png"
    full_payload = _run_cli(project, "screenshot", str(full))
    assert full_payload["ok"] is True
    assert full_payload["result"]["bytes"] > payload["result"]["bytes"]


def test_windowed_screenshot_node_offscreen_1011(gui_daemon: Any, tmp_path: Path) -> None:
    """节点挪到视口外 → 1011（NODE_NOT_ON_SCREEN），且不落盘。"""
    project = gui_daemon
    assert _run_cli(project, "set", "/root/Main/Patch", "position", "[-500, -500]")["ok"]
    try:
        out = tmp_path / "off.png"
        payload = _run_cli(project, "screenshot", str(out), "--node", "/root/Main/Patch")
        assert payload["ok"] is False and payload["error"]["code"] == 1011, payload
        assert not out.exists()
    finally:
        # 还原位置，避免污染同 module 的其它用例（gui_daemon 是 function 级，
        # 但 godot_project 是 module 级 —— daemon 重启不重建场景文件，保险起见还原）
        _run_cli(project, "set", "/root/Main/Patch", "position", "[40, 30]")


def test_screenshot_raw_server_side_write_no_base64(
    gui_daemon: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon 直写协议（issue #149）：传 path 时 PNG 由 daemon 落盘，响应只回
    ``{path, bytes}`` 元数据、**不含 base64**。

    这是「大图不过 WS」的直接证据 —— CLI 信封在新旧协议下长得一样（回退
    透明），只有 raw 响应能区分 daemon 直写与本地回退。"""
    import asyncio

    from godot_cli_control.client import GameClient

    project = gui_daemon
    monkeypatch.chdir(project)  # 端口 auto-discover 读 CWD 的 .cli_control
    out = (tmp_path / "raw.png").resolve()

    async def go() -> dict:
        async with GameClient() as client:
            return await client.screenshot_raw(path=str(out))

    raw = asyncio.run(go())
    assert "image" not in raw, f"path 模式不得回 base64：keys={list(raw)}"
    assert raw["path"] == str(out)
    data = out.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert raw["bytes"] == len(data)


def test_screenshot_server_write_failure_reports_1013(
    gui_daemon: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon 落盘失败（父目录不存在）→ 1013 WRITE_FAILED，信息带路径。

    CLI 正常路径建好父目录打不到这里；这是 raw RPC 调用方的契约测试
    （issue #149：fail-loud，不能静默回 ok）。"""
    import asyncio

    from godot_cli_control.client import GameClient, RpcError

    project = gui_daemon
    monkeypatch.chdir(project)
    missing = tmp_path / "no_such_dir" / "x.png"

    async def go() -> None:
        async with GameClient() as client:
            await client.screenshot_raw(path=str(missing))

    with pytest.raises(RpcError) as ei:
        asyncio.run(go())
    assert ei.value.code == 1013, f"应报 1013 WRITE_FAILED：{ei.value}"
    assert "no_such_dir" in ei.value.message
