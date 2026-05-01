"""CLI 单元测试 —— 覆盖 ``_exec_user_script`` 的脚本加载边界。

不实际启动 Godot：用 monkeypatch 替换 ``GameBridge``，只验证 importer 行为。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def stub_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 GameBridge 换成不连真 daemon 的桩，让脚本只跑 import 路径。"""

    class _StubBridge:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def close(self) -> None:
            pass

    import godot_cli_control.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_exec_user_script", cli_mod._exec_user_script)
    # bridge 是延迟 import，patch 模块级以便后续 _exec_user_script 拿到桩
    import godot_cli_control.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "GameBridge", _StubBridge)


def _write(p: Path, body: str) -> None:
    p.write_text(body, encoding="utf-8")


def test_exec_user_script_can_import_sibling_module(
    tmp_path: Path, stub_bridge: None
) -> None:
    """脚本 ``from helpers import foo``（同目录辅助）必须能解析。

    P1 修复：早期版本不把 script_path.parent 注入 sys.path，导致只能写单文件
    脚本，稍微复杂一点的 e2e 用例就要 PYTHONPATH 手动 hack。
    """
    helpers = tmp_path / "helpers.py"
    _write(helpers, "VALUE = 'imported_ok'\n")

    script = tmp_path / "user_script.py"
    _write(
        script,
        "from helpers import VALUE\n"
        "def run(bridge):\n"
        "    assert VALUE == 'imported_ok'\n",
    )

    from godot_cli_control.cli import _exec_user_script

    rc = _exec_user_script(script, port=9999)
    assert rc == 0


def test_exec_user_script_registers_module_for_dataclass_lookup(
    tmp_path: Path, stub_bridge: None
) -> None:
    """``sys.modules['user_script']`` 必须指向加载的脚本 —— pickle / dataclass
    在 ``__module__ == 'user_script'`` 上查类时依赖此注册。"""
    script = tmp_path / "user_script.py"
    _write(
        script,
        "import sys\n"
        "class Marker: pass\n"
        "def run(bridge):\n"
        "    assert sys.modules['user_script'].Marker is Marker\n",
    )

    from godot_cli_control.cli import _exec_user_script

    rc = _exec_user_script(script, port=9999)
    assert rc == 0


def test_exec_user_script_returns_1_on_missing_run(
    tmp_path: Path, stub_bridge: None
) -> None:
    script = tmp_path / "user_script.py"
    _write(script, "x = 1\n")

    from godot_cli_control.cli import _exec_user_script

    rc = _exec_user_script(script, port=9999)
    assert rc == 1


