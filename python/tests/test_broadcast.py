"""--instance all 广播（issue #145）CLI 层单测。

覆盖：保留名 all 的 CLI 面拒绝、顶层 --instance all 放行、daemon/run 路径拒绝、
{instance} 占位符替换、screenshot preflight 守卫、_run_rpc_broadcast 聚合信封
与退出码矩阵、main() 接线。daemon.py 层的保留名校验在 test_daemon.py。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ── Task 1: 保留名 all 的 CLI 面 ──


def test_daemon_name_all_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """daemon start --name all → argparse type 校验失败 → exit 64 + -1003 信封，
    message 含保留原因与 daemon stop --all 指引。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["daemon", "start", "--name", "all"])
    assert exc_info.value.code == 64
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "广播保留名" in payload["error"]["message"]
    assert "daemon stop --all" in payload["error"]["message"]


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "x.py", "--name", "all"],  # 顶层 run 命令也挂 --name（test_cli.py:2967 同款）
        ["daemon", "status", "--name", "all"],
        ["daemon", "logs", "--name", "all"],
        ["daemon", "stop", "--name", "all"],
    ],
    ids=["run", "status", "logs", "stop"],
)
def test_name_all_rejected_everywhere(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """全部带 --name 的命令拒绝 'all'（同一 type 校验器，钉死不回归）。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(argv)
    assert exc_info.value.code == 64
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == -1003


# ── Task 2: 顶层 --instance all 放行 + daemon/run 路径拒绝 ──


def test_top_level_instance_accepts_all() -> None:
    """顶层 --instance 放行广播哨兵 'all'（RPC 路径的入口）。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["--instance", "all", "exists", "/root/Foo"])
    assert ns.instance == "all"


def test_resolve_daemon_instance_rejects_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--instance all 流入 daemon/run 单靶路径 → -1003 信封 + None（调用方 exit 64）。
    覆盖 daemon stop/status/logs 与 run 四个调用方。"""
    from godot_cli_control.cli import OUTPUT_JSON, _resolve_daemon_instance

    ns = argparse.Namespace(name=None, instance="all", output_format=OUTPUT_JSON)
    assert _resolve_daemon_instance(ns, Path.cwd()) is None
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == -1003
    assert "RPC" in payload["error"]["message"]


def test_daemon_start_rejects_top_level_instance_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--instance all daemon start → exit 64 + -1003（start 不经 _resolve_daemon_instance，
    需要独立守卫）。"""
    import godot_cli_control.cli as cli_mod

    ns = cli_mod.build_parser().parse_args(["--instance", "all", "daemon", "start"])
    rc = cli_mod.cmd_daemon_start(ns)
    assert rc == cli_mod.EXIT_USAGE
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == -1003


