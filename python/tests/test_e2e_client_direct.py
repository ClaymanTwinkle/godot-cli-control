"""端到端回归：直连 ``GameClient`` 真实 daemon，覆盖 client.py 的 RPC 方法体（issue #81）。

为什么单开这条：既有 e2e（``test_e2e_input`` 等）经 ``subprocess.run([python, -m
godot_cli_control, ...])`` 调 CLI，``coverage run`` 默认不统计子进程，所以真实
WebSocket 链路虽然跑了、``client.py`` 的 ``set_property`` / ``call_method`` /
``get_children`` / ``wait_for_node`` 等方法体却没计入覆盖率。本文件**在 pytest 进程内**
``async with GameClient(port=...)`` 直接 await 这些方法打真 daemon —— 方法体在被
trace 的进程里执行，覆盖率如实计入。下游 CultivationWorld 依赖这些方法，回归不能只靠
mock 级单测。

需要真实 Godot 4：PATH 里有 ``godot``（或设 ``GODOT_BIN``），否则整文件 skip。
截图走 GUI 路径（headless dummy renderer 拿不到 viewport texture），不在此覆盖——
见 ``test_e2e_screenshot_gui.py``。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from godot_cli_control.client import GameClient, RpcError
from godot_cli_control.daemon import find_godot_binary

_GODOT_BIN = find_godot_binary()
_ADDON_SRC = Path(__file__).resolve().parents[2] / "addons" / "godot_cli_control"

pytestmark = pytest.mark.skipif(
    not _GODOT_BIN,
    reason="需要真实 Godot 4：把 godot 装进 PATH 或设 GODOT_BIN，否则整文件 skip",
)

_CLI = [sys.executable, "-m", "godot_cli_control"]

_PROJECT_GODOT = """\
config_version=5

[application]
config/name="gcc_e2e_client"
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

# Main 挂一个脚本暴露可读写属性 counter 与用户方法 bump —— 给 set_property /
# call_method 一个不撞 blacklist（script/set/call 等被禁）的合法目标。
_MAIN_GD = """\
extends Node

var counter: int = 0

func bump(n: int) -> int:
	counter += n
	return counter
"""

_MAIN_TSCN = """\
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://main.gd" id="1"]

[node name="Main" type="Node"]
script = ExtResource("1")

[node name="Title" type="Label" parent="."]
text = "Hello"

[node name="Player" type="Node2D" parent="."]

[node name="Panel" type="Control" parent="."]
"""


def _run_cli(project: Path, *args: str, timeout: float = 60.0) -> dict[str, Any]:
    proc = subprocess.run(
        _CLI + list(args), cwd=project, capture_output=True, text=True, timeout=timeout
    )
    json_lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert json_lines, f"无 JSON 输出：args={args} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return json.loads(json_lines[-1])


@pytest.fixture(scope="module")
def godot_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    proj = tmp_path_factory.mktemp("gcc_e2e_client")
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


@pytest.fixture(scope="module")
def daemon_port(godot_project: Path) -> Any:
    start = _run_cli(godot_project, "daemon", "start", "--headless", timeout=90)
    assert start["ok"] is True and start["result"]["started"], start
    try:
        yield godot_project, start["result"]["port"]
    finally:
        _run_cli(godot_project, "release-all")
        _run_cli(godot_project, "daemon", "stop", timeout=30)


@pytest.mark.asyncio
async def test_client_read_methods(daemon_port: Any) -> None:
    """读路径：node_exists / is_visible / get_text / get_property / get_children /
    get_scene_tree / wait_for_node（命中 + 超时）—— 直连真实 daemon。"""
    _, port = daemon_port
    async with GameClient(port=port) as client:
        assert await client.node_exists("/root/Main") is True
        assert await client.node_exists("/root/Main/DoesNotExist") is False

        assert await client.is_visible("/root/Main/Panel") is True

        assert await client.get_text("/root/Main/Title") == "Hello"

        assert await client.get_property("/root/Main", "counter") == 0

        children = await client.get_children("/root/Main")
        names = {c.get("name") for c in children}
        assert {"Title", "Player", "Panel"} <= names, children

        # 类型过滤：只要 Label
        labels = await client.get_children("/root/Main", type_filter="Label")
        assert [c.get("name") for c in labels] == ["Title"], labels

        tree = await client.get_scene_tree(depth=3)
        assert isinstance(tree, dict) and tree, tree

        assert await client.wait_for_node("/root/Main/Player", timeout=2.0) is True
        assert await client.wait_for_node("/root/Main/Ghost", timeout=0.5) is False


@pytest.mark.asyncio
async def test_client_write_and_call_methods(daemon_port: Any) -> None:
    """写路径：set_property 落值可被 get_property 读回；call_method 调用户方法拿返回值。"""
    _, port = daemon_port
    async with GameClient(port=port) as client:
        await client.set_property("/root/Main", "counter", 7)
        assert await client.get_property("/root/Main", "counter") == 7

        # 用户方法 bump(n) 不在 method blacklist（call/set/free 才禁）：counter 7 + 5 = 12
        assert await client.call_method("/root/Main", "bump", [5]) == 12
        assert await client.get_property("/root/Main", "counter") == 12

        # 写非法属性（script 在 property blacklist）应抛 RpcError，错误码保留
        with pytest.raises(RpcError):
            await client.set_property("/root/Main", "script", "res://evil.gd")