def test_exec_user_script_friendly_error_on_connection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon 没起 → GameBridge 抛 ConnectionError 时，cmd_run 必须给一行
    可读提示，而不是把 traceback 直接喷给用户。"""

    def _raise_conn(*_: Any, **__: Any) -> None:
        raise ConnectionError("Failed to connect after 1 attempts")

    import godot_cli_control.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "GameBridge", _raise_conn)

    script = tmp_path / "user_script.py"
    _write(script, "def run(bridge):\n    pass\n")

    from godot_cli_control.cli import _exec_user_script

    rc = _exec_user_script(script, port=9999)
    assert rc == 1

    captured = capsys.readouterr()
    # 友好提示包含端口、原因、引导用户起 daemon 的 hint
    assert "连接 daemon 失败" in captured.err
    assert "9999" in captured.err
    assert "daemon start" in captured.err
    # 不是 raw traceback：不应出现 Traceback 头
    assert "Traceback (most recent call last)" not in captured.err


def test_exec_user_script_cleans_up_sys_path_and_sys_modules(
    tmp_path: Path, stub_bridge: None
) -> None:
    """连跑两次后，sys.path 头部不应累积 tmp_path、sys.modules['user_script'] 应被删。

    pytest 在同一进程跑多个用例，本函数若不 finally 还原会污染后续测试。
    """
    import sys as _sys

    from godot_cli_control.cli import _exec_user_script

    snapshot_path = list(_sys.path)
    snapshot_modules = "user_script" in _sys.modules

    script = tmp_path / "user_script.py"
    _write(script, "def run(bridge):\n    pass\n")

    assert _exec_user_script(script, port=9999) == 0
    assert _exec_user_script(script, port=9999) == 0

    # sys.path 至少不应在头部多出脚本目录
    assert _sys.path == snapshot_path, (
        "sys.path 被污染："
        f"前={snapshot_path[:3]}... 后={_sys.path[:3]}..."
    )
    # sys.modules 不应留有 user_script 引用（除非测试启动前就有，那就保持原样）
    assert ("user_script" in _sys.modules) is snapshot_modules


# ── init 子命令的 skill 互斥参数 ──


def test_init_subcommand_accepts_no_skills_flag() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--no-skills"])
    assert ns.cmd == "init"
    assert ns.no_skills is True
    assert ns.skills_only is False


def test_init_subcommand_accepts_skills_only_flag() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--skills-only"])
    assert ns.no_skills is False
    assert ns.skills_only is True


def test_init_subcommand_rejects_both_flags() -> None:
    """argparse mutually_exclusive_group 应让两者并存触发 SystemExit。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["init", "--no-skills", "--skills-only"])


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    """``-V`` / ``--version`` 必须打印版本并 exit 0。SKILL.md 渲染依赖该版本号
    保持可用，回归保护。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "godot-cli-control" in out


def test_daemon_status_subcommand_parses() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["daemon", "status"])
    assert ns.cmd == "daemon"
    assert ns.action == "status"


def test_daemon_status_returns_1_when_not_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """无 daemon 时 ``daemon status`` 必须输出 ``stopped`` 且 exit 1，
    供 shell ``if … status; then …`` 直接判分支。"""
    import godot_cli_control.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: False)

    from godot_cli_control.cli import cmd_daemon_status

    rc = cmd_daemon_status(__import__("argparse").Namespace())
    assert rc == 1
    assert "stopped" in capsys.readouterr().out


def test_daemon_status_returns_0_when_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import godot_cli_control.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: True)
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: 4242)
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9877)

    from godot_cli_control.cli import cmd_daemon_status

    rc = cmd_daemon_status(__import__("argparse").Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "running" in out and "4242" in out and "9877" in out


def test_format_full_help_covers_every_subcommand() -> None:
    """``format_full_help`` 必须遍历到所有顶层子命令 + daemon 三动作。
    SKILL.md 完全依赖这一点 —— 漏一个，agent 那侧的 -h 就要靠 shell 出去查。"""
    from godot_cli_control.cli import RPC_SPECS, format_full_help

    full = format_full_help()
    for spec in RPC_SPECS:
        assert f"godot-cli-control {spec.name} --help" in full, (
            f"format_full_help 漏了 RPC 子命令：{spec.name}"
        )
    for action in ("start", "stop", "status"):
        assert f"godot-cli-control daemon {action} --help" in full
    for top in ("init", "run"):
        assert f"godot-cli-control {top} --help" in full


def test_combo_help_documents_step_schema() -> None:
    """``combo -h`` 的 epilog 必须含真实 step schema：之前 SKILL.md 文案错把
    schema 写成 ``press/release/wait``，agent 写出来的 combo 文件全跑不通。"""
    from godot_cli_control.cli import build_parser

    parser = build_parser()
    sub = next(
        sp
        for action in parser._actions  # noqa: SLF001
        if hasattr(action, "choices") and action.choices is not None
        for name, sp in action.choices.items()
        if name == "combo"
    )
    epilog = sub.epilog or ""
    assert '"action"' in epilog and '"wait"' in epilog
    assert "duration" in epilog


def test_init_subcommand_accepts_skills_no_clobber() -> None:
    """--skills-no-clobber 正交于互斥组，可单独使用、也可搭 --skills-only。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--skills-no-clobber"])
    assert ns.skills_no_clobber is True
    assert ns.no_skills is False
    assert ns.skills_only is False

    ns2 = build_parser().parse_args(
        ["init", "--skills-only", "--skills-no-clobber"]
    )
    assert ns2.skills_only is True
    assert ns2.skills_no_clobber is True


