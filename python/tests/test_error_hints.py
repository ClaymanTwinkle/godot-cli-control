"""错误信封 hint 字段（服务端下发透传 + 客户端 -1xxx 映射）单元测试。

契约：
* 服务端 1xxx/-32xxx 的 hint 由 addon 随响应下发（error.hint），客户端只透传；
* 客户端 -1xxx 的 hint 由 cli.py `_CLIENT_HINTS` 在发射点补齐；
* 显式 hint（服务端下发）优先于映射表；无 hint 的码不带空字段占位；
* --text 模式在 stderr 尾部追加「（提示：...）」。
"""

from __future__ import annotations

import asyncio
import json

import pytest


# ── RpcError / GameClient 透传 ──


def test_rpc_error_hint_defaults_none() -> None:
    from godot_cli_control.client import RpcError

    e = RpcError(1001, "node not found")
    assert e.hint is None


def test_unwrap_response_parses_hint() -> None:
    from godot_cli_control.client import GameClient, RpcError

    with pytest.raises(RpcError) as ei:
        GameClient._unwrap_response(
            {
                "error": {
                    "code": 1009,
                    "message": "not paused",
                    "hint": "call `pause` first",
                }
            }
        )
    assert ei.value.code == 1009
    assert ei.value.hint == "call `pause` first"


def test_unwrap_response_hint_absent_is_none() -> None:
    from godot_cli_control.client import GameClient, RpcError

    with pytest.raises(RpcError) as ei:
        GameClient._unwrap_response(
            {"error": {"code": 1001, "message": "node not found"}}
        )
    assert ei.value.hint is None


# ── _error_object / _emit_error_payload ──


def test_error_object_explicit_hint_wins() -> None:
    from godot_cli_control.cli import _error_object

    err = _error_object(1009, "not paused", hint="call `pause` first")
    assert err == {
        "code": 1009,
        "message": "not paused",
        "hint": "call `pause` first",
    }


def test_error_object_client_code_falls_back_to_map() -> None:
    from godot_cli_control.cli import CLIENT_CODE_CONNECTION, _error_object

    err = _error_object(CLIENT_CODE_CONNECTION, "refused")
    assert "daemon status" in err["hint"]


def test_error_object_unhinted_code_has_no_hint_key() -> None:
    """-1003 的 message 各 case 已足够具体，不进映射表 → 不带空字段。"""
    from godot_cli_control.cli import CLIENT_CODE_USAGE, _error_object

    err = _error_object(CLIENT_CODE_USAGE, "combo 没给 steps")
    assert "hint" not in err


def test_emit_error_payload_includes_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from godot_cli_control.cli import CLIENT_CODE_TIMEOUT, _emit_error_payload

    _emit_error_payload(CLIENT_CODE_TIMEOUT, "timed out")
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "daemon logs" in payload["error"]["hint"]


# ── _emit_envelope_error text 模式 ──


def test_emit_envelope_error_text_appends_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from godot_cli_control.cli import OUTPUT_TEXT, _emit_envelope_error

    _emit_envelope_error(OUTPUT_TEXT, 1009, "not paused", hint="call `pause` first")
    err = capsys.readouterr().err
    assert "not paused" in err
    assert "提示：call `pause` first" in err


def test_emit_envelope_error_text_no_hint_no_suffix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from godot_cli_control.cli import OUTPUT_TEXT, _emit_envelope_error

    _emit_envelope_error(OUTPUT_TEXT, 1001, "node not found")
    err = capsys.readouterr().err
    assert "node not found" in err
    assert "提示" not in err


# ── 端到端：RpcError.hint 穿透 _run_rpc 到 JSON 信封 ──


def test_run_rpc_envelope_carries_server_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from unittest.mock import AsyncMock, patch

    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from godot_cli_control.client import RpcError

    spec = RPC_BY_NAME["click"]
    ns = __import__("argparse").Namespace(node_path="/root/Nope")

    mock_client = AsyncMock()
    mock_client.click = AsyncMock(
        side_effect=RpcError(
            1001, "node not found", hint="locate by text with `find`"
        )
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["hint"] == "locate by text with `find`"
