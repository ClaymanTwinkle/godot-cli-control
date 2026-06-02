"""单测：本批 issue 修复的契约锁定（无需真实 daemon）。

覆盖：
  - #82  RPC 子命令的用法错（-1003）恒退 64，不再 2
  - #90  hold / tap / combo 的 --wait 在输入后阻塞 wait_game_time(duration)
  - #44  项目级 .cli_control/config.json 的 idle_timeout 回退
  - #69  GODOT_CLI_LONG_OP_TIMEOUT env 覆盖长操作生死线
  - #91  GameClient()/discover_port 无显式 port 时从 .cli_control/port 发现
"""

from __future__ import annotations

import argparse
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


# ── #82：RPC 子命令用法错恒退 64 ──────────────────────────────────────────


def test_run_rpc_value_error_exits_usage_not_infra() -> None:
    """handler 抛 ValueError（如 wait-time 的 seconds 非数字）→ EXIT_USAGE(64) + -1003。

    回归 #82：此前 _run_rpc 的 ValueError 分支退 EXIT_INFRA_ERROR(2)，与 preflight
    路径（combo/hold 退 64）冲突，使同一 -1003 码退出码有歧义。"""
    from godot_cli_control.cli import (
        EXIT_USAGE,
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )

    spec = RPC_BY_NAME["wait-time"]
    ns = argparse.Namespace(seconds="not-a-number")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == EXIT_USAGE, f"用法错应退 64，实际 {rc}"


