"""单元测试：GameBridge 同步封装层。

目标：验证 bridge.py 把每个公开方法忠实地路由到 GameClient 的对应 async 方法、
参数透传、返回值透传、异常透传。不起真 websocket，整个 GameClient 替换成桩
（``_StubClient``）记录每次调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from godot_cli_control import bridge as bridge_mod
from godot_cli_control.bridge import GameBridge
from godot_cli_control.client import DEFAULT_PORT


class _StubClient:
    """替代 GameClient 的桩。所有 async 方法把调用记录到 ``calls`` 并返回预置值。"""

    def __init__(self, port: int = DEFAULT_PORT, instance: str | None = None) -> None:
        self.port = port
        self.instance = instance
        self.calls: list[tuple[str, tuple, dict]] = []
        # 每个方法的预置返回值；默认 None
        self.returns: dict[str, Any] = {}
        # 抛错配置：方法名 → 异常实例
        self.raises: dict[str, BaseException] = {}
        self.connect_kwargs: dict | None = None
        self.disconnected = False

    def _record(self, name: str, args: tuple, kwargs: dict) -> Any:
        self.calls.append((name, args, kwargs))
        if name in self.raises:
            raise self.raises[name]
        return self.returns.get(name)

    async def connect(self, **kwargs: Any) -> None:
        self.connect_kwargs = kwargs

    async def disconnect(self) -> None:
        self.disconnected = True

    # 方法签名与 GameClient 对齐 —— 只保留 GameBridge 实际调用的方法
    async def wait_game_time(self, seconds: float) -> dict:
        return self._record("wait_game_time", (seconds,), {})

    async def get_scene_tree(
        self, depth: int = 5, max_nodes: int | None = None, path: str | None = None
    ) -> dict:
        kwargs: dict = {"depth": depth}
        if max_nodes is not None:
            kwargs["max_nodes"] = max_nodes
        if path is not None:
            kwargs["path"] = path
        return self._record("get_scene_tree", (), kwargs)

    async def node_exists(self, path: str) -> bool:
        return self._record("node_exists", (path,), {})

    async def is_visible(self, path: str) -> bool:
        return self._record("is_visible", (path,), {})

    async def get_text(self, path: str) -> str:
        return self._record("get_text", (path,), {})

    async def wait_for_node(self, path: str, timeout: float = 5.0) -> bool:
        return self._record("wait_for_node", (path,), {"timeout": timeout})

    async def click(self, path: str) -> dict:
        return self._record("click", (path,), {})

    async def hold(self, action: str, duration: float) -> dict:
        return self._record("hold", (action, duration), {})

    async def action_tap(self, action: str, duration: float = 0.1) -> dict:
        return self._record("action_tap", (action,), {"duration": duration})

    async def action_press(self, action: str) -> dict:
        return self._record("action_press", (action,), {})

    async def action_release(self, action: str) -> dict:
        return self._record("action_release", (action,), {})

    async def release_all(self) -> dict:
        return self._record("release_all", (), {})

    async def combo(self, steps: list[dict]) -> dict:
        return self._record("combo", (steps,), {})

    async def combo_cancel(self) -> dict:
        return self._record("combo_cancel", (), {})

    async def get_pressed(self) -> list[str]:
        return self._record("get_pressed", (), {})

    async def list_input_actions(self, include_builtin: bool = False) -> list[str]:
        return self._record("list_input_actions", (include_builtin,), {})

    async def screenshot(self, node: str | None = None) -> bytes:
        return self._record("screenshot", (), {"node": node})

    async def screenshot_raw(
        self, node: str | None = None, path: str | None = None
    ) -> dict:
        return self._record("screenshot_raw", (), {"node": node, "path": path})

    async def sprite_info(self, path: str) -> dict:
        return self._record("sprite_info", (path,), {})

    async def errors(self, since: int = 0, limit: int = 100) -> dict:
        return self._record("errors", (), {"since": since, "limit": limit})

    async def get_property(self, path: str, prop: str) -> Any:
        return self._record("get_property", (path, prop), {})

    async def get_properties(self, path: str, props: list) -> dict:
        return self._record("get_properties", (path, props), {})

    async def set_property(self, path: str, prop: str, value: Any) -> dict:
        return self._record("set_property", (path, prop, value), {})

    async def call_method(
        self, path: str, method: str, args: list | None = None
    ) -> Any:
        return self._record("call_method", (path, method, args), {})

    async def get_children(self, path: str, type_filter: str = "") -> list[dict]:
        return self._record("get_children", (path,), {"type_filter": type_filter})

    async def wait_property(
        self,
        path: str,
        prop: str,
        value: Any,
        op: str = "eq",
        timeout: float = 5.0,
        tolerance: float = 0.0,
    ) -> dict:
        return self._record("wait_property", (path, prop, value), {"op": op, "timeout": timeout, "tolerance": tolerance})

    async def wait_signal(self, path: str, signal: str, timeout: float = 5.0) -> dict:
        return self._record("wait_signal", (path, signal), {"timeout": timeout})

    async def wait_frames(self, frames: int, physics: bool = False) -> dict:
        return self._record("wait_frames", (frames,), {"physics": physics})

    async def scene_reload(self, timeout: float = 10.0) -> dict:
        return self._record("scene_reload", (), {"timeout": timeout})

    async def scene_change(self, path: str, timeout: float = 10.0) -> dict:
        return self._record("scene_change", (path,), {"timeout": timeout})

    async def time_scale(self, value: float | None = None) -> dict:
        return self._record("time_scale", (value,), {})

    async def pause(self) -> dict:
        return self._record("pause", (), {})

    async def unpause(self) -> dict:
        return self._record("unpause", (), {})

    async def step_frames(self, frames: int, physics: bool = False) -> dict:
        return self._record("step_frames", (frames,), {"physics": physics})


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    """patch 模块级 GameClient 符号，让 GameBridge() 实例化时拿到桩。"""
    holder: dict[str, _StubClient] = {}

    def _factory(port: int = DEFAULT_PORT, instance: str | None = None) -> _StubClient:
        # instance 参数接入后 GameBridge 会透传过来，桩需接收以避免 TypeError
        c = _StubClient(port=port, instance=instance)
        holder["client"] = c
        return c

    monkeypatch.setattr(bridge_mod, "GameClient", _factory)
    # GameBridge 实例化才会调 _factory；先把 holder 暴露给测试
    holder["client"] = _StubClient()  # 占位，实例化后会被覆盖
    return holder  # type: ignore[return-value]


def _make_bridge(holder: dict, port: int = DEFAULT_PORT) -> tuple[GameBridge, _StubClient]:
    b = GameBridge(port=port)
    return b, holder["client"]


# ── 构造与连接 ──


def test_init_passes_port_to_client(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client, port=12345)
    assert c.port == 12345
    b.close()


def test_init_calls_connect_with_documented_retry_params(stub_client: dict) -> None:
    """构造函数对 connect 的参数是合同 —— 改默认值需要同步改 daemon 启动等待。"""
    b, c = _make_bridge(stub_client)
    assert c.connect_kwargs == {
        "retries": 15,
        "backoff": 1.0,
        "total_timeout": 60.0,
    }
    b.close()


def test_close_disconnects_and_closes_loop(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    loop = b._loop
    b.close()
    assert c.disconnected is True
    assert loop.is_closed()


# ── 等待 / 场景树 ──


def test_wait_uses_game_time_not_wall_time(stub_client: dict) -> None:
    """wait(seconds) 必须走 wait_game_time —— Movie Maker 模式下两者差 2-3×。"""
    b, c = _make_bridge(stub_client)
    c.returns["wait_game_time"] = {"success": True}
    b.wait(2.5)
    assert c.calls[-1] == ("wait_game_time", (2.5,), {})
    b.close()


def test_wait_game_time_is_alias_for_wait(stub_client: dict) -> None:
    """issue #60: bridge.wait_game_time 必须存在并与 bridge.wait 行为一致。"""
    b, c = _make_bridge(stub_client)
    c.returns["wait_game_time"] = {"success": True}
    b.wait_game_time(1.25)
    assert c.calls[-1] == ("wait_game_time", (1.25,), {})
    b.close()