# ── 0.2.0 AI-friendly CLI 改造（--json 默认开 + 12 个新 RPC 子命令） ──


@pytest.mark.parametrize(
    "argv,expected_attrs",
    [
        # 读
        (["get", "/root/Main", "position"], {"node_path": "/root/Main", "prop": "position"}),
        (["text", "/root/Main/Title"], {"node_path": "/root/Main/Title"}),
        (["exists", "/root/Foo"], {"node_path": "/root/Foo"}),
        (["visible", "/root/Hud"], {"node_path": "/root/Hud"}),
        (["children", "/root/Main"], {"node_path": "/root/Main", "type_filter": None}),
        (["children", "/root/Main", "Button"], {"node_path": "/root/Main", "type_filter": "Button"}),
        # 写
        (["set", "/root/Main", "score", "42"], {"node_path": "/root/Main", "prop": "score", "value": "42"}),
        (["call", "/root/Main", "start_game"], {"node_path": "/root/Main", "method": "start_game"}),
        (["call", "/root/Main", "go", "1", "true"], {"node_path": "/root/Main", "method": "go", "args": ["1", "true"]}),
        # 等待
        (["wait-node", "/root/Boss"], {"node_path": "/root/Boss", "timeout": None}),
        (["wait-node", "/root/Boss", "3"], {"node_path": "/root/Boss", "timeout": "3"}),
        (["wait-time", "0.5"], {"seconds": "0.5"}),
        # 状态 / 发现
        (["pressed"], {}),
        (["combo-cancel"], {}),
        (["actions"], {"all": False}),
        (["actions", "--all"], {"all": True}),
    ],
)
def test_new_rpc_subcommands_parse(
    argv: list[str], expected_attrs: dict[str, Any]
) -> None:
    """每个新增子命令 argparse 装配正确。零 daemon 依赖。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(argv)
    assert ns.cmd == argv[0]
    for attr, expected in expected_attrs.items():
        assert getattr(ns, attr) == expected, (
            f"{argv[0]}: ns.{attr} 期望 {expected!r}，实际 {getattr(ns, attr)!r}"
        )


def test_screenshot_now_requires_path() -> None:
    """0.2.0 BREAKING：screenshot 不再支持省略 output_path（旧版本会喷 base64 到
    stdout，把 LLM 上下文撑爆）。argparse 必须直接拒绝。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["screenshot"])


def test_screenshot_with_path_parses() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["screenshot", "/tmp/x.png"])
    assert ns.output_path == "/tmp/x.png"


def test_combo_inline_steps_json_parses() -> None:
    """combo --steps-json 应解析成 ns.steps_json 字符串。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(
        ["combo", "--steps-json", '[{"action":"jump"}]']
    )
    assert ns.steps_json == '[{"action":"jump"}]'
    assert ns.json_file is None


def test_combo_stdin_dash_parses() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["combo", "-"])
    assert ns.json_file == "-"
    assert ns.steps_json is None


def test_combo_file_path_parses() -> None:
    """旧路径 ``combo combo.json`` 必须保留兼容。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["combo", "combo.json"])
    assert ns.json_file == "combo.json"
    assert ns.steps_json is None


def test_combo_read_steps_rejects_double_source(tmp_path: Path) -> None:
    """同时给 file 和 --steps-json 应在 _read_combo_steps 报错。"""
    from godot_cli_control.cli import _read_combo_steps, build_parser

    f = tmp_path / "c.json"
    f.write_text("[]", encoding="utf-8")
    ns = build_parser().parse_args(
        ["combo", str(f), "--steps-json", "[]"]
    )
    with pytest.raises(ValueError, match="互斥"):
        _read_combo_steps(ns)


def test_combo_read_steps_requires_some_source() -> None:
    from godot_cli_control.cli import _read_combo_steps, build_parser

    ns = build_parser().parse_args(["combo"])
    with pytest.raises(ValueError, match="必须提供"):
        _read_combo_steps(ns)