@pytest.mark.asyncio
async def test_client_input_methods(daemon_port: Any) -> None:
    """输入路径：list_input_actions / press / get_pressed / release / tap / hold /
    combo / combo_cancel / release_all —— 全部直连真实模拟器。"""
    _, port = daemon_port
    async with GameClient(port=port) as client:
        try:
            actions = await client.list_input_actions(include_builtin=False)
            assert {"move_right", "jump"} <= set(actions), actions

            await client.action_press("jump")
            assert "jump" in await client.get_pressed()
            await client.action_release("jump")
            assert "jump" not in await client.get_pressed()

            await client.action_tap("jump", 0.05)

            await client.hold("move_right", 0.2)
            assert "move_right" in await client.get_pressed()

            # combo 串行跑完后返回；随后 combo_cancel 无 combo 在跑 → 安全 no-op success
            combo_res = await client.combo([{"action": "jump", "duration": 0.05}])
            assert combo_res.get("success") is True, combo_res
            cancel_res = await client.combo_cancel()
            assert cancel_res.get("success") is True, cancel_res
        finally:
            await client.release_all()
            assert await client.get_pressed() == []


@pytest.mark.asyncio
async def test_client_autodiscovers_port_from_control_dir(
    daemon_port: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 port 参数的 GameClient() 从 cwd 的 .cli_control/port auto-discover（issue #91）。

    daemon 已写 .cli_control/port；chdir 到项目根后 GameClient()（无参）应据此连上，
    覆盖 client.GameClient.__init__ 的 discover_port 分支。"""
    project, _ = daemon_port
    monkeypatch.chdir(project)
    async with GameClient() as client:  # 无 port → auto-discover
        assert await client.node_exists("/root/Main") is True


@pytest.mark.asyncio
async def test_client_get_properties_multi(daemon_port: Any) -> None:
    """client.get_properties(path, props) 原子读——返回 dict，key 齐，每项是裸 value
    （不含 type 字段）。覆盖 client.py get_properties 方法体（issue #100 direct-connect）。"""
    _, port = daemon_port
    async with GameClient(port=port) as client:
        result = await client.get_properties("/root/Main", ["counter", "name"])
        assert isinstance(result, dict), result
        # 两个 key 都在
        assert "counter" in result, result
        assert "name" in result, result
        # Python API 只给裸 value（不含 type），counter 是 int，name 是 str
        assert isinstance(result["counter"], int), result
        assert isinstance(result["name"], str), result


@pytest.mark.asyncio
async def test_client_request_get_property_has_type_field(daemon_port: Any) -> None:
    """client.request('get_property', {...}) 对 Node2D.position 返回带 type 字段的对象。

    Python API (get_property) 只给裸 value；要拿 type + value 必须走底层 request()。
    这里直接断言 shape: {"value": [x, y], "type": "Vector2"}。
    覆盖 issue #99 的服务端编码 + client.request 直通路径。"""
    _, port = daemon_port
    async with GameClient(port=port) as client:
        result = await client.request(
            "get_property", {"path": "/root/Main/Player", "property": "position"}
        )
        assert isinstance(result, dict), result
        # value 是 list（Vector2 编码为数组）
        assert "value" in result, result
        assert isinstance(result["value"], list), result
        assert len(result["value"]) == 2, result
        # type 字段存在且是 "Vector2"
        assert result.get("type") == "Vector2", result


@pytest.mark.asyncio
async def test_client_wait_frames(daemon_port: Any) -> None:
    """client.wait_frames(2) 返回 {"success": True, "frames": 2}（issue #96 直连覆盖）。"""
    _, port = daemon_port
    async with GameClient(port=port) as client:
        result = await client.wait_frames(2)
        assert isinstance(result, dict), result
        assert result.get("success") is True, result
        assert result.get("frames") == 2, result


@pytest.mark.asyncio
async def test_client_wait_property_immediate_match_and_timeout(daemon_port: Any) -> None:
    """wait_property 直连测试：立即命中路径 + 超时路径（issue #96）。

    先 set_property 一个已知值，再 wait eq → matched=True（立即命中）。
    然后等一个永远不成立的条件 timeout=0.5 → matched=False, reason='timeout'。
    """
    _, port = daemon_port
    async with GameClient(port=port) as client:
        # 写 counter = 42，然后立即等 counter == 42 → 应立即命中
        await client.set_property("/root/Main", "counter", 42)
        result_match = await client.wait_property(
            "/root/Main", "counter", 42, op="eq", timeout=2.0
        )
        assert isinstance(result_match, dict), result_match
        assert result_match.get("matched") is True, result_match

        # 等 counter > 9999（永远不成立）超时
        result_timeout = await client.wait_property(
            "/root/Main", "counter", 9999, op="gt", timeout=0.5
        )
        assert isinstance(result_timeout, dict), result_timeout
        assert result_timeout.get("matched") is False, result_timeout
        assert result_timeout.get("reason") == "timeout", result_timeout


@pytest.mark.asyncio
async def test_client_wait_signal_timeout_path(daemon_port: Any) -> None:
    """wait_signal 超时路径：等一个不会发射的内置信号 renamed（issue #96）。

    emitted=False 且超时时间合理（0.5s）。
    """
    _, port = daemon_port
    async with GameClient(port=port) as client:
        result = await client.wait_signal("/root/Main/Player", "renamed", timeout=0.5)
        assert isinstance(result, dict), result
        assert result.get("emitted") is False, result