def test_tree_default_depth_3_not_client_default_5(stub_client: dict) -> None:
    """bridge.tree() 默认 depth=3，比 client 默认 5 浅，避免大场景树阻塞。"""
    b, c = _make_bridge(stub_client)
    c.returns["get_scene_tree"] = {"name": "root"}
    result = b.tree()
    assert c.calls[-1] == ("get_scene_tree", (), {"depth": 3})
    assert result == {"name": "root"}
    b.close()


def test_tree_explicit_depth(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["get_scene_tree"] = {}
    b.tree(depth=10)
    assert c.calls[-1][2]["depth"] == 10
    b.close()


def test_tree_forwards_max_nodes_to_client(stub_client: dict) -> None:
    """bridge.tree(max_nodes=N) 必须把 N 透传到 GameClient（防 stub 漏带导致假绿）。"""
    b, c = _make_bridge(stub_client)
    c.returns["get_scene_tree"] = {}
    b.tree(max_nodes=50)
    assert c.calls[-1] == ("get_scene_tree", (), {"depth": 3, "max_nodes": 50})
    b.close()


def test_tree_forwards_path_to_client(stub_client: dict) -> None:
    """issue #150：bridge.tree(path=...) 必须把 path 透传到 GameClient。"""
    b, c = _make_bridge(stub_client)
    c.returns["get_scene_tree"] = {}
    b.tree(path="/root/GameUI")
    assert c.calls[-1] == ("get_scene_tree", (), {"depth": 3, "path": "/root/GameUI"})
    b.close()


def test_get_scene_tree_is_alias_for_tree(stub_client: dict) -> None:
    """issue #60: bridge.get_scene_tree 必须存在并与 bridge.tree 行为一致，
    包括默认 depth=3（比 client 默认 5 浅）+ max_nodes 透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["get_scene_tree"] = {"name": "root"}
    result = b.get_scene_tree()
    assert c.calls[-1] == ("get_scene_tree", (), {"depth": 3})
    assert result == {"name": "root"}
    b.get_scene_tree(depth=7, max_nodes=99)
    assert c.calls[-1] == ("get_scene_tree", (), {"depth": 7, "max_nodes": 99})
    b.close()


def test_node_exists_returns_value(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["node_exists"] = True
    assert b.node_exists("/root/X") is True
    c.returns["node_exists"] = False
    assert b.node_exists("/root/Y") is False
    b.close()


def test_is_visible_returns_value(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["is_visible"] = True
    assert b.is_visible("/root/X") is True
    b.close()


def test_get_text_returns_value(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["get_text"] = "hello"
    assert b.get_text("/root/Label") == "hello"
    b.close()


def test_wait_for_node_passes_timeout(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["wait_for_node"] = True
    b.wait_for_node("/root/X", timeout=2.5)
    assert c.calls[-1] == ("wait_for_node", ("/root/X",), {"timeout": 2.5})
    b.close()


def test_wait_for_node_default_timeout_5(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["wait_for_node"] = False
    b.wait_for_node("/root/X")
    assert c.calls[-1][2]["timeout"] == 5.0
    b.close()


# ── UI 交互 ──


def test_click_passes_path(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["click"] = {"ok": True}
    result = b.click("/root/Btn")
    assert c.calls[-1] == ("click", ("/root/Btn",), {})
    assert result == {"ok": True}
    b.close()


# ── 输入模拟 ──


def test_hold_passes_action_and_duration(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    b.hold("run", 1.5)
    assert c.calls[-1] == ("hold", ("run", 1.5), {})
    b.close()


def test_tap_default_duration_is_0_1(stub_client: dict) -> None:
    """tap 默认 0.1s，对应 GD 端 input_action_tap 的 default。"""
    b, c = _make_bridge(stub_client)
    b.tap("attack")
    assert c.calls[-1] == ("action_tap", ("attack",), {"duration": 0.1})
    b.close()


def test_tap_explicit_duration(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    b.tap("attack", duration=0.3)
    assert c.calls[-1][2]["duration"] == 0.3
    b.close()


def test_action_tap_is_alias_for_tap(stub_client: dict) -> None:
    """issue #58: bridge.action_tap 必须存在并与 bridge.tap 行为一致，
    保证 README 表格 + run --help 里"方法名一致"的承诺成立。"""
    b, c = _make_bridge(stub_client)
    b.action_tap("attack")
    assert c.calls[-1] == ("action_tap", ("attack",), {"duration": 0.1})
    b.action_tap("attack", duration=0.25)
    assert c.calls[-1] == ("action_tap", ("attack",), {"duration": 0.25})
    b.close()


def test_action_press_release(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    b.action_press("jump")
    assert c.calls[-1] == ("action_press", ("jump",), {})
    b.action_release("jump")
    assert c.calls[-1] == ("action_release", ("jump",), {})
    b.close()


def test_release_all(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    b.release_all()
    assert c.calls[-1] == ("release_all", (), {})
    b.close()


def test_combo_passes_steps(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    steps = [{"action": "a", "duration": 0.1}, {"wait": 0.2}]
    c.returns["combo"] = {"completed": True}
    result = b.combo(steps)
    assert c.calls[-1] == ("combo", (steps,), {})
    assert result == {"completed": True}
    b.close()


def test_combo_cancel(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["combo_cancel"] = {"cancelled": True}
    assert b.combo_cancel() == {"cancelled": True}
    assert c.calls[-1] == ("combo_cancel", (), {})
    b.close()


def test_get_pressed_returns_list(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["get_pressed"] = ["jump", "attack"]
    assert b.get_pressed() == ["jump", "attack"]
    b.close()


def test_list_input_actions_default_filters_builtin(stub_client: dict) -> None:
    """默认 include_builtin=False —— 与 client 默认对齐。"""
    b, c = _make_bridge(stub_client)
    c.returns["list_input_actions"] = ["jump"]
    b.list_input_actions()
    assert c.calls[-1] == ("list_input_actions", (False,), {})
    b.close()


def test_list_input_actions_include_builtin_true(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["list_input_actions"] = ["ui_accept", "jump"]
    b.list_input_actions(include_builtin=True)
    assert c.calls[-1] == ("list_input_actions", (True,), {})
    b.close()


# ── 截图 ──


def test_screenshot_no_path_returns_bytes(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["screenshot"] = b"\x89PNG\r\n"
    data = b.screenshot()
    assert data == b"\x89PNG\r\n"
    b.close()


def test_screenshot_with_path_server_side_write(
    stub_client: dict, tmp_path: Path
) -> None:
    """screenshot(path=...) 走 daemon 直写（issue #149）：绝对路径递给
    screenshot_raw，bytes 从落盘文件读回，base64 不过 WS。"""
    b, c = _make_bridge(stub_client)
    out = tmp_path / "subdir" / "shot.png"
    # 模拟新版 addon：daemon 进程已把 PNG 写到目标路径
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\x89PNG\r\nserver")
    c.returns["screenshot_raw"] = {"path": str(out.resolve()), "bytes": 12}
    data = b.screenshot(str(out), node="/root/Game/Sprite")
    assert data == b"\x89PNG\r\nserver"
    assert c.calls[-1] == (
        "screenshot_raw",
        (),
        {"node": "/root/Game/Sprite", "path": str(out.resolve())},
    )
    b.close()


def test_screenshot_with_path_legacy_base64_fallback(
    stub_client: dict, tmp_path: Path
) -> None:
    """旧 addon 不认 path 参数、照旧回 base64：bridge 本地解码落盘，返回值
    与父目录自动创建契约不变（issue #149 优雅降级）。"""
    import base64 as _b64

    b, c = _make_bridge(stub_client)
    c.returns["screenshot_raw"] = {
        "image": _b64.b64encode(b"\x89PNG\r\nfake").decode()
    }
    out = tmp_path / "subdir" / "shot.png"
    data = b.screenshot(str(out))
    assert data == b"\x89PNG\r\nfake"
    assert out.read_bytes() == b"\x89PNG\r\nfake"
    # 父目录自动创建
    assert out.parent.is_dir()
    b.close()


def test_screenshot_node_passes_through(stub_client: dict) -> None:
    """screenshot(node=...) 把节点路径透传给 client（issue #101）。"""
    b, c = _make_bridge(stub_client)
    c.returns["screenshot"] = b"\x89PNG"
    b.screenshot(node="/root/Game/Sprite")
    assert c.calls[-1] == ("screenshot", (), {"node": "/root/Game/Sprite"})
    b.close()


def test_sprite_info_returns_aggregate(stub_client: dict) -> None:
    """sprite_info 透传路径并原样返回聚合 dict（issue #101）。"""
    b, c = _make_bridge(stub_client)
    c.returns["sprite_info"] = {"type": "Sprite2D", "frame": 3}
    info = b.sprite_info("/root/Game/Sprite")
    assert info == {"type": "Sprite2D", "frame": 3}
    assert c.calls[-1] == ("sprite_info", ("/root/Game/Sprite",), {})
    b.close()


# ── 属性读写 / 方法调用 ──


def test_get_property(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["get_property"] = 42
    assert b.get_property("/root/X", "value") == 42
    assert c.calls[-1] == ("get_property", ("/root/X", "value"), {})
    b.close()


def test_get_properties_delegates_to_client(stub_client: dict) -> None:
    """``bridge.get_properties`` 必须委托到 ``client.get_properties``，参数透传，返回值透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["get_properties"] = {"position": [1, 2], "visible": True}
    result = b.get_properties("/root/Player", ["position", "visible"])
    assert c.calls[-1] == ("get_properties", ("/root/Player", ["position", "visible"]), {})
    assert result == {"position": [1, 2], "visible": True}
    b.close()


def test_set_property_passes_value(stub_client: dict) -> None:
    """set_property 必须按位置传 (path, prop, value) —— 三个都不能漏。"""
    b, c = _make_bridge(stub_client)
    b.set_property("/root/X", "modulate", [1.0, 0.5, 0.0, 1.0])
    assert c.calls[-1] == (
        "set_property",
        ("/root/X", "modulate", [1.0, 0.5, 0.0, 1.0]),
        {},
    )
    b.close()


def test_call_method_default_args_none(stub_client: dict) -> None:
    """call_method 默认 args=None —— 由 client 内部转空 list。"""
    b, c = _make_bridge(stub_client)
    c.returns["call_method"] = "ok"
    result = b.call_method("/root/X", "do_thing")
    assert c.calls[-1] == ("call_method", ("/root/X", "do_thing", None), {})
    assert result == "ok"
    b.close()


def test_call_method_with_args(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["call_method"] = 7
    b.call_method("/root/X", "add", [3, 4])
    assert c.calls[-1] == ("call_method", ("/root/X", "add", [3, 4]), {})
    b.close()


def test_get_children_default_filter_empty(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["get_children"] = [{"name": "A"}]
    b.get_children("/root")
    assert c.calls[-1] == ("get_children", ("/root",), {"type_filter": ""})
    b.close()


def test_get_children_with_type_filter(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["get_children"] = [{"name": "Btn"}]
    b.get_children("/root", type_filter="Button")
    assert c.calls[-1][2]["type_filter"] == "Button"
    b.close()


# ── 异常透传 ──


def test_client_exception_propagates(stub_client: dict) -> None:
    """client.click 抛 RpcError → bridge.click 必须透出同一异常，不被吞。"""
    from godot_cli_control.client import RpcError

    b, c = _make_bridge(stub_client)
    c.raises["click"] = RpcError(1001, "Node not found")
    with pytest.raises(RpcError) as exc_info:
        b.click("/missing")
    assert exc_info.value.code == 1001
    assert exc_info.value.message == "Node not found"
    b.close()


def test_runtime_error_propagates(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.raises["screenshot"] = RuntimeError("disconnected")
    with pytest.raises(RuntimeError, match="disconnected"):
        b.screenshot()
    b.close()


# ── issue #96: wait_property / wait_signal / wait_frames bridge 委托 ──


def test_wait_property_delegates_all_params(stub_client: dict) -> None:
    """bridge.wait_property 必须把所有参数（含 op/tolerance）委托给 client.wait_property。"""
    b, c = _make_bridge(stub_client)
    c.returns["wait_property"] = {"matched": True, "value": 10, "waited": 0.05}
    result = b.wait_property("/root/Player", "health", 100, op="ge", timeout=3.0, tolerance=0.1)
    assert c.calls[-1] == (
        "wait_property",
        ("/root/Player", "health", 100),
        {"op": "ge", "timeout": 3.0, "tolerance": 0.1},
    )
    assert result == {"matched": True, "value": 10, "waited": 0.05}
    b.close()


def test_wait_signal_delegates_params(stub_client: dict) -> None:
    """bridge.wait_signal 把 path/signal/timeout 委托给 client.wait_signal。"""
    b, c = _make_bridge(stub_client)
    c.returns["wait_signal"] = {"emitted": True, "args": [42]}
    result = b.wait_signal("/root/Area", "door_opened", timeout=2.0)
    assert c.calls[-1] == (
        "wait_signal",
        ("/root/Area", "door_opened"),
        {"timeout": 2.0},
    )
    assert result == {"emitted": True, "args": [42]}
    b.close()


def test_wait_frames_delegates_params(stub_client: dict) -> None:
    """bridge.wait_frames 把 frames/physics 委托给 client.wait_frames。"""
    b, c = _make_bridge(stub_client)
    c.returns["wait_frames"] = {"success": True, "frames": 5}
    result = b.wait_frames(5, physics=True)
    assert c.calls[-1] == (
        "wait_frames",
        (5,),
        {"physics": True},
    )
    assert result == {"success": True, "frames": 5}
    b.close()


# ── issue #98: scene_reload / scene_change bridge 委托 ──


def test_scene_reload_delegates_to_client(stub_client: dict) -> None:
    """bridge.scene_reload() 委托给 client.scene_reload，返回值透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["scene_reload"] = {"scene_path": "res://Main.tscn", "name": "Main"}
    result = b.scene_reload()
    assert c.calls[-1] == ("scene_reload", (), {"timeout": 10.0})
    assert result == {"scene_path": "res://Main.tscn", "name": "Main"}
    b.close()


def test_scene_reload_custom_timeout(stub_client: dict) -> None:
    """bridge.scene_reload(timeout=3.0) 把 timeout 透传到 client。"""
    b, c = _make_bridge(stub_client)
    c.returns["scene_reload"] = {"scene_path": "res://Main.tscn", "name": "Main"}
    b.scene_reload(timeout=3.0)
    assert c.calls[-1] == ("scene_reload", (), {"timeout": 3.0})
    b.close()


def test_scene_change_delegates_to_client(stub_client: dict) -> None:
    """bridge.scene_change(path) 委托给 client.scene_change，参数透传，返回值透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["scene_change"] = {"scene_path": "res://a.tscn", "name": "A"}
    result = b.scene_change("res://a.tscn")
    assert c.calls[-1] == ("scene_change", ("res://a.tscn",), {"timeout": 10.0})
    assert result == {"scene_path": "res://a.tscn", "name": "A"}
    b.close()


def test_scene_change_custom_timeout(stub_client: dict) -> None:
    """bridge.scene_change(path, timeout=5.0) 把所有参数透传到 client。"""
    b, c = _make_bridge(stub_client)
    c.returns["scene_change"] = {"scene_path": "res://b.tscn", "name": "B"}
    b.scene_change("res://b.tscn", timeout=5.0)
    assert c.calls[-1] == ("scene_change", ("res://b.tscn",), {"timeout": 5.0})
    b.close()


# ── issue #102: time_scale / pause / unpause / step_frames bridge 委托 ──


def test_time_scale_no_arg_delegates_to_client(stub_client: dict) -> None:
    """bridge.time_scale() 无参时委托 client.time_scale(None)，返回值透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["time_scale"] = {"time_scale": 1.0}
    result = b.time_scale()
    assert c.calls[-1] == ("time_scale", (None,), {})
    assert result == {"time_scale": 1.0}
    b.close()


def test_time_scale_with_value_delegates_to_client(stub_client: dict) -> None:
    """bridge.time_scale(2.5) 委托 client.time_scale(2.5)，返回值透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["time_scale"] = {"time_scale": 2.5}
    result = b.time_scale(2.5)
    assert c.calls[-1] == ("time_scale", (2.5,), {})
    assert result == {"time_scale": 2.5}
    b.close()


def test_pause_delegates_to_client(stub_client: dict) -> None:
    """bridge.pause() 委托给 client.pause()，返回值透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["pause"] = {"paused": True}
    result = b.pause()
    assert c.calls[-1] == ("pause", (), {})
    assert result == {"paused": True}
    b.close()


def test_unpause_delegates_to_client(stub_client: dict) -> None:
    """bridge.unpause() 委托给 client.unpause()，返回值透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["unpause"] = {"paused": False}
    result = b.unpause()
    assert c.calls[-1] == ("unpause", (), {})
    assert result == {"paused": False}
    b.close()


def test_step_frames_delegates_to_client(stub_client: dict) -> None:
    """bridge.step_frames(10) 委托给 client.step_frames(10, physics=False)，返回值透传。"""
    b, c = _make_bridge(stub_client)
    c.returns["step_frames"] = {"stepped": 10, "paused": True}
    result = b.step_frames(10)
    assert c.calls[-1] == ("step_frames", (10,), {"physics": False})
    assert result == {"stepped": 10, "paused": True}
    b.close()


def test_step_frames_physics_flag_delegates_to_client(stub_client: dict) -> None:
    """bridge.step_frames(5, physics=True) 把 physics=True 透传到 client。"""
    b, c = _make_bridge(stub_client)
    c.returns["step_frames"] = {"stepped": 5, "paused": True}
    b.step_frames(5, physics=True)
    assert c.calls[-1] == ("step_frames", (5,), {"physics": True})
    b.close()


def test_errors_passes_cursor(stub_client: dict) -> None:
    """errors 透传 since/limit 并原样返回（issue #103）。"""
    b, c = _make_bridge(stub_client)
    c.returns["errors"] = {"errors": [], "marker": 5, "dropped": 0, "truncated": False}
    result = b.errors(since=3, limit=7)
    assert result["marker"] == 5
    assert c.calls[-1] == ("errors", (), {"since": 3, "limit": 7})
    b.close()


# ── Task 4: instance 参数透传 ──


def test_bridge_instance_param_forwarded_to_game_client(stub_client: dict) -> None:
    """GameBridge(instance="server") 必须把 instance 透传给 GameClient。

    stub_client fixture 已 patch 掉 bridge_mod.GameClient → _StubClient。
    _StubClient.__init__ 只接受 port=，不接受 instance=，
    所以需要升级 _StubClient 以验证透传行为。
    本测试另开一条 monkeypatch 路径：记录构造参数。
    """
    received_kwargs: dict = {}

    class _CapturingStub(_StubClient):
        def __init__(self, port: int = DEFAULT_PORT, instance: str | None = None) -> None:
            super().__init__(port=port)
            received_kwargs["port"] = port
            received_kwargs["instance"] = instance

    import godot_cli_control.bridge as _bridge_mod

    # 临时用 _CapturingStub 替换，不影响其他测试
    original = _bridge_mod.GameClient
    _bridge_mod.GameClient = _CapturingStub  # type: ignore
    try:
        b = GameBridge(instance="server")
        b.close()
    finally:
        _bridge_mod.GameClient = original

    assert received_kwargs.get("instance") == "server", (
        f"GameBridge 应把 instance='server' 透传给 GameClient，实际收到 {received_kwargs}"
    )


# ── Task 4: event loop 泄漏防御 ──


def test_loop_closed_when_client_constructor_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """GameClient 构造抛异常（如 InstanceAmbiguityError）时，
    已建的 event loop 必须被 close，避免 ResourceWarning 和 fd 累积。

    策略：monkeypatch asyncio.new_event_loop，捕获建立的 loop 对象；
    再 patch bridge_mod.GameClient 构造直接抛错；
    最后断言 captured_loop.is_closed()。
    """
    import asyncio as _asyncio

    captured: list[_asyncio.AbstractEventLoop] = []
    real_new_event_loop = _asyncio.new_event_loop

    def _capturing_new_event_loop() -> _asyncio.AbstractEventLoop:
        loop = real_new_event_loop()
        captured.append(loop)
        return loop

    monkeypatch.setattr(_asyncio, "new_event_loop", _capturing_new_event_loop)

    import godot_cli_control.bridge as _bridge_mod

    class _BoomClient:
        def __init__(self, port: int = DEFAULT_PORT, instance: str | None = None) -> None:
            raise RuntimeError("构造爆炸（模拟 InstanceAmbiguityError）")

    monkeypatch.setattr(_bridge_mod, "GameClient", _BoomClient)

    with pytest.raises(RuntimeError, match="构造爆炸"):
        GameBridge()

    assert len(captured) == 1, "应恰好创建了一个 event loop"
    assert captured[0].is_closed(), "构造失败后 event loop 必须已 close，否则会泄漏"


def test_loop_closed_when_connect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect() 抛异常（ConnectionError 等）时，event loop 同样必须 close。

    此路径在多实例 feature 上线后变为常见路径（daemon 未启动即连接），
    不能每次都泄漏一个 selector fd。
    """
    import asyncio as _asyncio

    captured: list[_asyncio.AbstractEventLoop] = []
    real_new_event_loop = _asyncio.new_event_loop

    def _capturing_new_event_loop() -> _asyncio.AbstractEventLoop:
        loop = real_new_event_loop()
        captured.append(loop)
        return loop

    monkeypatch.setattr(_asyncio, "new_event_loop", _capturing_new_event_loop)

    import godot_cli_control.bridge as _bridge_mod

    class _ConnectBoomClient(_StubClient):
        async def connect(self, **kwargs: object) -> None:
            raise ConnectionError("连接爆炸（模拟 daemon 未启动）")

    monkeypatch.setattr(_bridge_mod, "GameClient", _ConnectBoomClient)

    with pytest.raises(ConnectionError, match="连接爆炸"):
        GameBridge()

    assert len(captured) == 1, "应恰好创建了一个 event loop"
    assert captured[0].is_closed(), "connect 失败后 event loop 必须已 close，否则会泄漏"
