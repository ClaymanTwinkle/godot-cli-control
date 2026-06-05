"""端到端回归：sprite-info 渲染态聚合查询（issue #101）。

验证点（全部 headless 可跑——sprite_info 纯属性读，不依赖真渲染）：
  - Sprite2D 帧网格：effective_region 折算正确（视觉断言的核心字段）
  - AnimatedSprite2D：frame_texture 跟当前帧
  - TextureRect：flip/texture 聚合
  - 非 sprite 类节点 → 1010；不存在节点 → 1001
  - screenshot --node 的错误路径（1001/1010 在取图前快速返回；
    headless 下合法节点最终 1006——dummy renderer 拿不到 viewport texture）

裁剪 happy path 需要真渲染，由 test_e2e_screenshot_gui.py（GCC_GUI_E2E
门控，CI 的 macOS 格）兜底。

fixture 项目自建（不复用 platformer-demo：它没有任何 Sprite 节点）；
贴图全部运行时生成（ImageTexture），免去 .import 资源管线。

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
config/name="gcc-render-e2e"
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

# 8x4 贴图：Sheet 切 4x2 网格（每帧 2x2）、frame=5 → frame_coords (1,1)
_MAIN_GD = """\
extends Node2D


func _ready() -> void:
	var img := Image.create_empty(8, 4, false, Image.FORMAT_RGBA8)
	img.fill(Color.RED)
	var tex := ImageTexture.create_from_image(img)

	var sheet := Sprite2D.new()
	sheet.name = "Sheet"
	sheet.texture = tex
	sheet.hframes = 4
	sheet.vframes = 2
	sheet.frame = 5
	sheet.position = Vector2(100, 100)
	add_child(sheet)

	var frames := SpriteFrames.new()
	frames.add_frame("default", tex)
	var animated := AnimatedSprite2D.new()
	animated.name = "Animated"
	animated.sprite_frames = frames
	animated.position = Vector2(200, 100)
	add_child(animated)

	var banner := TextureRect.new()
	banner.name = "Banner"
	banner.texture = tex
	banner.flip_h = true
	add_child(banner)

	var plain := Node2D.new()
	plain.name = "Plain"
	add_child(plain)
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
def render_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """自建带 Sprite2D/AnimatedSprite2D/TextureRect 的最小项目，导入一次。"""
    proj = tmp_path_factory.mktemp("gcc_render")
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
def daemon(render_project: Path) -> Any:
    """module 级 daemon：本文件全部只读断言，无状态污染，起一次够用。"""
    start = _run_cli(render_project, "daemon", "start", "--headless", timeout=90)
    assert start["ok"] is True and start["result"]["started"], start
    assert _run_cli(render_project, "wait-node", "/root/Main/Sheet")["result"]["found"]
    try:
        yield render_project
    finally:
        _run_cli(render_project, "daemon", "stop", timeout=30)


def test_sprite_info_frame_grid(daemon: Any) -> None:
    """4x2 网格 frame=5 → frame_coords (1,1)、effective_region [2,2,2,2]。"""
    env = _run_cli(daemon, "sprite-info", "/root/Main/Sheet")
    assert env["ok"] is True, env
    info = env["result"]
    assert info["type"] == "Sprite2D"
    assert info["frame"] == 5
    assert info["frame_coords"] == [1, 1]
    assert info["hframes"] == 4 and info["vframes"] == 2
    assert info["effective_region"] == [2.0, 2.0, 2.0, 2.0]
    assert info["texture"]["size"] == [8, 4]
    # 运行时生成的贴图无资源路径 → null
    assert info["texture"]["path"] is None


def test_sprite_info_animated_frame_texture(daemon: Any) -> None:
    env = _run_cli(daemon, "sprite-info", "/root/Main/Animated")
    assert env["ok"] is True, env
    info = env["result"]
    assert info["type"] == "AnimatedSprite2D"
    assert info["animation"] == "default"
    assert info["frame_texture"]["size"] == [8, 4]


def test_sprite_info_texture_rect(daemon: Any) -> None:
    env = _run_cli(daemon, "sprite-info", "/root/Main/Banner")
    assert env["ok"] is True, env
    info = env["result"]
    assert info["type"] == "TextureRect"
    assert info["flip_h"] is True
    assert info["texture"]["size"] == [8, 4]


def test_sprite_info_unsupported_type_1010(daemon: Any) -> None:
    env = _run_cli(daemon, "sprite-info", "/root/Main/Plain")
    assert env["ok"] is False
    assert env["error"]["code"] == 1010


def test_sprite_info_missing_node_1001(daemon: Any) -> None:
    env = _run_cli(daemon, "sprite-info", "/root/Main/__missing__")
    assert env["ok"] is False
    assert env["error"]["code"] == 1001


def test_screenshot_node_errors_precede_capture(daemon: Any, tmp_path: Path) -> None:
    """--node 的 1001/1010 在取图之前快速返回（headless 下取图本身只会 1006）。"""
    out = tmp_path / "x.png"
    env = _run_cli(daemon, "screenshot", str(out), "--node", "/root/Main/__missing__")
    assert env["ok"] is False and env["error"]["code"] == 1001
    env = _run_cli(daemon, "screenshot", str(out), "--node", "/root/Main/Plain")
    assert env["ok"] is False and env["error"]["code"] == 1010
    assert not out.exists(), "错误路径不得写出文件"


def test_screenshot_node_headless_hits_1006_after_node_ok(daemon: Any, tmp_path: Path) -> None:
    """合法节点 + headless：node 校验通过后走取图 → dummy renderer 1006。

    （非 bug，是 headless 截图的既有契约；裁剪 happy path 由 GUI e2e 覆盖。）
    """
    out = tmp_path / "y.png"
    env = _run_cli(daemon, "screenshot", str(out), "--node", "/root/Main/Sheet")
    assert env["ok"] is False and env["error"]["code"] == 1006