def test_run_rejects_instance_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--instance all run script.py → exit 64 + -1003（run 必须单连接，无广播语义）。"""
    import godot_cli_control.cli as cli_mod

    script = tmp_path / "s.py"
    script.write_text("def run(bridge):\n    pass\n", encoding="utf-8")
    ns = cli_mod.build_parser().parse_args(["--instance", "all", "run", str(script)])
    rc = cli_mod.cmd_run(ns)
    assert rc == cli_mod.EXIT_USAGE
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == -1003


def test_stop_all_with_top_level_instance_all_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--instance all daemon stop --all → 既有「--all 与实例选靶互斥」校验拦截。
    本条当下就应绿（cli.py cmd_daemon_stop 既有逻辑覆盖），作回归钉。"""
    from godot_cli_control.cli import EXIT_USAGE, OUTPUT_JSON, cmd_daemon_stop

    ns = argparse.Namespace(
        all=True, name=None, instance="all", project=None, output_format=OUTPUT_JSON
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_USAGE
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == -1003


# ── Task 3: 异常→信封映射提取（_run_rpc 与广播共用） ──


@pytest.mark.parametrize(
    ("raise_exc", "want_code", "want_rc"),
    [
        ("rpc", 1002, 1),
        ("conn", -1001, 2),
        ("timeout", -1002, 2),
        ("io", -1004, 2),
        ("value", -1003, 64),
        ("internal", -1099, 2),
    ],
)
def test_rpc_failure_envelope_mapping(
    raise_exc: str, want_code: int, want_rc: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_rpc_failure_envelope 的 (code, rc) 映射必须与 _run_rpc 原 except 链一致：
    顺序敏感（ConnectionError/TimeoutError 都是 OSError 子类，先窄后宽）。"""
    from godot_cli_control.cli import _rpc_failure_envelope
    from godot_cli_control.client import RpcError

    excs: dict[str, Exception] = {
        "rpc": RpcError(1002, "node not found"),
        "conn": ConnectionError("refused"),
        "timeout": asyncio.TimeoutError(),
        "io": PermissionError(13, "denied"),
        "value": ValueError("bad json"),
        "internal": KeyError("boom"),
    }
    try:
        raise excs[raise_exc]
    except Exception as e:  # noqa: BLE001
        code, msg, rc = _rpc_failure_envelope(e)
    assert (code, rc) == (want_code, want_rc)
    assert msg  # message 永不为空（str(e) 为空时落 fallback）


# ── Task 4: {instance} 占位符 + screenshot preflight ──


def test_instance_substituted_recurses() -> None:
    """str/list/dict 递归替换；非字符串原样返回。"""
    from godot_cli_control.cli import _instance_substituted

    assert _instance_substituted("shot-{instance}.png", "a") == "shot-a.png"
    assert _instance_substituted(["{instance}", 7], "a") == ["a", 7]
    assert _instance_substituted({"k": "x-{instance}"}, "a") == {"k": "x-a"}
    assert _instance_substituted(3.5, "a") == 3.5
    assert _instance_substituted(None, "a") is None
    assert _instance_substituted(True, "a") is True


def test_namespace_for_instance_copies_and_substitutes() -> None:
    """产出新 namespace 且原 ns 不动（广播逐实例各拿一份）。"""
    from godot_cli_control.cli import _namespace_for_instance

    ns = argparse.Namespace(output_path="s-{instance}.png", port=None, depth=3)
    out = _namespace_for_instance(ns, "srv")
    assert out.output_path == "s-srv.png"
    assert out.depth == 3
    assert ns.output_path == "s-{instance}.png"  # 原件无副作用


def test_screenshot_preflight_guard() -> None:
    """广播 + 缺 {instance} → ValueError；带占位符 / 非广播 → 放行。"""
    from godot_cli_control.cli import RPC_BY_NAME

    pf = RPC_BY_NAME["screenshot"].preflight
    assert pf is not None, "screenshot RpcSpec 必须挂 preflight（#145）"
    with pytest.raises(ValueError, match=r"\{instance\}"):
        pf(argparse.Namespace(instance="all", output_path="/tmp/s.png"))
    pf(argparse.Namespace(instance="all", output_path="/tmp/s-{instance}.png"))
    pf(argparse.Namespace(instance=None, output_path="/tmp/s.png"))
    pf(argparse.Namespace(instance="server", output_path="/tmp/s.png"))


def test_screenshot_broadcast_preflight_via_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main()：--instance all screenshot 缺 {instance} → 连接前 exit 64 + -1003
    （preflight 在 daemon 发现/连接之前跑，agent 不用等 30s retry）。"""
    import godot_cli_control.cli as cli_mod

    monkeypatch.setattr(
        sys, "argv",
        ["godot-cli-control", "--instance", "all", "screenshot", "/tmp/s.png"],
    )
    with pytest.raises(SystemExit) as ei:
        cli_mod.main()
    assert ei.value.code == 64
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == -1003


# ── Task 5: _run_rpc_broadcast 聚合 + main() 接线 ──


def _mock_client(**methods: Any) -> AsyncMock:
    """可 async with 的假 GameClient（模式同 test_cli.py 的 _run_rpc 用例）。"""
    client = AsyncMock()
    for name, value in methods.items():
        setattr(client, name, value)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture()
def two_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """打桩：cwd 项目有 a / b 两个活实例，端口 9001 / 9002。

    _run_rpc_broadcast 是函数内 `from .daemon import ...`，运行时取模块属性，
    monkeypatch daemon 模块即可生效。
    """
    import godot_cli_control.daemon as daemon_mod

    monkeypatch.setattr(
        daemon_mod, "list_live_instances", lambda root=None: ["a", "b"]
    )
    monkeypatch.setattr(
        daemon_mod.Daemon,
        "current_port",
        lambda self: {"a": 9001, "b": 9002}[self.instance],
    )


def _broadcast_ns(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"node_path": "/root/Main", "instance": "all", "port": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_broadcast_all_success(
    two_instances: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """两实例全成功：顶层 ok=true、entry 复刻单命令信封 + instance + rc、
    数组按实例名排序、聚合 rc=0、退出码 0。"""
    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc_broadcast

    clients = {
        9001: _mock_client(node_exists=AsyncMock(return_value=True)),
        9002: _mock_client(node_exists=AsyncMock(return_value=True)),
    }
    with patch(
        "godot_cli_control.cli.GameClient", side_effect=lambda port: clients[port]
    ):
        rc = asyncio.run(
            _run_rpc_broadcast(RPC_BY_NAME["exists"], _broadcast_ns(), OUTPUT_JSON)
        )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["result"]["rc"] == 0
    assert payload["result"]["instances"] == [
        {"instance": "a", "ok": True, "result": True, "rc": 0},
        {"instance": "b", "ok": True, "result": True, "rc": 0},
    ]


def test_broadcast_partial_rpc_error_exits_3(
    two_instances: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """一成一败（RPC 错）：失败 entry 带错误信封 + rc=1；聚合 rc=3；顶层 ok 仍 true
    （沿 daemon stop --all 先例，失败细节在 entry）。"""
    from godot_cli_control.cli import (
        EXIT_PARTIAL,
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc_broadcast,
    )
    from godot_cli_control.client import RpcError

    clients = {
        9001: _mock_client(node_exists=AsyncMock(return_value=True)),
        9002: _mock_client(node_exists=AsyncMock(side_effect=RpcError(1002, "boom"))),
    }
    with patch(
        "godot_cli_control.cli.GameClient", side_effect=lambda port: clients[port]
    ):
        rc = asyncio.run(
            _run_rpc_broadcast(RPC_BY_NAME["exists"], _broadcast_ns(), OUTPUT_JSON)
        )

    assert rc == EXIT_PARTIAL
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    by_name = {e["instance"]: e for e in payload["result"]["instances"]}
    assert by_name["a"] == {"instance": "a", "ok": True, "result": True, "rc": 0}
    assert by_name["b"]["ok"] is False
    assert by_name["b"]["error"] == {"code": 1002, "message": "boom"}
    assert by_name["b"]["rc"] == 1
    assert payload["result"]["rc"] == EXIT_PARTIAL


def test_broadcast_semantic_false_exits_3(
    two_instances: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """exists 一 true 一 false：false entry 走 exit_code_from（rc=1，ok 仍 true），
    聚合 rc=3——shell `if --instance all exists` 表达「所有实例都存在」。"""
    from godot_cli_control.cli import EXIT_PARTIAL, OUTPUT_JSON, RPC_BY_NAME, _run_rpc_broadcast

    clients = {
        9001: _mock_client(node_exists=AsyncMock(return_value=True)),
        9002: _mock_client(node_exists=AsyncMock(return_value=False)),
    }
    with patch(
        "godot_cli_control.cli.GameClient", side_effect=lambda port: clients[port]
    ):
        rc = asyncio.run(
            _run_rpc_broadcast(RPC_BY_NAME["exists"], _broadcast_ns(), OUTPUT_JSON)
        )

    assert rc == EXIT_PARTIAL
    by_name = {
        e["instance"]: e
        for e in json.loads(capsys.readouterr().out.strip())["result"]["instances"]
    }
    assert by_name["b"] == {"instance": "b", "ok": True, "result": False, "rc": 1}


def test_broadcast_connection_error_entry_rc2(
    two_instances: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """单实例连接失败：entry rc=2 / code=-1001，不拖死另一实例；聚合 rc=3。"""
    from godot_cli_control.cli import EXIT_PARTIAL, OUTPUT_JSON, RPC_BY_NAME, _run_rpc_broadcast

    dead = AsyncMock()
    dead.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))
    dead.__aexit__ = AsyncMock(return_value=None)
    clients = {
        9001: _mock_client(node_exists=AsyncMock(return_value=True)),
        9002: dead,
    }
    with patch(
        "godot_cli_control.cli.GameClient", side_effect=lambda port: clients[port]
    ):
        rc = asyncio.run(
            _run_rpc_broadcast(RPC_BY_NAME["exists"], _broadcast_ns(), OUTPUT_JSON)
        )

    assert rc == EXIT_PARTIAL
    by_name = {
        e["instance"]: e
        for e in json.loads(capsys.readouterr().out.strip())["result"]["instances"]
    }
    assert by_name["a"]["ok"] is True
    assert by_name["b"]["rc"] == 2
    assert by_name["b"]["error"]["code"] == -1001


def test_broadcast_port_file_missing_entry_rc2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """探活后端口文件读不到（启动中/刚死瞬态）：按连接错落 entry rc=2。"""
    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_PARTIAL, OUTPUT_JSON, RPC_BY_NAME, _run_rpc_broadcast

    monkeypatch.setattr(daemon_mod, "list_live_instances", lambda root=None: ["a", "b"])
    monkeypatch.setattr(
        daemon_mod.Daemon,
        "current_port",
        lambda self: {"a": 9001, "b": None}[self.instance],
    )
    clients = {9001: _mock_client(node_exists=AsyncMock(return_value=True))}
    with patch(
        "godot_cli_control.cli.GameClient", side_effect=lambda port: clients[port]
    ):
        rc = asyncio.run(
            _run_rpc_broadcast(RPC_BY_NAME["exists"], _broadcast_ns(), OUTPUT_JSON)
        )

    assert rc == EXIT_PARTIAL
    by_name = {
        e["instance"]: e
        for e in json.loads(capsys.readouterr().out.strip())["result"]["instances"]
    }
    assert by_name["b"]["rc"] == 2
    assert by_name["b"]["error"]["code"] == -1001
    assert "port" in by_name["b"]["error"]["message"]


def test_broadcast_no_live_instances_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """0 个活实例 → -1006 + exit 2（同「daemon 没起」语义类）；message 提示
    legacy 平铺 daemon 不是广播目标。"""
    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_INFRA_ERROR, OUTPUT_JSON, RPC_BY_NAME, _run_rpc_broadcast

    monkeypatch.setattr(daemon_mod, "list_live_instances", lambda root=None: [])
    rc = asyncio.run(
        _run_rpc_broadcast(RPC_BY_NAME["exists"], _broadcast_ns(), OUTPUT_JSON)
    )
    assert rc == EXIT_INFRA_ERROR
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1006


def test_broadcast_single_instance_keeps_envelope_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 个活实例仍走广播信封（数组长度 1）——形状确定性优先，agent 不用分支解析。"""
    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc_broadcast

    monkeypatch.setattr(daemon_mod, "list_live_instances", lambda root=None: ["solo"])
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9001)
    clients = {9001: _mock_client(node_exists=AsyncMock(return_value=True))}
    with patch(
        "godot_cli_control.cli.GameClient", side_effect=lambda port: clients[port]
    ):
        rc = asyncio.run(
            _run_rpc_broadcast(RPC_BY_NAME["exists"], _broadcast_ns(), OUTPUT_JSON)
        )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert [e["instance"] for e in payload["result"]["instances"]] == ["solo"]


def test_broadcast_substitutes_instance_per_target(
    two_instances: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """handler 收到的 namespace 已做逐实例 {instance} 替换（通用机制贯通广播路径）。"""
    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc_broadcast

    seen: dict[int, str] = {}

    def _exists_for(port: int) -> Any:
        async def _exists(path: str) -> bool:
            seen[port] = path
            return True

        return _exists

    clients = {
        p: _mock_client(node_exists=AsyncMock(side_effect=_exists_for(p)))
        for p in (9001, 9002)
    }
    with patch(
        "godot_cli_control.cli.GameClient", side_effect=lambda port: clients[port]
    ):
        rc = asyncio.run(
            _run_rpc_broadcast(
                RPC_BY_NAME["exists"],
                _broadcast_ns(node_path="/root/{instance}"),
                OUTPUT_JSON,
            )
        )
    assert rc == 0
    assert seen == {9001: "/root/a", 9002: "/root/b"}
    capsys.readouterr()  # 清掉本测试不关心的信封输出


def test_broadcast_text_mode_prefixes_instance(
    two_instances: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """--text：成功行 stdout 带 [name] 前缀；失败行走 stderr。"""
    from godot_cli_control.cli import EXIT_PARTIAL, OUTPUT_TEXT, RPC_BY_NAME, _run_rpc_broadcast
    from godot_cli_control.client import RpcError

    clients = {
        9001: _mock_client(node_exists=AsyncMock(return_value=True)),
        9002: _mock_client(node_exists=AsyncMock(side_effect=RpcError(1002, "boom"))),
    }
    with patch(
        "godot_cli_control.cli.GameClient", side_effect=lambda port: clients[port]
    ):
        rc = asyncio.run(
            _run_rpc_broadcast(RPC_BY_NAME["exists"], _broadcast_ns(), OUTPUT_TEXT)
        )
    assert rc == EXIT_PARTIAL
    captured = capsys.readouterr()
    assert captured.out.strip().startswith("[a] ")
    assert "[b] " in captured.err
    assert "boom" in captured.err


def test_main_dispatches_instance_all_to_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main()：--instance all + RPC 子命令分流进 _run_rpc_broadcast，
    退出码原样透传，不走单实例端口发现。"""
    import godot_cli_control.cli as cli_mod

    called: dict[str, Any] = {}

    async def fake_broadcast(spec: Any, ns: Any, fmt: str) -> int:
        called["spec"] = spec.name
        called["instance"] = ns.instance
        return 3

    monkeypatch.setattr(cli_mod, "_run_rpc_broadcast", fake_broadcast)
    monkeypatch.setattr(
        sys, "argv", ["godot-cli-control", "--instance", "all", "exists", "/root/Foo"]
    )
    with pytest.raises(SystemExit) as ei:
        cli_mod.main()
    assert ei.value.code == 3
    assert called == {"spec": "exists", "instance": "all"}


def test_rpc_failure_envelope_network_oserror_is_connection() -> None:
    """裸 OSError 带网络 errno（ECONNREFUSED）→ -1001：钉死 _is_network_oserror
    True 分支（OSError 内部唯一的条件二分，原参数矩阵没覆盖到）。"""
    import errno

    from godot_cli_control.cli import _rpc_failure_envelope

    try:
        raise OSError(errno.ECONNREFUSED, "refused")
    except Exception as e:  # noqa: BLE001
        code, _msg, rc = _rpc_failure_envelope(e)
    assert (code, rc) == (-1001, 2)


def test_rpc_failure_envelope_real_jsondecodeerror() -> None:
    """真实 json.JSONDecodeError（set/call value 解析失败的实际异常型）→ -1003/64。"""
    from godot_cli_control.cli import _rpc_failure_envelope

    try:
        json.loads("{bad")
    except json.JSONDecodeError as e:
        code, _msg, rc = _rpc_failure_envelope(e)
    assert (code, rc) == (-1003, 64)


def test_rpc_failure_envelope_internal_prints_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """兜底分支唯一可观测副作用：traceback 落 stderr（绝不进 stdout）；
    非兜底分支 stderr 必须干净。"""
    from godot_cli_control.cli import _rpc_failure_envelope

    try:
        raise KeyError("boom")
    except Exception as e:  # noqa: BLE001
        _rpc_failure_envelope(e)
    captured = capsys.readouterr()
    assert "KeyError" in captured.err
    assert captured.out == ""

    try:
        raise ValueError("usage")
    except Exception as e:  # noqa: BLE001
        _rpc_failure_envelope(e)
    captured = capsys.readouterr()
    assert captured.err == ""
