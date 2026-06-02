"""端到端 smoke：``--record`` 真起 Godot Movie Maker，产出有效 ``.mp4``（issue #78）。

录屏路径此前只有 mock 级覆盖（``test_daemon`` 拦 ``subprocess.Popen`` 断言 argv 含
``--write-movie/--fixed-fps``，没真起 Godot）。真实链路「Godot 写 ``.avi`` →
``daemon stop`` → ffmpeg 转 ``.mp4`` → 分辨率/时长有效」从未端到端验证过。

本测试在**能开窗的 runner** 上：
  1. ``daemon start --record --movie-path <tmp>.avi --fps N``（record ⇒ 自动 GUI）
  2. 跑几步输入 + ``wait-time`` 让画面推进若干帧
  3. ``daemon stop`` → 触发 ffmpeg 转码
  4. 断言 ``.mp4`` 存在且非空；``ffprobe`` 校验时长 > 0、分辨率与项目窗口一致

回归这条能拦的 bug：CultivationWorld#180（``--record --headless`` Movie Maker 首帧
SIGSEGV）的修复目前只有 mock 级保护。

依赖：真实 Godot 4 + 真实显示（``GCC_GUI_E2E=1``）+ ``ffmpeg``/``ffprobe`` 在 PATH。
**headless runner 跑不了录制（正是 #180 根因）**，故必须 gate 在有 display 的 job 上；
缺 display / ffmpeg 时 skip 并在 reason 写明原因（不静默跳过）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from godot_cli_control.daemon import find_godot_binary

_GODOT_BIN = find_godot_binary()
_ADDON_SRC = Path(__file__).resolve().parents[2] / "addons" / "godot_cli_control"

# 项目窗口尺寸（断言录像分辨率与之一致）。
_VIEW_W, _VIEW_H = 320, 240
_FPS = 15
_WAIT_SECONDS = 1.0  # 推进的 game-time → ≈ _FPS * _WAIT_SECONDS 帧

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(
        not _GODOT_BIN,
        reason="需要真实 Godot 4：把 godot 装进 PATH 或设 GODOT_BIN",
    ),
    pytest.mark.skipif(
        os.environ.get("GCC_GUI_E2E") != "1",
        reason="录屏 e2e 默认 skip（需真实显示 / xvfb）；CI 专档设 GCC_GUI_E2E=1 开启",
    ),
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
        reason="需要 ffmpeg + ffprobe 在 PATH 才能转码并校验录像",
    ),
]

_CLI = [sys.executable, "-m", "godot_cli_control"]

_PROJECT_GODOT = f"""\
config_version=5

[application]
config/name="gcc_e2e_record"
run/main_scene="res://main.tscn"
config/features=PackedStringArray("4.2")

[autoload]
GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"

[debug]
settings/stdout/print_fps=false

[display]
window/size/viewport_width={_VIEW_W}
window/size/viewport_height={_VIEW_H}

[editor_plugins]
enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")

[input]
jump={{"deadzone":0.5,"events":[]}}
"""

# 让 Bg 每帧平移，录像里有真实运动（而非单帧静止），更贴近「录 demo」的实际用途。
_MAIN_GD = """\
extends Control

func _process(delta: float) -> void:
	var bg := $Bg
	bg.position.x = fmod(bg.position.x + 80.0 * delta, 160.0)
"""

_MAIN_TSCN = """\
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://main.gd" id="1"]

[node name="Main" type="Control"]
layout_mode = 3
anchors_preset = 15
script = ExtResource("1")

[node name="Bg" type="ColorRect" parent="."]
offset_right = 80.0
offset_bottom = 240.0
color = Color(0.2, 0.4, 0.8, 1)
"""


def _run_cli(project: Path, *args: str, timeout: float = 120.0) -> dict[str, Any]:
    proc = subprocess.run(
        _CLI + list(args), cwd=project, capture_output=True, text=True, timeout=timeout
    )
    json_lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert json_lines, f"无 JSON 输出：args={args} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return json.loads(json_lines[-1])


def _ffprobe(mp4: Path) -> dict[str, Any]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json", str(mp4),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"ffprobe 失败：{out.stderr}"
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def godot_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    proj = tmp_path_factory.mktemp("gcc_e2e_record")
    (proj / "addons").mkdir()
    shutil.copytree(_ADDON_SRC, proj / "addons" / "godot_cli_control")
    (proj / "project.godot").write_text(_PROJECT_GODOT)
    (proj / "main.gd").write_text(_MAIN_GD)
    (proj / "main.tscn").write_text(_MAIN_TSCN)

    imp = subprocess.run(
        [_GODOT_BIN, "--headless", "--editor", "--quit", "--path", str(proj)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert imp.returncode == 0, f"Godot 导入失败：{imp.stdout}\n{imp.stderr}"
    return proj


def test_record_produces_valid_mp4(godot_project: Path) -> None:
    """--record → 推进若干帧 → daemon stop 转码 → 有效 .mp4（时长 > 0、分辨率匹配）。"""
    project = godot_project
    avi = project / "demo.avi"
    mp4 = avi.with_suffix(".mp4")

    # record ⇒ 自动 GUI（即便本进程非 TTY）。movie-path 用 .avi（Godot Movie Maker
    # 内置 MJPEG-AVI 写出），stop 时 ffmpeg 转成 .mp4。
    start = _run_cli(
        project, "daemon", "start",
        "--record", "--movie-path", str(avi), "--fps", str(_FPS),
        timeout=120,
    )
    assert start["ok"] is True and start["result"]["started"], start

    stopped_cleanly = False
    try:
        # 跑一步输入 + 推进 game-time，让 Movie Maker 写出若干帧。
        _run_cli(project, "tap", "jump", "0.1")
        wt = _run_cli(project, "wait-time", str(_WAIT_SECONDS))
        assert wt["ok"] is True, wt
    finally:
        stop = _run_cli(project, "daemon", "stop", timeout=60)
        stopped_cleanly = True
        # rc 0 = 转码成功；rc 2 = daemon 已停但 ffmpeg 转码失败（保留 .avi）。
        assert stop["ok"] is True, stop
        assert stop["result"].get("rc") == 0, (
            f"daemon stop 报转码失败 rc={stop['result'].get('rc')}；"
            f"看 .cli_control/ffmpeg.log。原始 .avi 是否存在：{avi.exists()}"
        )

    assert stopped_cleanly
    assert mp4.exists(), f"转码后未见 {mp4}（.avi 存在={avi.exists()}）"
    assert mp4.stat().st_size > 0, "转码出的 .mp4 为空"

    probe = _ffprobe(mp4)
    duration = float(probe.get("format", {}).get("duration", 0.0))
    assert duration > 0.0, f"录像时长应 > 0：{probe}"

    stream = probe.get("streams", [{}])[0]
    assert (stream.get("width"), stream.get("height")) == (_VIEW_W, _VIEW_H), (
        f"录像分辨率应与项目窗口 {_VIEW_W}x{_VIEW_H} 一致，实际 "
        f"{stream.get('width')}x{stream.get('height')}：{probe}"
    )
