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

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
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

    async def get_scene_tree(self, depth: int = 5) -> dict:
        return self._record("get_scene_tree", (), {"depth": depth})

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

    async def screenshot(self) -> bytes:
        return self._record("screenshot", (), {})

    async def get_property(self, path: str, prop: str) -> Any:
        return self._record("get_property", (path, prop), {})

    async def set_property(self, path: str, prop: str, value: Any) -> dict:
        return self._record("set_property", (path, prop, value), {})

    async def call_method(
        self, path: str, method: str, args: list | None = None
    ) -> Any:
        return self._record("call_method", (path, method, args), {})

    async def get_children(self, path: str, type_filter: str = "") -> list[dict]:
        return self._record("get_children", (path,), {"type_filter": type_filter})


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    """patch 模块级 GameClient 符号，让 GameBridge() 实例化时拿到桩。"""
    holder: dict[str, _StubClient] = {}

    def _factory(port: int = DEFAULT_PORT) -> _StubClient:
        c = _StubClient(port=port)
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


def test_screenshot_with_path_writes_file(
    stub_client: dict, tmp_path: Path
) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["screenshot"] = b"\x89PNG\r\nfake"
    out = tmp_path / "subdir" / "shot.png"
    data = b.screenshot(str(out))
    assert data == b"\x89PNG\r\nfake"
    assert out.exists()
    assert out.read_bytes() == b"\x89PNG\r\nfake"
    # 父目录自动创建
    assert out.parent.is_dir()
    b.close()


# ── 属性读写 / 方法调用 ──


def test_get_property(stub_client: dict) -> None:
    b, c = _make_bridge(stub_client)
    c.returns["get_property"] = 42
    assert b.get_property("/root/X", "value") == 42
    assert c.calls[-1] == ("get_property", ("/root/X", "value"), {})
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
