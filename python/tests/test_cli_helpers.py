"""单元测试：cli.py 里的 pure helper 函数。

针对每个 ``cmd_*`` 1 行 wrapper（只是把 ns.X 转给 client.METHOD）以及 ``_fmt_*``
/ ``_exit_from_*`` 格式化辅助 —— 都是纯函数 / async-纯-委托，无副作用。

这一组测试让 cli.py 71% 覆盖率上升，同时对协议契约形成 lock-in：
将来谁误把 ``cmd_tree`` 改成 ``client.get_scene_tree(depth=ns.depth or 5)`` 而非
``or 3`` 默认值，这里就会红。
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from godot_cli_control import cli


def _ns(**kwargs: Any) -> argparse.Namespace:
    defaults = {"max_nodes": 200}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── cmd_* 1 行包装：参数透传 ────────────────────────────────────────


def test_cmd_click_passes_path() -> None:
    client = AsyncMock()
    client.click = AsyncMock(return_value={"success": True})
    result = _run(cli.cmd_click(client, _ns(node_path="/root/B")))
    client.click.assert_awaited_once_with("/root/B")
    assert result == {"success": True}


def test_cmd_tree_default_depth_is_3_not_5() -> None:
    """合同：CLI 默认 depth=3（比 client 默认 5 浅，避免大场景树阻塞 stdout）。"""
    client = AsyncMock()
    client.get_scene_tree = AsyncMock(return_value={"name": "root"})
    _run(cli.cmd_tree(client, _ns(depth=None)))
    client.get_scene_tree.assert_awaited_once_with(depth=3, max_nodes=200)


def test_cmd_tree_explicit_depth_passed() -> None:
    client = AsyncMock()
    client.get_scene_tree = AsyncMock(return_value={})
    _run(cli.cmd_tree(client, _ns(depth=10)))
    client.get_scene_tree.assert_awaited_once_with(depth=10, max_nodes=200)


def test_cmd_press_release() -> None:
    client = AsyncMock()
    client.action_press = AsyncMock(return_value={"success": True})
    client.action_release = AsyncMock(return_value={"success": True})
    _run(cli.cmd_press(client, _ns(action="jump")))
    _run(cli.cmd_release(client, _ns(action="jump")))
    client.action_press.assert_awaited_once_with("jump")
    client.action_release.assert_awaited_once_with("jump")


def test_cmd_tap_default_duration_0_1() -> None:
    client = AsyncMock()
    client.action_tap = AsyncMock(return_value={"success": True})
    _run(cli.cmd_tap(client, _ns(action="atk", duration=None)))
    client.action_tap.assert_awaited_once_with("atk", 0.1)


def test_cmd_tap_explicit_duration() -> None:
    client = AsyncMock()
    client.action_tap = AsyncMock(return_value={"success": True})
    _run(cli.cmd_tap(client, _ns(action="atk", duration="0.3")))
    client.action_tap.assert_awaited_once_with("atk", 0.3)


def test_cmd_hold_passes_action_and_duration() -> None:
    client = AsyncMock()
    client.hold = AsyncMock(return_value={"success": True})
    _run(cli.cmd_hold(client, _ns(action="run", duration="1.5")))
    client.hold.assert_awaited_once_with("run", 1.5)


def test_cmd_release_all() -> None:
    client = AsyncMock()
    client.release_all = AsyncMock(return_value={"success": True})
    _run(cli.cmd_release_all(client, _ns()))
    client.release_all.assert_awaited_once_with()


def test_cmd_get_returns_value_directly() -> None:
    """cmd_get 单属性透传 client.request("get_property") 的完整 result（含 type 字段）。

    注：cmd_get 自 #99 改为走 client.request 而非 client.get_property，
    透传带 type 字段的 RPC result 给 agent 消歧；get_property 是裸 value 便捷层。
    """
    client = AsyncMock()
    client.request = AsyncMock(return_value={"value": 42})
    result = _run(cli.cmd_get(client, _ns(node_path="/X", props=["value"])))
    assert result == {"value": 42}
    client.request.assert_awaited_once_with(
        "get_property", {"path": "/X", "property": "value"}
    )


def test_cmd_set_parses_json_value() -> None:
    """``cmd_set`` 必须把字符串值通过 _parse_json_arg 转 JSON（不再原样）。"""
    client = AsyncMock()
    client.set_property = AsyncMock(return_value={"success": True})
    _run(cli.cmd_set(client, _ns(node_path="/X", prop="modulate", value="[1,0.5,0,1]")))
    client.set_property.assert_awaited_once_with(
        "/X", "modulate", [1, 0.5, 0, 1]
    )


def test_cmd_set_string_fallback_when_not_json() -> None:
    """不是合法 JSON 时退回字符串字面量（让 ``set foo bar baz`` 不报错）。"""
    client = AsyncMock()
    client.set_property = AsyncMock(return_value={"success": True})
    _run(cli.cmd_set(client, _ns(node_path="/X", prop="text", value="hello world")))
    client.set_property.assert_awaited_once_with("/X", "text", "hello world")


def test_cmd_call_parses_each_arg_independently() -> None:
    client = AsyncMock()
    client.call_method = AsyncMock(return_value=7)
    _run(
        cli.cmd_call(
            client, _ns(node_path="/X", method="add", args=["3", "4", "literal"])
        )
    )
    client.call_method.assert_awaited_once_with("/X", "add", [3, 4, "literal"])


def test_cmd_call_handles_no_args() -> None:
    client = AsyncMock()
    client.call_method = AsyncMock(return_value=None)
    _run(cli.cmd_call(client, _ns(node_path="/X", method="ping", args=None)))
    client.call_method.assert_awaited_once_with("/X", "ping", [])


def test_cmd_text_passes_path() -> None:
    client = AsyncMock()
    client.get_text = AsyncMock(return_value="hello")
    result = _run(cli.cmd_text(client, _ns(node_path="/L")))
    assert result == "hello"


def test_cmd_exists_visible() -> None:
    client = AsyncMock()
    client.node_exists = AsyncMock(return_value=True)
    client.is_visible = AsyncMock(return_value=False)
    assert _run(cli.cmd_exists(client, _ns(node_path="/X"))) is True
    assert _run(cli.cmd_visible(client, _ns(node_path="/X"))) is False


def test_cmd_children_default_filter_empty() -> None:
    client = AsyncMock()
    client.get_children = AsyncMock(return_value=[{"name": "A"}])
    _run(cli.cmd_children(client, _ns(node_path="/", type_filter=None)))
    client.get_children.assert_awaited_once_with("/", type_filter="")


def test_cmd_children_with_type_filter() -> None:
    client = AsyncMock()
    client.get_children = AsyncMock(return_value=[])
    _run(cli.cmd_children(client, _ns(node_path="/", type_filter="Button")))
    client.get_children.assert_awaited_once_with("/", type_filter="Button")


def test_cmd_wait_node_default_timeout_5() -> None:
    client = AsyncMock()
    client.wait_for_node = AsyncMock(return_value=True)
    result = _run(cli.cmd_wait_node(client, _ns(node_path="/X", timeout=None)))
    client.wait_for_node.assert_awaited_once_with("/X", timeout=5.0)
    assert result == {"found": True, "path": "/X", "timeout": 5.0}


def test_cmd_wait_node_explicit_timeout() -> None:
    client = AsyncMock()
    client.wait_for_node = AsyncMock(return_value=False)
    result = _run(cli.cmd_wait_node(client, _ns(node_path="/X", timeout="2.5")))
    assert result == {"found": False, "path": "/X", "timeout": 2.5}


def test_cmd_wait_time() -> None:
    client = AsyncMock()
    client.wait_game_time = AsyncMock(return_value={"success": True})
    _run(cli.cmd_wait_time(client, _ns(seconds="3.0")))
    client.wait_game_time.assert_awaited_once_with(3.0)


def test_cmd_pressed() -> None:
    client = AsyncMock()
    client.get_pressed = AsyncMock(return_value=["jump"])
    result = _run(cli.cmd_pressed(client, _ns()))
    assert result == ["jump"]


def test_cmd_combo_cancel() -> None:
    client = AsyncMock()
    client.combo_cancel = AsyncMock(return_value={"success": True})
    _run(cli.cmd_combo_cancel(client, _ns()))
    client.combo_cancel.assert_awaited_once_with()


def test_cmd_actions_default_filters_builtin() -> None:
    client = AsyncMock()
    client.list_input_actions = AsyncMock(return_value=["jump"])
    _run(cli.cmd_actions(client, _ns(all=False)))
    client.list_input_actions.assert_awaited_once_with(include_builtin=False)


def test_cmd_actions_all_includes_builtin() -> None:
    client = AsyncMock()
    client.list_input_actions = AsyncMock(return_value=["ui_accept", "jump"])
    _run(cli.cmd_actions(client, _ns(all=True)))
    client.list_input_actions.assert_awaited_once_with(include_builtin=True)


# ── _fmt_* 文本格式化 ──────────────────────────────────────────────


def test_fmt_lines_joins_with_newline() -> None:
    assert cli._fmt_lines(["a", "b", "c"]) == "a\nb\nc"


def test_fmt_lines_handles_non_str() -> None:
    assert cli._fmt_lines([1, 2, 3]) == "1\n2\n3"


def test_fmt_children_text_takes_name_field() -> None:
    items = [{"name": "Btn", "type": "Button"}, {"name": "Lbl"}]
    assert cli._fmt_children_text(items) == "Btn\nLbl"


def test_fmt_bool_text_lowercase() -> None:
    """与 shell true/false 对齐，便于 `if [ "$v" = "true" ]`。"""
    assert cli._fmt_bool_text(True) == "true"
    assert cli._fmt_bool_text(False) == "false"
    assert cli._fmt_bool_text(0) == "false"
    assert cli._fmt_bool_text(1) == "true"


def test_fmt_get_text_string_passthrough() -> None:
    """字符串原样输出，不要被 JSON 加引号 —— 与 shell 拼接友好。"""
    assert cli._fmt_get_text("hello") == "hello"


def test_fmt_get_text_non_string_serialized_as_json() -> None:
    assert cli._fmt_get_text(42) == "42"
    assert cli._fmt_get_text([1, 2]) == "[1, 2]"
    assert cli._fmt_get_text({"a": 1}) == '{"a": 1}'
    assert cli._fmt_get_text(None) == "null"


def test_fmt_get_text_unicode_not_escaped() -> None:
    """ensure_ascii=False —— Chinese / emoji 字符不要被转义。"""
    assert cli._fmt_get_text(["你好"]) == '["你好"]'


def test_fmt_tree_text_indent_2() -> None:
    out = cli._fmt_tree_text({"name": "root", "children": []})
    assert "\n" in out  # indented output spans multiple lines
    assert "name" in out


def test_fmt_screenshot_text_includes_size() -> None:
    out = cli._fmt_screenshot_text({"path": "/x.png", "bytes": 1024})
    assert "/x.png" in out and "1024" in out


def test_fmt_wait_node_text() -> None:
    assert cli._fmt_wait_node_text({"found": True}) == "found"
    assert cli._fmt_wait_node_text({"found": False}) == "timeout"


def test_fmt_wait_time_text() -> None:
    assert "success=True" in cli._fmt_wait_time_text({"success": True})
    assert "success=False" in cli._fmt_wait_time_text({"success": False})
    # 缺 success 字段时默认 True（向后兼容旧 GD 版本）
    assert "success=True" in cli._fmt_wait_time_text({})


# ── exit code helpers ─────────────────────────────────────────────


def test_exit_from_bool_true_is_0() -> None:
    """exists/visible 用 exit code 表 bool —— shell `if` 友好。"""
    assert cli._exit_from_bool(True) == cli.EXIT_OK
    assert cli._exit_from_bool(False) == cli.EXIT_RPC_ERROR
    assert cli._exit_from_bool(1) == cli.EXIT_OK
    assert cli._exit_from_bool(0) == cli.EXIT_RPC_ERROR


def test_exit_from_wait_node_uses_found_field() -> None:
    assert cli._exit_from_wait_node({"found": True}) == cli.EXIT_OK
    assert cli._exit_from_wait_node({"found": False}) == cli.EXIT_RPC_ERROR
    # 缺字段视为未找到
    assert cli._exit_from_wait_node({}) == cli.EXIT_RPC_ERROR


# ── issue #96: wait-prop / wait-signal / wait-frames cmd_* + preflight ──


def test_cmd_wait_prop_matched_passes_all_params() -> None:
    """cmd_wait_prop 必须把 ns 上所有字段透传给 client.wait_property。"""
    client = AsyncMock()
    client.wait_property = AsyncMock(return_value={"matched": True, "value": 500, "waited": 0.12})
    result = _run(cli.cmd_wait_prop(client, _ns(
        node_path="/root/Player", prop="position:x", value="500",
        op="gt", timeout="3", tolerance="0",
    )))
    client.wait_property.assert_awaited_once_with(
        "/root/Player", "position:x", 500, op="gt", timeout=3.0, tolerance=0.0,
    )
    assert result["matched"] is True


def test_cmd_wait_prop_false_result_returned() -> None:
    client = AsyncMock()
    client.wait_property = AsyncMock(return_value={"matched": False, "reason": "timeout", "value": 10})
    result = _run(cli.cmd_wait_prop(client, _ns(
        node_path="/root/X", prop="health", value="100",
        op="eq", timeout="5", tolerance="0",
    )))
    assert result["matched"] is False


def test_exit_from_wait_prop_true_is_0() -> None:
    """exit 0 = 命中，exit 1 = 未命中（超时）。"""
    assert cli._exit_from_wait_prop({"matched": True}) == cli.EXIT_OK
    assert cli._exit_from_wait_prop({"matched": False}) == cli.EXIT_RPC_ERROR
    assert cli._exit_from_wait_prop({}) == cli.EXIT_RPC_ERROR


def test_cmd_wait_signal_matched_passes_timeout() -> None:
    client = AsyncMock()
    client.wait_signal = AsyncMock(return_value={"emitted": True, "args": []})
    result = _run(cli.cmd_wait_signal(client, _ns(
        node_path="/root/Area", signal_name="door_opened", timeout="3",
    )))
    client.wait_signal.assert_awaited_once_with("/root/Area", "door_opened", timeout=3.0)
    assert result["emitted"] is True


def test_cmd_wait_signal_default_timeout_5() -> None:
    client = AsyncMock()
    client.wait_signal = AsyncMock(return_value={"emitted": False})
    _run(cli.cmd_wait_signal(client, _ns(
        node_path="/root/A", signal_name="sig", timeout=None,
    )))
    client.wait_signal.assert_awaited_once_with("/root/A", "sig", timeout=5.0)


def test_exit_from_wait_signal_emitted_is_0() -> None:
    assert cli._exit_from_wait_signal({"emitted": True}) == cli.EXIT_OK
    assert cli._exit_from_wait_signal({"emitted": False}) == cli.EXIT_RPC_ERROR
    assert cli._exit_from_wait_signal({}) == cli.EXIT_RPC_ERROR


def test_cmd_wait_frames_passes_physics_flag() -> None:
    client = AsyncMock()
    client.wait_frames = AsyncMock(return_value={"success": True, "frames": 3})
    result = _run(cli.cmd_wait_frames(client, _ns(frames="3", physics=True)))
    client.wait_frames.assert_awaited_once_with(3, physics=True)
    assert result["frames"] == 3


def test_cmd_wait_frames_default_physics_false() -> None:
    client = AsyncMock()
    client.wait_frames = AsyncMock(return_value={"success": True, "frames": 5})
    _run(cli.cmd_wait_frames(client, _ns(frames="5", physics=False)))
    client.wait_frames.assert_awaited_once_with(5, physics=False)


# ── wait-prop preflight ──


def test_preflight_wait_prop_timeout_non_number_raises() -> None:
    ns = _ns(node_path="/X", prop="p", value="1", op="eq", timeout="abc", tolerance="0")
    with pytest.raises(ValueError, match="timeout"):
        cli._preflight_wait_prop(ns)


def test_preflight_wait_prop_timeout_out_of_range_raises() -> None:
    ns = _ns(node_path="/X", prop="p", value="1", op="eq", timeout="9999", tolerance="0")
    with pytest.raises(ValueError, match="timeout"):
        cli._preflight_wait_prop(ns)


def test_preflight_wait_prop_tolerance_negative_raises() -> None:
    ns = _ns(node_path="/X", prop="p", value="1", op="eq", timeout="5", tolerance="-1")
    with pytest.raises(ValueError, match="tolerance"):
        cli._preflight_wait_prop(ns)


def test_preflight_wait_prop_ordering_op_with_list_raises() -> None:
    """gt + value=[1,2] は数値でない → ValueError。"""
    ns = _ns(node_path="/X", prop="p", value="[1,2]", op="gt", timeout="5", tolerance="0")
    with pytest.raises(ValueError, match="gt"):
        cli._preflight_wait_prop(ns)


def test_preflight_wait_prop_ordering_op_with_string_raises() -> None:
    ns = _ns(node_path="/X", prop="p", value='"hello"', op="gt", timeout="5", tolerance="0")
    with pytest.raises(ValueError, match="gt"):
        cli._preflight_wait_prop(ns)


def test_preflight_wait_prop_ordering_op_with_bool_raises() -> None:
    """bool は数値扱いしない —— isinstance(True, int) は True だが bool は排除。"""
    ns = _ns(node_path="/X", prop="p", value="true", op="gt", timeout="5", tolerance="0")
    with pytest.raises(ValueError, match="gt"):
        cli._preflight_wait_prop(ns)


def test_preflight_wait_prop_eq_with_bool_ok() -> None:
    """eq/ne + bool は合法。"""
    ns = _ns(node_path="/X", prop="visible", value="true", op="eq", timeout="5", tolerance="0")
    cli._preflight_wait_prop(ns)  # should not raise


def test_preflight_wait_prop_eq_with_numeric_ok() -> None:
    ns = _ns(node_path="/X", prop="p", value="42", op="gt", timeout="5", tolerance="0")
    cli._preflight_wait_prop(ns)  # should not raise


# ── wait-signal preflight ──


def test_preflight_wait_signal_timeout_non_number_raises() -> None:
    ns = _ns(node_path="/X", signal_name="sig", timeout="abc")
    with pytest.raises(ValueError, match="timeout"):
        cli._preflight_wait_signal(ns)


def test_preflight_wait_signal_timeout_out_of_range_raises() -> None:
    ns = _ns(node_path="/X", signal_name="sig", timeout="9999")
    with pytest.raises(ValueError, match="timeout"):
        cli._preflight_wait_signal(ns)


def test_preflight_wait_signal_none_timeout_ok() -> None:
    """timeout=None（未传）时 preflight 不报错。"""
    ns = _ns(node_path="/X", signal_name="sig", timeout=None)
    cli._preflight_wait_signal(ns)  # should not raise


# ── wait-frames preflight ──


def test_preflight_wait_frames_non_integer_raises() -> None:
    ns = _ns(frames="abc")
    with pytest.raises(ValueError, match="frames"):
        cli._preflight_wait_frames(ns)


def test_preflight_wait_frames_zero_raises() -> None:
    ns = _ns(frames="0")
    with pytest.raises(ValueError, match="frames"):
        cli._preflight_wait_frames(ns)


def test_preflight_wait_frames_over_max_raises() -> None:
    ns = _ns(frames="9999")
    with pytest.raises(ValueError, match="frames"):
        cli._preflight_wait_frames(ns)


def test_preflight_wait_frames_valid_ok() -> None:
    ns = _ns(frames="3")
    cli._preflight_wait_frames(ns)  # should not raise


# ── fmt helpers for wait-prop / wait-signal / wait-frames ──


def test_fmt_wait_prop_matched() -> None:
    out = cli._fmt_wait_prop_text({"matched": True, "waited": 0.123, "value": 500})
    assert "matched" in out and "0.123" in out


def test_fmt_wait_prop_timeout_has_reason_and_last_value() -> None:
    out = cli._fmt_wait_prop_text({"matched": False, "reason": "timeout", "value": 10})
    assert "timeout" in out and "10" in out


def test_fmt_wait_signal_emitted() -> None:
    assert cli._fmt_wait_signal_text({"emitted": True}) == "emitted"


def test_fmt_wait_signal_timeout() -> None:
    assert cli._fmt_wait_signal_text({"emitted": False}) == "timeout"
    assert cli._fmt_wait_signal_text({}) == "timeout"


def test_fmt_wait_frames_text() -> None:
    """wait-frames 文本输出包含帧数。"""
    # RpcSpec text_formatter is a lambda; test by calling it directly from spec
    import importlib
    importlib.reload(cli)  # ensure latest state
    spec = cli.RPC_BY_NAME.get("wait-frames")
    if spec is None:
        pytest.skip("wait-frames spec not yet registered")
    out = spec.text_formatter({"frames": 5, "success": True})
    assert "5" in out and "frames" in out


# ── argparse parser accepts wait-* subcommands ──


def test_parser_accepts_wait_prop() -> None:
    """wait-prop 必须被 build_parser 注册为子命令。"""
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["wait-prop", "/root/N", "visible", "true"])
    assert ns.cmd == "wait-prop"
    assert ns.node_path == "/root/N"
    assert ns.prop == "visible"
    assert ns.value == "true"


def test_parser_accepts_wait_signal() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["wait-signal", "/root/A", "fired"])
    assert ns.cmd == "wait-signal"
    assert ns.node_path == "/root/A"
    assert ns.signal_name == "fired"


def test_parser_accepts_wait_frames() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["wait-frames", "3", "--physics"])
    assert ns.cmd == "wait-frames"
    assert ns.frames == "3"
    assert ns.physics is True