def test_run_rpc_value_error_envelope_code_is_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ValueError 分支的信封 code 必须是 -1003（CLIENT_CODE_USAGE）。"""
    from godot_cli_control.cli import (
        CLIENT_CODE_USAGE,
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )

    spec = RPC_BY_NAME["wait-time"]
    ns = argparse.Namespace(seconds="xyz")
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == CLIENT_CODE_USAGE


# ── #90：--wait 阻塞 ───────────────────────────────────────────────────────


def _wait_aware_client() -> AsyncMock:
    c = AsyncMock()
    c.hold = AsyncMock(return_value={"success": True})
    c.action_tap = AsyncMock(return_value={"success": True})
    c.combo = AsyncMock(return_value={"success": True})
    c.wait_game_time = AsyncMock(return_value={"success": True})
    return c


def test_hold_wait_blocks_for_duration() -> None:
    from godot_cli_control.cli import cmd_hold

    c = _wait_aware_client()
    ns = argparse.Namespace(action="move_right", duration="1.5", wait=True)
    asyncio.run(cmd_hold(c, ns))
    c.hold.assert_awaited_once_with("move_right", 1.5)
    c.wait_game_time.assert_awaited_once_with(1.5)


def test_hold_without_wait_does_not_block() -> None:
    from godot_cli_control.cli import cmd_hold

    c = _wait_aware_client()
    ns = argparse.Namespace(action="move_right", duration="1.5", wait=False)
    asyncio.run(cmd_hold(c, ns))
    c.hold.assert_awaited_once()
    c.wait_game_time.assert_not_awaited()


def test_hold_missing_wait_attr_defaults_async() -> None:
    """ns 没有 wait 属性（旧调用方）时按异步处理，不崩。"""
    from godot_cli_control.cli import cmd_hold

    c = _wait_aware_client()
    ns = argparse.Namespace(action="jump", duration="0.5")
    asyncio.run(cmd_hold(c, ns))
    c.wait_game_time.assert_not_awaited()


def test_tap_wait_blocks_for_duration() -> None:
    from godot_cli_control.cli import cmd_tap

    c = _wait_aware_client()
    ns = argparse.Namespace(action="jump", duration="0.3", wait=True)
    asyncio.run(cmd_tap(c, ns))
    c.action_tap.assert_awaited_once_with("jump", 0.3)
    c.wait_game_time.assert_awaited_once_with(0.3)


def test_tap_wait_uses_default_duration() -> None:
    """tap 不传 duration 时默认 0.1，--wait 阻塞 0.1。"""
    from godot_cli_control.cli import cmd_tap

    c = _wait_aware_client()
    ns = argparse.Namespace(action="jump", duration=None, wait=True)
    asyncio.run(cmd_tap(c, ns))
    c.wait_game_time.assert_awaited_once_with(0.1)


def test_combo_wait_blocks_for_summed_duration() -> None:
    from godot_cli_control.cli import cmd_combo

    c = _wait_aware_client()
    steps = [{"action": "jump", "duration": 0.2}, {"wait": 0.3}, {"action": "attack"}]
    ns = argparse.Namespace(wait=True, _combo_steps=steps)
    asyncio.run(cmd_combo(c, ns))
    c.combo.assert_awaited_once_with(steps)
    # 0.2 + 0.3 + 0.1(默认) = 0.6
    c.wait_game_time.assert_awaited_once()
    awaited_arg = c.wait_game_time.await_args.args[0]
    assert abs(awaited_arg - 0.6) < 1e-9, awaited_arg


def test_combo_total_duration_tolerates_garbage() -> None:
    from godot_cli_control.cli import _combo_total_duration

    steps = [{"action": "x", "duration": "bad"}, "not-a-dict", {"wait": 1}]
    # 0.1(bad→默认) + 0(非 dict 跳过) + 1 = 1.1
    assert abs(_combo_total_duration(steps) - 1.1) < 1e-9


# ── #44：项目级 config idle_timeout 回退 ──────────────────────────────────


def test_resolve_idle_timeout_explicit_wins(tmp_path, monkeypatch) -> None:
    from godot_cli_control.cli import _resolve_idle_timeout

    ctrl = tmp_path / ".cli_control"
    ctrl.mkdir()
    (ctrl / "config.json").write_text('{"idle_timeout": "30m"}')
    monkeypatch.chdir(tmp_path)
    # 显式 --idle-timeout 10s 压过 config
    assert _resolve_idle_timeout(argparse.Namespace(idle_timeout="10s")) == 10


def test_resolve_idle_timeout_falls_back_to_config(tmp_path, monkeypatch) -> None:
    from godot_cli_control.cli import _resolve_idle_timeout

    ctrl = tmp_path / ".cli_control"
    ctrl.mkdir()
    (ctrl / "config.json").write_text('{"idle_timeout": "30m"}')
    monkeypatch.chdir(tmp_path)
    assert _resolve_idle_timeout(argparse.Namespace(idle_timeout="0")) == 1800


def test_resolve_idle_timeout_no_config_is_zero(tmp_path, monkeypatch) -> None:
    from godot_cli_control.cli import _resolve_idle_timeout

    monkeypatch.chdir(tmp_path)
    assert _resolve_idle_timeout(argparse.Namespace(idle_timeout="0")) == 0


def test_resolve_idle_timeout_bad_config_raises_clear_error(tmp_path, monkeypatch) -> None:
    from godot_cli_control.cli import _resolve_idle_timeout

    ctrl = tmp_path / ".cli_control"
    ctrl.mkdir()
    (ctrl / "config.json").write_text('{"idle_timeout": "totally-bad"}')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="config.json"):
        _resolve_idle_timeout(argparse.Namespace(idle_timeout="0"))


def test_read_project_config_tolerates_missing_and_garbage(tmp_path, monkeypatch) -> None:
    from godot_cli_control.daemon import read_project_config

    monkeypatch.chdir(tmp_path)
    assert read_project_config() == {}  # 无文件

    ctrl = tmp_path / ".cli_control"
    ctrl.mkdir()
    (ctrl / "config.json").write_text("{ not valid json")
    assert read_project_config() == {}  # 坏 JSON → {}

    (ctrl / "config.json").write_text("[1, 2, 3]")
    assert read_project_config() == {}  # 顶层非对象 → {}


# ── #69：GODOT_CLI_LONG_OP_TIMEOUT env 覆盖 ───────────────────────────────


def test_long_op_timeout_default(monkeypatch) -> None:
    from godot_cli_control.client import LONG_OP_DEFAULT_TIMEOUT, _resolve_long_op_timeout

    monkeypatch.delenv("GODOT_CLI_LONG_OP_TIMEOUT", raising=False)
    assert _resolve_long_op_timeout() == LONG_OP_DEFAULT_TIMEOUT


def test_long_op_timeout_env_override(monkeypatch) -> None:
    from godot_cli_control.client import _resolve_long_op_timeout

    monkeypatch.setenv("GODOT_CLI_LONG_OP_TIMEOUT", "1800")
    assert _resolve_long_op_timeout() == 1800.0


@pytest.mark.parametrize("bad", ["-5", "0", "nope", ""])
def test_long_op_timeout_invalid_env_falls_back(monkeypatch, bad) -> None:
    from godot_cli_control.client import LONG_OP_DEFAULT_TIMEOUT, _resolve_long_op_timeout

    monkeypatch.setenv("GODOT_CLI_LONG_OP_TIMEOUT", bad)
    assert _resolve_long_op_timeout() == LONG_OP_DEFAULT_TIMEOUT


@pytest.mark.asyncio
async def test_wait_game_time_uses_env_override(monkeypatch) -> None:
    """wait_game_time 每次现取生死线：env 改了即时生效（传给 request 的 timeout）。"""
    from godot_cli_control.client import GameClient

    monkeypatch.setenv("GODOT_CLI_LONG_OP_TIMEOUT", "1234")
    client = GameClient(port=9999)
    client.request = AsyncMock(return_value={"success": True})  # type: ignore[method-assign]
    await client.wait_game_time(2.0)
    assert client.request.await_args.kwargs["timeout"] == 1234.0


# ── #91：无 port 时 auto-discover .cli_control/port ───────────────────────


def test_discover_port_reads_control_dir(tmp_path) -> None:
    from godot_cli_control.daemon import discover_port

    assert discover_port(tmp_path) is None  # 无 port 文件
    ctrl = tmp_path / ".cli_control"
    ctrl.mkdir()
    (ctrl / "port").write_text("54321\n")
    assert discover_port(tmp_path) == 54321


def test_gameclient_no_port_autodiscovers(tmp_path, monkeypatch) -> None:
    from godot_cli_control.client import GameClient

    ctrl = tmp_path / ".cli_control"
    ctrl.mkdir()
    (ctrl / "port").write_text("55555")
    monkeypatch.chdir(tmp_path)
    client = GameClient()  # 无 port → 从 cwd 的 .cli_control/port 发现
    assert client._port == 55555


def test_gameclient_no_port_no_file_falls_back_to_default(tmp_path, monkeypatch) -> None:
    from godot_cli_control.client import DEFAULT_PORT, GameClient

    monkeypatch.chdir(tmp_path)  # 无 .cli_control/port
    client = GameClient()
    assert client._port == DEFAULT_PORT


def test_gameclient_explicit_port_skips_discovery(tmp_path, monkeypatch) -> None:
    from godot_cli_control.client import GameClient

    ctrl = tmp_path / ".cli_control"
    ctrl.mkdir()
    (ctrl / "port").write_text("55555")
    monkeypatch.chdir(tmp_path)
    client = GameClient(port=8888)  # 显式 port 不走发现
    assert client._port == 8888