def test_combo_read_steps_unwraps_object_form() -> None:
    """``{"steps": [...]}`` 形态必须被解开成裸数组。"""
    from godot_cli_control.cli import _read_combo_steps, build_parser

    ns = build_parser().parse_args(
        ["combo", "--steps-json", '{"steps":[{"action":"jump"}]}']
    )
    steps = _read_combo_steps(ns)
    assert steps == [{"action": "jump"}]


def test_parse_json_arg_falls_back_to_string() -> None:
    """set/call 的参数：是 JSON 就解析，否则当字符串。"""
    from godot_cli_control.cli import _parse_json_arg

    assert _parse_json_arg("42") == 42
    assert _parse_json_arg('"hi"') == "hi"
    assert _parse_json_arg("[1, 2]") == [1, 2]
    assert _parse_json_arg('{"a": 1}') == {"a": 1}
    assert _parse_json_arg("hello") == "hello"  # 非 JSON → 字符串
    assert _parse_json_arg("/root/Main") == "/root/Main"


def test_output_format_default_is_json() -> None:
    """0.2.0 默认 ``--json`` 开。``--text`` / ``--no-json`` 切回旧行为。"""
    from godot_cli_control.cli import OUTPUT_JSON, OUTPUT_TEXT, build_parser

    assert build_parser().parse_args(["pressed"]).output_format == OUTPUT_JSON
    assert (
        build_parser().parse_args(["--text", "pressed"]).output_format
        == OUTPUT_TEXT
    )
    assert (
        build_parser().parse_args(["--no-json", "pressed"]).output_format
        == OUTPUT_TEXT
    )
    assert (
        build_parser().parse_args(["--json", "--text", "pressed"]).output_format
        == OUTPUT_TEXT
    )  # 后者覆盖前者


def test_daemon_status_json_envelope_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON 模式下 daemon status 必须输出统一信封；老 ``running pid=…`` 行只在
    --text 模式下保留。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: True)
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: 4242)
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9877)

    from godot_cli_control.cli import OUTPUT_JSON, cmd_daemon_status

    ns = __import__("argparse").Namespace(output_format=OUTPUT_JSON)
    rc = cmd_daemon_status(ns)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "result": {"state": "running", "pid": 4242, "port": 9877},
    }


def test_daemon_status_json_envelope_stopped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json as _json

    import godot_cli_control.daemon as daemon_mod

    # chdir tmp_path 避免读到工作目录里残留的 .cli_control/godot.log
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: False)

    from godot_cli_control.cli import OUTPUT_JSON, cmd_daemon_status

    ns = __import__("argparse").Namespace(output_format=OUTPUT_JSON)
    rc = cmd_daemon_status(ns)
    assert rc == 1  # exit code 语义不变
    payload = _json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "result": {"state": "stopped"}}


def test_daemon_status_stopped_includes_last_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #38：daemon 已停 + .cli_control/godot.log 存在 → status 必须把
    日志路径透出来，让用户立刻知道去哪 cat 上次启动失败的原因。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod

    monkeypatch.chdir(tmp_path)
    control_dir = tmp_path / ".cli_control"
    control_dir.mkdir()
    log = control_dir / "godot.log"
    log.write_text("ERROR: autoload failed\n")

    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: False)

    from godot_cli_control.cli import OUTPUT_JSON, cmd_daemon_status

    ns = __import__("argparse").Namespace(output_format=OUTPUT_JSON)
    rc = cmd_daemon_status(ns)
    assert rc == 1
    payload = _json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["state"] == "stopped"
    assert payload["result"]["last_log"] == str(log)


def test_daemon_status_stopped_includes_last_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #38 进阶：last_exit_code 文件存在时，JSON 必须把退出码透出。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod

    monkeypatch.chdir(tmp_path)
    control_dir = tmp_path / ".cli_control"
    control_dir.mkdir()
    (control_dir / "godot.log").write_text("FATAL\n")
    (control_dir / "last_exit_code").write_text("137")

    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: False)

    from godot_cli_control.cli import OUTPUT_JSON, cmd_daemon_status

    ns = __import__("argparse").Namespace(output_format=OUTPUT_JSON)
    rc = cmd_daemon_status(ns)
    assert rc == 1
    payload = _json.loads(capsys.readouterr().out)
    assert payload["result"]["last_exit_code"] == 137
    assert payload["result"]["last_log"].endswith("godot.log")


def test_daemon_status_stopped_text_mode_includes_last_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """text 模式下也要带 last exit + log 提示，否则人类用户读不到。"""
    import godot_cli_control.daemon as daemon_mod

    monkeypatch.chdir(tmp_path)
    control_dir = tmp_path / ".cli_control"
    control_dir.mkdir()
    (control_dir / "godot.log").write_text("crash\n")
    (control_dir / "last_exit_code").write_text("11")

    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: False)

    from godot_cli_control.cli import OUTPUT_TEXT, cmd_daemon_status

    ns = __import__("argparse").Namespace(output_format=OUTPUT_TEXT)
    rc = cmd_daemon_status(ns)
    assert rc == 1
    out = capsys.readouterr().out
    assert "stopped" in out
    assert "last exit: 11" in out
    assert "godot.log" in out


def test_run_rpc_emits_success_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """成功路径：dispatcher 必须输出 ``{"ok": true, "result": ...}``，exit 0。"""
    import json as _json

    from godot_cli_control.cli import (
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )

    spec = RPC_BY_NAME["exists"]
    ns = __import__("argparse").Namespace(node_path="/root/Main")

    # mock GameClient context manager + node_exists
    from unittest.mock import AsyncMock, patch

    mock_client = AsyncMock()
    mock_client.node_exists = AsyncMock(return_value=True)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 0  # exists=True → exit_from_bool 给 0
    out = capsys.readouterr().out.strip()
    assert _json.loads(out) == {"ok": True, "result": True}


def test_run_rpc_exists_false_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``exists`` 的 exit_code_from 必须把 False 转成退出码 1，shell ``if`` 友好。"""
    import json as _json

    from godot_cli_control.cli import (
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["exists"]
    ns = __import__("argparse").Namespace(node_path="/root/Nope")

    mock_client = AsyncMock()
    mock_client.node_exists = AsyncMock(return_value=False)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 1
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": True, "result": False}


def test_run_rpc_emits_error_envelope_on_rpc_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """RpcError 必须落进 ``{"ok": false, "error": {"code": N, "message": "..."}}``。"""
    import json as _json

    from godot_cli_control.cli import (
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )
    from godot_cli_control.client import RpcError
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["click"]
    ns = __import__("argparse").Namespace(node_path="/root/Nope")

    mock_client = AsyncMock()
    mock_client.click = AsyncMock(
        side_effect=RpcError(1001, "node not found")
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 1
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "ok": False,
        "error": {"code": 1001, "message": "node not found"},
    }


def test_run_rpc_emits_error_envelope_on_connection_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon 没起 / proxy 干扰：连接异常被收口成 infra error，exit 2。"""
    import json as _json

    from godot_cli_control.cli import (
        CLIENT_CODE_CONNECTION,
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["pressed"]
    ns = __import__("argparse").Namespace()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(
        side_effect=ConnectionError("Failed to connect after 1 attempts")
    )
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 2
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == CLIENT_CODE_CONNECTION
    assert "connect" in payload["error"]["message"].lower()


def test_combo_preflight_rejects_no_steps_before_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``combo`` 不带 steps 时必须在连 daemon **之前**报 EXIT_USAGE，
    避免 agent 等 30s 连接 retry 才看到错误。"""
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, build_parser, main

    # 把 GameClient 替成会爆的桩 —— 如果 preflight 没生效、跑到连接环节就立刻看见。
    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError(
                "preflight 失效：combo 用法错误时不应该尝试连 daemon"
            )

    import godot_cli_control.cli as cli_mod

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "combo"])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "必须提供" in payload["error"]["message"]


def test_combo_preflight_caches_steps_to_avoid_stdin_double_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdin 流不能读两次：preflight 必须把解析结果缓存到 ns，handler 复用。"""
    import io

    from godot_cli_control.cli import _preflight_combo, _read_combo_steps, build_parser

    monkeypatch.setattr(sys, "stdin", io.StringIO('[{"action":"jump"}]'))
    ns = build_parser().parse_args(["combo", "-"])
    _preflight_combo(ns)
    assert getattr(ns, "_combo_steps", None) == [{"action": "jump"}]
    # 此时 stdin 已经被读完；二次调用 _read_combo_steps 会拿到空字符串、JSON 解析失败。
    # 但 cmd_combo 走的是缓存，不会触发再读。


def test_daemon_start_emits_json_envelope_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """daemon start 在 --json 默认下也要输出 ``{"ok":true,"result":{"started":true,...}}``，
    跟 daemon status 信封形状对齐，让 agent 一套 jq 处理三个命令。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.Daemon, "start", lambda self, **kw: None)
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9877)
    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: True)
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: 1234)

    from godot_cli_control.cli import (
        OUTPUT_JSON,
        cmd_daemon_start,
    )

    ns = __import__("argparse").Namespace(
        record=False,
        movie_path=None,
        headless=True,
        fps=30,
        port=9877,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_start(ns)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["result"]["started"] is True
    assert payload["result"]["port"] == 9877
    assert payload["result"]["pid"] == 1234


def test_daemon_start_text_mode_keeps_silent_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--text`` 模式 daemon start 成功仍然不输出（旧行为保留）。"""
    import godot_cli_control.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.Daemon, "start", lambda self, **kw: None)
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9877)
    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: True)
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: 1234)

    from godot_cli_control.cli import OUTPUT_TEXT, cmd_daemon_start

    ns = __import__("argparse").Namespace(
        record=False,
        movie_path=None,
        headless=True,
        fps=30,
        port=9877,
        output_format=OUTPUT_TEXT,
    )
    assert cmd_daemon_start(ns) == 0
    assert capsys.readouterr().out == ""


def test_daemon_stop_emits_json_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """daemon stop 在 --json 默认下输出信封；rc 透出（让 agent 知道转码是否成功）。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.Daemon, "stop", lambda self: 2)

    from godot_cli_control.cli import OUTPUT_JSON, cmd_daemon_stop

    ns = __import__("argparse").Namespace(output_format=OUTPUT_JSON)
    rc = cmd_daemon_stop(ns)
    assert rc == 2  # exit code 直通：agent 既能从 stdout JSON 拿到 rc=2，也能从 $? 看
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": True, "result": {"stopped": True, "rc": 2}}


def test_cmd_set_passes_json_parsed_value_to_client(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``set /Foo pos '[10, 20]'`` 必须把 [10, 20] 数组 forward 给
    client.set_property，**不是字符串 "[10, 20]"**。这是 _parse_json_arg
    的契约，回归保护。"""
    from godot_cli_control.cli import (
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["set"]
    ns = __import__("argparse").Namespace(
        node_path="/root/Foo",
        prop="position",
        value="[10, 20]",
    )

    mock_client = AsyncMock()
    mock_client.set_property = AsyncMock(return_value={"success": True})
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 0
    mock_client.set_property.assert_awaited_once_with(
        "/root/Foo", "position", [10, 20]
    )


def test_cmd_set_falls_back_to_string_on_non_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """非 JSON 字符串（如 ``hello``）应原样作为字符串透传，不强行报错。"""
    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["set"]
    ns = __import__("argparse").Namespace(
        node_path="/root/Label", prop="text", value="hello"
    )

    mock_client = AsyncMock()
    mock_client.set_property = AsyncMock(return_value={"success": True})
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    mock_client.set_property.assert_awaited_once_with(
        "/root/Label", "text", "hello"
    )


def test_cmd_call_parses_each_arg_independently(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``call /Game go 1 "easy" [1,2]`` 应把每个 arg 独立按 JSON-or-string 解析。"""
    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["call"]
    ns = __import__("argparse").Namespace(
        node_path="/root/Game",
        method="go",
        args=["1", '"easy"', "[1, 2]", "raw_string"],
    )

    mock_client = AsyncMock()
    mock_client.call_method = AsyncMock(return_value=None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    mock_client.call_method.assert_awaited_once_with(
        "/root/Game", "go", [1, "easy", [1, 2], "raw_string"]
    )


def test_run_rpc_emits_envelope_on_unexpected_internal_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """客户端内部 bug（AttributeError / KeyError 等）也必须落进信封，
    绝不让 traceback 漏到 stdout 破坏 ``--json`` 契约。"""
    import json as _json

    from godot_cli_control.cli import (
        CLIENT_CODE_INTERNAL,
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["click"]
    ns = __import__("argparse").Namespace(node_path="/root/Main")

    mock_client = AsyncMock()
    # 模拟客户端内部 bug：handler 路径上抛非业务异常
    mock_client.click = AsyncMock(side_effect=AttributeError("nope"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 2  # EXIT_INFRA_ERROR
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)  # 必须是合法 JSON，traceback 不能污染
    assert payload["ok"] is False
    assert payload["error"]["code"] == CLIENT_CODE_INTERNAL
    assert "AttributeError" in payload["error"]["message"]


def test_run_rpc_separates_local_io_error_from_connection(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``screenshot`` 写盘失败必须报 -1004 (IO)，不能误标 -1001 (connection)，
    否则 agent 会去重启 daemon 浪费一轮。"""
    import json as _json

    from godot_cli_control.cli import (
        CLIENT_CODE_IO,
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["screenshot"]
    # 指向只读目录的子路径：mkdir / write_bytes 必失败
    bad_path = tmp_path / "ro_dir" / "out.png"
    (tmp_path / "ro_dir").mkdir()
    (tmp_path / "ro_dir").chmod(0o400)  # 只读
    ns = __import__("argparse").Namespace(output_path=str(bad_path))

    mock_client = AsyncMock()
    mock_client.screenshot = AsyncMock(return_value=b"fake png bytes")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    try:
        with patch(
            "godot_cli_control.cli.GameClient", return_value=mock_client
        ):
            rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))
    finally:
        (tmp_path / "ro_dir").chmod(0o700)  # 让 tmp_path 清理能跑

    assert rc == 2
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == CLIENT_CODE_IO, (
        f"本地 IO 错误必须用 -1004，不能挤进 -1001 (connection)；"
        f"实际：{payload['error']}"
    )


def test_combo_dash_rejects_tty_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``combo -`` 在 TTY 上不能 read() 阻塞；preflight 应报 ValueError。"""
    import io

    from godot_cli_control.cli import _read_combo_steps, build_parser

    fake_stdin = io.StringIO("")
    fake_stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    ns = build_parser().parse_args(["combo", "-"])
    with pytest.raises(ValueError, match="TTY"):
        _read_combo_steps(ns)


def test_run_rpc_text_mode_uses_text_formatter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--text`` 下应输出 spec.text_formatter 的结果，不裹信封。"""
    from godot_cli_control.cli import (
        OUTPUT_TEXT,
        RPC_BY_NAME,
        _run_rpc,
    )
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["exists"]
    ns = __import__("argparse").Namespace(node_path="/root/Main")

    mock_client = AsyncMock()
    mock_client.node_exists = AsyncMock(return_value=True)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_TEXT))

    assert rc == 0
    assert capsys.readouterr().out.strip() == "true"


def test_daemon_ls_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")

    from godot_cli_control.cli import cmd_daemon_ls, OUTPUT_JSON
    import argparse

    rc = cmd_daemon_ls(argparse.Namespace(output_format=OUTPUT_JSON))
    out = capsys.readouterr().out
    assert rc == 0
    assert '"daemons": []' in out


def test_daemon_ls_lists_active_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    proj = tmp_path / "p"; proj.mkdir()
    registry.register(
        proj, pid=os.getpid(), port=12345, godot_bin="x", log_path="y"
    )

    from godot_cli_control.cli import cmd_daemon_ls, OUTPUT_JSON
    import argparse

    rc = cmd_daemon_ls(argparse.Namespace(output_format=OUTPUT_JSON))
    out = capsys.readouterr().out
    assert rc == 0
    assert "12345" in out
    assert str(proj.resolve()) in out


def test_daemon_ls_subcommand_parses() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "ls"])
    assert ns.cmd == "daemon"
    assert ns.action == "ls"
