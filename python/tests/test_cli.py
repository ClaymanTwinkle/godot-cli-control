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


def test_exec_user_script_returns_usage_on_missing_run(
    tmp_path: Path, stub_bridge: None
) -> None:
    """缺 run(bridge) 函数 → EXIT_USAGE(64)（#92 修复：用法错归 64）。"""
    script = tmp_path / "user_script.py"
    _write(script, "x = 1\n")

    from godot_cli_control.cli import EXIT_USAGE, _exec_user_script

    rc = _exec_user_script(script, port=9999)
    assert rc == EXIT_USAGE


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


class TestOutputFormatFlagsOnSubcommands:
    """--json / --text 必须能放在子命令前 *和* 后两种位置。

    AI agent 习惯把 flag 写在尾巴；早期 build_parser 只在顶层注册，
    `click /root/X --json` 会被 argparse 报 unrecognized。
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["click", "/root/X", "--json"],
            ["--json", "click", "/root/X"],
            ["click", "/root/X", "--text"],
            ["click", "/root/X", "--no-json"],
            ["exists", "/root/Foo", "--text"],
            ["tree", "3", "--json"],
            ["daemon", "status", "--json"],
            ["daemon", "stop", "--text"],
            ["daemon", "start", "--headless", "--json"],
        ],
    )
    def test_output_flag_accepted_at_tail(self, argv: list[str]) -> None:
        from godot_cli_control.cli import build_parser

        parser = build_parser()
        ns = parser.parse_args(argv)
        assert ns.output_format in ("json", "text")


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
        # 读（get props 改为 nargs='+'，ns.props 是 list）
        (["get", "/root/Main", "position"], {"node_path": "/root/Main", "props": ["position"]}),
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


# ── run / _exec_user_script JSON envelope（issue #50）──


def test_exec_user_script_json_success_emits_envelope(
    tmp_path: Path, stub_bridge: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """成功跑完 → stdout 一行 ``{"ok": true, "result": {"exit_code": 0, ...}}``。"""
    import json as _json

    from godot_cli_control.cli import _exec_user_script

    script = tmp_path / "user_script.py"
    _write(script, "def run(bridge):\n    pass\n")

    rc = _exec_user_script(script, port=9999, output_format="json")
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)
    assert payload["ok"] is True
    assert payload["result"]["exit_code"] == 0
    assert payload["result"]["script"] == str(script)


def test_exec_user_script_json_redirects_script_stdout_to_stderr(
    tmp_path: Path, stub_bridge: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """用户脚本里 print() 在 json 模式下必须走 stderr —— 否则会把 envelope 撕成两行。"""
    import json as _json

    from godot_cli_control.cli import _exec_user_script

    script = tmp_path / "user_script.py"
    _write(
        script,
        "def run(bridge):\n"
        "    print('LEAK_TO_STDOUT_IF_BROKEN')\n",
    )

    rc = _exec_user_script(script, port=9999, output_format="json")
    captured = capsys.readouterr()
    assert rc == 0
    out_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(out_lines) == 1, f"stdout 不是单行 envelope：{captured.out!r}"
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is True
    # 脚本的 print 必须能在 stderr 看到，便于人 debug
    assert "LEAK_TO_STDOUT_IF_BROKEN" in captured.err


def test_exec_user_script_json_error_on_runtime_exception(
    tmp_path: Path, stub_bridge: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 raise → stdout 一行 error envelope（CLIENT_CODE_SCRIPT_ERROR = -1005）；
    完整 traceback 仍在 stderr。"""
    import json as _json

    from godot_cli_control.cli import _exec_user_script

    script = tmp_path / "user_script.py"
    _write(
        script,
        "def run(bridge):\n"
        "    raise RuntimeError('boom from user script')\n",
    )

    rc = _exec_user_script(script, port=9999, output_format="json")
    captured = capsys.readouterr()
    assert rc == 1
    out_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(out_lines) == 1
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1005
    assert "RuntimeError" in payload["error"]["message"]
    assert "boom from user script" in payload["error"]["message"]
    # 完整 traceback 仍留在 stderr 给人看
    assert "Traceback" in captured.err
    assert "RuntimeError: boom from user script" in captured.err


def test_exec_user_script_json_error_on_missing_run(
    tmp_path: Path, stub_bridge: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺 run() → ``-1003`` (CLIENT_CODE_USAGE) envelope + EXIT_USAGE(64)（#92 修复）。"""
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, _exec_user_script

    script = tmp_path / "user_script.py"
    _write(script, "x = 1\n")

    rc = _exec_user_script(script, port=9999, output_format="json")
    out = capsys.readouterr().out.strip()
    assert rc == EXIT_USAGE
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "run(bridge)" in payload["error"]["message"]


def test_exec_user_script_json_error_on_connection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon 没起 → ``-1001`` (CLIENT_CODE_CONNECTION) envelope。"""
    import json as _json

    def _raise_conn(*_: Any, **__: Any) -> None:
        raise ConnectionError("Failed to connect after 1 attempts")

    import godot_cli_control.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "GameBridge", _raise_conn)

    script = tmp_path / "user_script.py"
    _write(script, "def run(bridge):\n    pass\n")

    from godot_cli_control.cli import _exec_user_script

    rc = _exec_user_script(script, port=9999, output_format="json")
    captured = capsys.readouterr()
    out = captured.out.strip()
    assert rc == 1
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1001
    assert "9999" in payload["error"]["message"]
    # 友好提示仍留在 stderr
    assert "daemon start" in captured.err


def test_cmd_run_emits_envelope_when_script_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``godot-cli-control run nonexistent.py --json`` → 单行 error envelope +
    EXIT_USAGE(64)。用户传错路径是"用法错"→ -1003 + 64（#92 修复）。"""
    import argparse
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, OUTPUT_JSON, cmd_run

    ns = argparse.Namespace(
        script=str(tmp_path / "does-not-exist.py"),
        record=False,
        movie_path=None,
        headless=False,
        gui=False,
        fps=30,
        port=0,
        idle_timeout="0",
        output_format=OUTPUT_JSON,
    )
    rc = cmd_run(ns)
    out = capsys.readouterr().out.strip()
    assert rc == EXIT_USAGE
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "does-not-exist.py" in payload["error"]["message"]


def test_cmd_run_emits_envelope_on_idle_timeout_parse_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--idle-timeout xyz`` 解析失败 → envelope (code -1003) + exit 64。"""
    import argparse
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, OUTPUT_JSON, cmd_run

    script = tmp_path / "s.py"
    script.write_text("def run(bridge): pass\n", encoding="utf-8")
    ns = argparse.Namespace(
        script=str(script),
        record=False,
        movie_path=None,
        headless=False,
        gui=False,
        fps=30,
        port=0,
        idle_timeout="totally-not-a-duration",
        output_format=OUTPUT_JSON,
    )
    rc = cmd_run(ns)
    out = capsys.readouterr().out.strip()
    assert rc == EXIT_USAGE
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


def test_exec_user_script_json_top_level_print_does_not_leak(
    tmp_path: Path, stub_bridge: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """用户脚本"顶层"（exec_module 阶段）print() 在 json 模式也必须走 stderr。
    redirect_stdout 范围若只包 module.run、不包 exec_module，顶层 print 会
    污染 envelope 单行契约——review P1 焦点。"""
    import json as _json

    from godot_cli_control.cli import _exec_user_script

    script = tmp_path / "user_script.py"
    _write(
        script,
        # 顶层 + run 都打——任一漏出 stdout 都会让 JSON 解析崩
        "print('TOP_LEVEL_LEAK')\n"
        "def run(bridge):\n"
        "    print('RUN_LEAK')\n",
    )

    rc = _exec_user_script(script, port=9999, output_format="json")
    captured = capsys.readouterr()
    assert rc == 0
    out_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(out_lines) == 1, (
        f"stdout 不是单行 envelope（顶层 print 漏了？）：{captured.out!r}"
    )
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is True
    assert payload["result"]["exit_code"] == 0
    # 两条 print 都应该在 stderr 仍能看到，便于 human debug
    assert "TOP_LEVEL_LEAK" in captured.err
    assert "RUN_LEAK" in captured.err


def test_exec_user_script_json_error_on_top_level_exception(
    tmp_path: Path, stub_bridge: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本顶层 raise（exec_module 阶段）也走 CLIENT_CODE_SCRIPT_ERROR；
    完整 traceback 仍在 stderr。"""
    import json as _json

    from godot_cli_control.cli import _exec_user_script

    script = tmp_path / "user_script.py"
    _write(
        script,
        "raise ImportError('cannot find frobnicator')\n"
        "def run(bridge): pass\n",
    )

    rc = _exec_user_script(script, port=9999, output_format="json")
    captured = capsys.readouterr()
    out = captured.out.strip()
    assert rc == 1
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1005
    assert "加载" in payload["error"]["message"]
    assert "ImportError" in payload["error"]["message"]
    assert "Traceback" in captured.err


def test_cmd_run_emits_envelope_on_daemon_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon.start raise DaemonError → envelope (-1006 PRECONDITION) + EXIT_INFRA_ERROR(2)。
    daemon 起不来是 infra 前置失败 → -1006 + exit 2（#92 修复）。"""
    import argparse
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_INFRA_ERROR, OUTPUT_JSON, cmd_run

    def _fake_is_running(self: Any) -> bool:
        return False

    def _fake_start(self: Any, **__: Any) -> None:
        raise daemon_mod.DaemonError("port already bound")

    monkeypatch.setattr(daemon_mod.Daemon, "is_running", _fake_is_running)
    monkeypatch.setattr(daemon_mod.Daemon, "start", _fake_start)

    script = tmp_path / "s.py"
    _write(script, "def run(bridge): pass\n")
    ns = argparse.Namespace(
        script=str(script),
        record=False,
        movie_path=None,
        headless=False,
        gui=False,
        fps=30,
        port=0,
        idle_timeout="0",
        output_format=OUTPUT_JSON,
    )
    rc = cmd_run(ns)
    out = capsys.readouterr().out.strip()
    assert rc == EXIT_INFRA_ERROR
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1006
    assert "port already bound" in payload["error"]["message"]


def test_cmd_run_catches_unexpected_exception_into_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_run 顶层未捕获异常必须落 envelope（CLIENT_CODE_INTERNAL = -1099）+
    EXIT_INFRA_ERROR(2)。CLAUDE.md 契约 1 ──"任何异常都必须落进信封"。"""
    import argparse
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_INFRA_ERROR, OUTPUT_JSON, cmd_run

    def _kaboom(self: Any) -> bool:
        # daemon.is_running 抛了不应抛的 OSError —— 模拟 race / FS 损坏
        raise OSError("disk on fire")

    monkeypatch.setattr(daemon_mod.Daemon, "is_running", _kaboom)

    script = tmp_path / "s.py"
    _write(script, "def run(bridge): pass\n")
    ns = argparse.Namespace(
        script=str(script),
        record=False,
        movie_path=None,
        headless=False,
        gui=False,
        fps=30,
        port=0,
        idle_timeout="0",
        output_format=OUTPUT_JSON,
    )
    rc = cmd_run(ns)
    captured = capsys.readouterr()
    out = captured.out.strip()
    assert rc == EXIT_INFRA_ERROR
    # envelope 是唯一 stdout，traceback 走 stderr
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1099
    assert "OSError" in payload["error"]["message"]
    assert "disk on fire" in payload["error"]["message"]
    assert "Traceback" in captured.err


def test_cmd_init_catches_unexpected_exception_into_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_init 顶层未捕获异常也落 envelope。模拟 run_init 内部 raise
    非预期异常（例如 reimport_project 抛 OSError）。"""
    import argparse
    import json as _json

    from godot_cli_control.cli import EXIT_INFRA_ERROR, OUTPUT_JSON, cmd_init

    def _boom(*_: Any, **__: Any) -> int:
        raise OSError("permission denied on addons/")

    monkeypatch.setattr("godot_cli_control.init_cmd.run_init", _boom)

    ns = argparse.Namespace(
        path=str(tmp_path),
        force=False,
        no_skills=False,
        skills_only=False,
        skills_no_clobber=False,
        no_gitignore=False,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_init(ns)
    captured = capsys.readouterr()
    out = captured.out.strip()
    assert rc == EXIT_INFRA_ERROR
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1099
    assert "OSError" in payload["error"]["message"]
    assert "Traceback" in captured.err


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

    from godot_cli_control.cli import EXIT_USAGE, main

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


@pytest.mark.parametrize("bad_duration", ["0", "-1.5", "abc"])
def test_hold_preflight_rejects_bad_duration_before_connecting(
    bad_duration: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``hold`` 的 duration <= 0 或非数字必须在连 daemon **之前**报 EXIT_USAGE。

    duration<=0 会让动作下一帧就释放（只生效一帧）；无限按住该用 ``press``。
    见 issue #71。"""
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：hold duration 非法时不应连 daemon")

    import godot_cli_control.cli as cli_mod

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "hold", "jump", bad_duration])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "duration" in payload["error"]["message"]


def test_hold_preflight_accepts_positive_duration() -> None:
    """合法 duration > 0 不应被 preflight 拦下（让它进到连接环节）。"""
    import argparse

    from godot_cli_control.cli import _preflight_hold

    ns = argparse.Namespace(duration="1.5")
    _preflight_hold(ns)  # 不抛即通过


def test_combo_preflight_caches_steps_to_avoid_stdin_double_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdin 流不能读两次：preflight 必须把解析结果缓存到 ns，handler 复用。"""
    import io

    from godot_cli_control.cli import _preflight_combo, build_parser

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


def test_daemon_start_rejects_record_with_explicit_headless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``daemon start --record --movie-path x --headless`` → ok:false 信封 +
    EXIT_INFRA_ERROR(2)，且 message 点名 headless（CultivationWorld #180）。

    显式 --headless 经 _resolve_headless 仍是 True，被 daemon.start 的 preflight
    拒绝。find_godot_binary mock 成 None 确保 RED 时不会真去 spawn Godot。"""
    import json as _json

    (tmp_path / "project.godot").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "godot_cli_control.daemon.find_godot_binary", lambda: None
    )

    from godot_cli_control.cli import (
        EXIT_INFRA_ERROR,
        OUTPUT_JSON,
        cmd_daemon_start,
    )

    ns = __import__("argparse").Namespace(
        record=True,
        movie_path=str(tmp_path / "demo.avi"),
        headless=True,
        gui=False,
        fps=30,
        port=0,
        idle_timeout="0",
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_start(ns)
    assert rc == EXIT_INFRA_ERROR
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "headless" in payload["error"]["message"]


def test_scene_change_preflight_rejects_bad_prefix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """scene-change 非 res://、uid:// 路径：连 daemon 前 -1003 + exit 64。

    main() 不接参数（读 sys.argv、以 sys.exit 退出）——必须用
    test_cli.py 现有 preflight 测试的 monkeypatch+SystemExit 模式。
    """
    import json as _json

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：scene-change 坏前缀时不应连 daemon")

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(
        sys, "argv", ["godot-cli-control", "scene-change", "second.tscn"]
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == EXIT_USAGE  # 64
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "res://" in payload["error"]["message"]


def test_scene_specs_registered() -> None:
    from godot_cli_control.cli import RPC_BY_NAME

    for name in ("scene-reload", "scene-change"):
        spec = RPC_BY_NAME[name]
        assert spec.preflight is not None
        assert spec.text_formatter({"scene_path": "res://m.tscn", "name": "M"})


def test_scene_text_formatters() -> None:
    from godot_cli_control.cli import RPC_BY_NAME

    r = {"scene_path": "res://main.tscn", "name": "Main"}
    assert RPC_BY_NAME["scene-reload"].text_formatter(r) == \
        "scene reloaded: res://main.tscn (root: Main)"
    assert RPC_BY_NAME["scene-change"].text_formatter(r) == \
        "scene changed: res://main.tscn (root: Main)"


@pytest.mark.parametrize("bad_timeout", ["-1", "0"])
def test_scene_reload_preflight_rejects_bad_timeout(
    bad_timeout: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """scene-reload --timeout <=0 必须在连 daemon 之前报 EXIT_USAGE。"""
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：scene-reload 非法 timeout 时不应连 daemon")

    import godot_cli_control.cli as cli_mod

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "scene-reload", "--timeout", bad_timeout])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "timeout" in payload["error"]["message"]


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
    assert payload["ok"] is True
    assert payload["result"]["stopped"] is True
    assert payload["result"]["rc"] == 2
    assert "project_root" in payload["result"]


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
    # 父级是个文件（不是目录），mkdir(parents=True, exist_ok=True) 必抛
    # FileExistsError —— 跨平台可靠（chmod 0o400 在 Windows 上不让目录只读）。
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    bad_path = blocker / "out.png"
    ns = __import__("argparse").Namespace(output_path=str(bad_path))

    mock_client = AsyncMock()
    mock_client.screenshot = AsyncMock(return_value=b"fake png bytes")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

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


# ── get 命令：多属性 + 信封透传（issue #99 / #100）──


def test_get_single_prop_envelope_passthrough(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """单属性 get 必须透传 RPC result（含 type），不拆包裸值。

    信封 result = {"value": [...], "type": "Vector2"}。
    agent 用 type 字段消歧，这是 issue #99 的核心契约。
    """
    import json as _json

    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["get"]
    ns = __import__("argparse").Namespace(
        node_path="/root/Player", props=["position"]
    )

    mock_client = AsyncMock()
    # client.request 直接返回服务端 result dict（{"value": ..., "type": ...}）
    mock_client.request = AsyncMock(
        return_value={"value": [1.0, 2.0], "type": "Vector2"}
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "ok": True,
        "result": {"value": [1.0, 2.0], "type": "Vector2"},
    }
    # 必须走 client.request，不走 client.get_property（裸值便捷层）
    mock_client.request.assert_awaited_once_with(
        "get_property", {"path": "/root/Player", "property": "position"}
    )


def test_get_multi_prop_envelope_passthrough(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """多属性 get 必须发 get_properties，信封 result = {"values": {...}}。

    这是 issue #100 的原子读契约。
    """
    import json as _json

    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["get"]
    ns = __import__("argparse").Namespace(
        node_path="/root/Enemy", props=["position", "health"]
    )

    mock_result = {
        "values": {
            "position": {"value": [3.0, 4.0], "type": "Vector2"},
            "health": {"value": 80, "type": "int"},
        }
    }
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_result)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": True, "result": mock_result}
    mock_client.request.assert_awaited_once_with(
        "get_properties",
        {"path": "/root/Enemy", "properties": ["position", "health"]},
    )


def test_get_single_prop_text_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--text 下单属性打印裸 value（保持旧 text 行为，只把 value 取出渲染）。"""
    from godot_cli_control.cli import OUTPUT_TEXT, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["get"]
    ns = __import__("argparse").Namespace(
        node_path="/root/Player", props=["position"]
    )

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        return_value={"value": [1.5, -2.0], "type": "Vector2"}
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_TEXT))

    assert rc == 0
    out = capsys.readouterr().out.strip()
    # value 是 list → JSON 序列化
    assert out == "[1.5, -2.0]"


def test_get_multi_prop_text_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--text 下多属性每行输出 ``prop = value``。"""
    from godot_cli_control.cli import OUTPUT_TEXT, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["get"]
    ns = __import__("argparse").Namespace(
        node_path="/root/Enemy", props=["position", "health"]
    )

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        return_value={
            "values": {
                "position": {"value": [3.0, 4.0], "type": "Vector2"},
                "health": {"value": 80, "type": "int"},
            }
        }
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_TEXT))

    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert "position = [3.0, 4.0]" in out
    assert "health = 80" in out


def test_get_parses_multiple_props_from_argv() -> None:
    """argparse 层面 props nargs='+' 能解析 2+ 属性。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["get", "/root/Node", "position", "visible"])
    assert ns.cmd == "get"
    assert ns.node_path == "/root/Node"
    assert ns.props == ["position", "visible"]


def test_get_parses_single_prop_from_argv() -> None:
    """argparse 层面 props nargs='+' 能解析单个属性。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["get", "/root/Node", "health"])
    assert ns.props == ["health"]


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
    proj = tmp_path / "p"
    proj.mkdir()
    registry.register(
        proj, pid=os.getpid(), port=12345, godot_bin="x", log_path="y"
    )

    from godot_cli_control.cli import cmd_daemon_ls, OUTPUT_JSON
    import argparse

    rc = cmd_daemon_ls(argparse.Namespace(output_format=OUTPUT_JSON))
    out = capsys.readouterr().out
    assert rc == 0
    # 直接读字段而不是子串匹配 —— Windows 路径含反斜杠，JSON 输出会转义成 \\
    import json as _json
    payload = _json.loads(out)
    daemons = payload["result"]["daemons"]
    assert any(d["port"] == 12345 for d in daemons)
    assert any(d["project_root"] == str(proj.resolve()) for d in daemons)


def test_daemon_ls_subcommand_parses() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "ls"])
    assert ns.cmd == "daemon"
    assert ns.action == "ls"


def test_daemon_stop_all_invokes_terminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--all 应对注册表里每条记录调 Daemon(...).stop()。"""
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")

    proj1 = tmp_path / "a"
    proj1.mkdir()
    proj2 = tmp_path / "b"
    proj2.mkdir()
    import os
    registry.register(proj1, pid=os.getpid(), port=1, godot_bin="x", log_path="y")
    registry.register(proj2, pid=os.getpid(), port=2, godot_bin="x", log_path="y")

    stopped: list[Path] = []
    def fake_stop(self) -> int:
        stopped.append(self.project_root)
        return 0
    import godot_cli_control.daemon as daemon_mod
    monkeypatch.setattr(daemon_mod.Daemon, "stop", fake_stop)

    from godot_cli_control.cli import cmd_daemon_stop, OUTPUT_JSON
    import argparse
    ns = argparse.Namespace(all=True, project=None, output_format=OUTPUT_JSON)
    rc = cmd_daemon_stop(ns)
    assert rc == 0
    assert {p.resolve() for p in stopped} == {proj1.resolve(), proj2.resolve()}


def test_daemon_stop_all_returns_partial_when_one_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--all 中至少一条 stop 抛 DaemonError → rc=EXIT_PARTIAL（与 ffmpeg rc=2 区分）。"""
    from godot_cli_control import registry
    from godot_cli_control.daemon import DaemonError
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")

    proj1 = tmp_path / "ok"
    proj1.mkdir()
    proj2 = tmp_path / "bad"
    proj2.mkdir()
    import os
    registry.register(proj1, pid=os.getpid(), port=1, godot_bin="x", log_path="y")
    registry.register(proj2, pid=os.getpid(), port=2, godot_bin="x", log_path="y")

    def fake_stop(self) -> int:
        if "bad" in str(self.project_root):
            raise DaemonError("simulated failure")
        return 0
    import godot_cli_control.daemon as daemon_mod
    monkeypatch.setattr(daemon_mod.Daemon, "stop", fake_stop)

    from godot_cli_control.cli import cmd_daemon_stop, OUTPUT_JSON, EXIT_PARTIAL
    import argparse
    ns = argparse.Namespace(all=True, project=None, output_format=OUTPUT_JSON)
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_PARTIAL
    assert EXIT_PARTIAL != 2, "EXIT_PARTIAL 必须与 EXIT_INFRA_ERROR 区分，避免与单项目 ffmpeg rc=2 撞码"


def test_daemon_stop_all_ffmpeg_rc2_does_not_promote_to_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """单条 stop 返回 2（ffmpeg 转码失败但 daemon 已停）不算 --all 失败 —— rc 应为 0。"""
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    proj = tmp_path / "p"
    proj.mkdir()
    import os
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x", log_path="y")

    import godot_cli_control.daemon as daemon_mod
    monkeypatch.setattr(daemon_mod.Daemon, "stop", lambda self: 2)

    from godot_cli_control.cli import cmd_daemon_stop, OUTPUT_JSON
    import argparse
    ns = argparse.Namespace(all=True, project=None, output_format=OUTPUT_JSON)
    rc = cmd_daemon_stop(ns)
    assert rc == 0


def test_daemon_stop_project_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--project <path> 应对该项目调 Daemon(...).stop()，与 cwd 无关。"""
    target = tmp_path / "p"
    target.mkdir()
    seen: list[Path] = []
    import godot_cli_control.daemon as daemon_mod
    monkeypatch.setattr(daemon_mod.Daemon, "stop",
                        lambda self: (seen.append(self.project_root), 0)[1])

    from godot_cli_control.cli import cmd_daemon_stop, OUTPUT_JSON
    import argparse
    ns = argparse.Namespace(all=False, project=target, output_format=OUTPUT_JSON)
    rc = cmd_daemon_stop(ns)
    assert rc == 0
    assert seen == [target.resolve()]


def test_daemon_stop_default_uses_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """无 --all 也无 --project 时，行为与今天一致：使用 cwd 的 Daemon。"""
    monkeypatch.chdir(tmp_path)
    import godot_cli_control.daemon as daemon_mod
    seen: list[Path] = []
    monkeypatch.setattr(daemon_mod.Daemon, "stop",
                        lambda self: (seen.append(self.project_root), 0)[1])

    from godot_cli_control.cli import cmd_daemon_stop, OUTPUT_JSON
    import argparse
    ns = argparse.Namespace(all=False, project=None, output_format=OUTPUT_JSON)
    rc = cmd_daemon_stop(ns)
    assert rc == 0
    assert seen == [tmp_path.resolve()]


def test_daemon_stop_subcommand_parses_all_and_project() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "stop", "--all"])
    assert ns.all is True

    ns = build_parser().parse_args(["daemon", "stop", "--project", "/tmp/x"])
    # argparse 用 type=Path，Windows 上 str(WindowsPath("/tmp/x")) == "\\tmp\\x"，
    # 所以与 Path("/tmp/x") 比较保证跨平台。
    assert ns.project == Path("/tmp/x")

    # mutually exclusive — argparse should refuse both
    import pytest as _pt
    with _pt.raises(SystemExit):
        build_parser().parse_args(["daemon", "stop", "--all", "--project", "/tmp/x"])


def test_daemon_start_idle_timeout_flag_parses() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "start", "--idle-timeout", "30m"])
    assert ns.idle_timeout == "30m"


def test_daemon_start_idle_timeout_default_is_zero() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "start"])
    assert ns.idle_timeout == "0"


def test_tree_accepts_max_nodes_flag() -> None:
    from godot_cli_control.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["tree", "3", "--max-nodes", "50"])
    assert ns.depth == "3"
    assert ns.max_nodes == 50


def test_tree_max_nodes_default_is_200() -> None:
    from godot_cli_control.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["tree"])
    assert ns.max_nodes == 200


def test_set_text_value_disables_json_parse() -> None:
    """--text-value 让 set 把 value 当字面字符串，不走 JSON-or-string fallback。"""
    from godot_cli_control.cli import build_parser, _resolve_value_for_set

    parser = build_parser()
    ns = parser.parse_args(["set", "/root/X", "flag", "true", "--text-value"])
    assert ns.text_value is True
    assert _resolve_value_for_set(ns) == "true"


def test_set_default_still_json_parses() -> None:
    from godot_cli_control.cli import build_parser, _resolve_value_for_set

    parser = build_parser()
    ns = parser.parse_args(["set", "/root/X", "flag", "true"])
    assert ns.text_value is False
    assert _resolve_value_for_set(ns) is True


# ── _fmt_get_result_text 防御 + 文本渲染补测（review 遗留）──


def test_fmt_get_result_text_nested_compound_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """嵌套复合 value（Array 内含 dict）在 --text 模式下渲染成 JSON 串，不崩。

    info：client.request("get_property") 返回 {"value": [[1.0, 2.0], {"p": [3.0, 4.0, 5.0]}]}；
    这种结构在普通 Python 属性里少见，但 agent 可能读到 custom Variant / Rect2 等。
    """
    from godot_cli_control.cli import OUTPUT_TEXT, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    import json as _json

    spec = RPC_BY_NAME["get"]
    ns = __import__("argparse").Namespace(
        node_path="/root/Node", props=["weird"]
    )

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        return_value={"value": [[1.0, 2.0], {"p": [3.0, 4.0, 5.0]}]}
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_TEXT))

    assert rc == 0
    out = capsys.readouterr().out.strip()
    # 必须是合法 JSON 串（value 是复合类型，走 json.dumps 分支）
    parsed = _json.loads(out)
    assert parsed == [[1.0, 2.0], {"p": [3.0, 4.0, 5.0]}]


def test_fmt_get_result_text_multi_prop_non_dict_entry_does_not_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """多属性 result 中 entry 不是 dict（防御性非 dict 分支）不崩，打印为 str(entry)。

    正常路径下服务端总是返回 dict，但防御性代码必须能接受非 dict 的 entry
    （如 result.values["weird"] = 42 这种裸值，对齐 client 侧 guard）。
    """
    from godot_cli_control.cli import _fmt_get_result_text

    # 多属性 result，values 里有一个非 dict 的裸值 42
    r = {"values": {"weird": 42}}
    out = _fmt_get_result_text(r)
    # 不抛；渲染出来包含 key 和值
    assert "weird" in out
    assert "42" in out


def test_call_text_value_disables_arg_parse() -> None:
    from godot_cli_control.cli import build_parser, _resolve_args_for_call

    parser = build_parser()
    ns = parser.parse_args(
        ["call", "/root/X", "set_label", "true", "42", "--text-value"]
    )
    assert _resolve_args_for_call(ns) == ["true", "42"]


class TestDaemonHeadlessAutodetect:
    def test_default_headless_when_stdout_not_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        ns = type("NS", (), {"headless": False, "gui": False})()
        assert _resolve_headless(ns) is True

    def test_default_gui_when_stdout_is_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        ns = type("NS", (), {"headless": False, "gui": False})()
        assert _resolve_headless(ns) is False

    def test_explicit_headless_wins_over_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        ns = type("NS", (), {"headless": True, "gui": False})()
        assert _resolve_headless(ns) is True

    def test_explicit_gui_wins_over_pipe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        ns = type("NS", (), {"headless": False, "gui": True})()
        assert _resolve_headless(ns) is False

    def test_gui_and_headless_mutually_exclusive(self) -> None:
        from godot_cli_control.cli import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["daemon", "start", "--headless", "--gui"])

    def test_force_gui_hint_flips_pipe_to_gui(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """脚本含 screenshot 时 cmd_run 传 force_gui_hint=True，
        非 TTY 默认从 headless 翻成 GUI（issue #65）。"""
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        ns = type("NS", (), {"headless": False, "gui": False})()
        assert _resolve_headless(ns, force_gui_hint=True) is False

    def test_explicit_headless_still_wins_over_force_gui_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户显式 --headless 永远赢 —— 即使脚本含 screenshot，
        用户也可能就是想跑 headless（CI 不需要截图的子集等）。"""
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        ns = type("NS", (), {"headless": True, "gui": False})()
        assert _resolve_headless(ns, force_gui_hint=True) is True

    def test_record_flips_pipe_to_gui(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--record 没显式 --headless 时强制开窗 —— Movie Maker 需真实渲染器，
        headless dummy renderer 拿不到 viewport texture 会 SIGSEGV
        （CultivationWorld #180，同 screenshot/#65）。非 TTY 默认 headless
        的 subagent/pipe/CI 场景下，--record 也应自动翻成 GUI。"""
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        ns = type("NS", (), {"headless": False, "gui": False, "record": True})()
        assert _resolve_headless(ns) is False

    def test_explicit_headless_wins_over_record_flip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式 --headless + --record 仍返回 True —— 不静默改写用户意图，
        而是把矛盾交给 daemon.start 的 preflight 拒绝（给明确用法错）。"""
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        ns = type("NS", (), {"headless": True, "gui": False, "record": True})()
        assert _resolve_headless(ns) is True


class TestScriptLikelyUsesScreenshot:
    def test_detects_method_call(self, tmp_path: Path) -> None:
        from godot_cli_control.cli import _script_likely_uses_screenshot

        script = tmp_path / "s.py"
        script.write_text(
            "def run(bridge):\n    bridge.screenshot('/tmp/x.png')\n",
            encoding="utf-8",
        )
        assert _script_likely_uses_screenshot(script) is True

    def test_no_match_when_script_clean(self, tmp_path: Path) -> None:
        from godot_cli_control.cli import _script_likely_uses_screenshot

        script = tmp_path / "s.py"
        script.write_text("def run(bridge):\n    bridge.click('/root/A')\n", "utf-8")
        assert _script_likely_uses_screenshot(script) is False

    def test_missing_file_returns_false_not_raises(self, tmp_path: Path) -> None:
        """读不到不抛 —— 让 cmd_run 走原 isatty 默认，"脚本不存在"
        由后续 script_path.exists() 检查统一报错。"""
        from godot_cli_control.cli import _script_likely_uses_screenshot

        assert _script_likely_uses_screenshot(tmp_path / "missing.py") is False


class TestCmdRunGuiAutoDetect:
    """issue #65：cli run 静态检测脚本含 screenshot 时，非 TTY 也强制开窗。"""

    @staticmethod
    def _mock_run_pipeline(
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, Any]:
        """拦截 daemon.start / is_running / current_port / stop +
        _exec_user_script，让 cmd_run 跑完整路径但不真起进程。
        返回 dict 记录 daemon.start kwargs，测试断言用。"""
        import godot_cli_control.cli as cli_mod
        import godot_cli_control.daemon as daemon_mod

        captured: dict[str, Any] = {}

        def _is_running(self: Any) -> bool:
            return False

        def _start(self: Any, **kw: Any) -> None:
            captured["start_kwargs"] = kw

        def _current_port(self: Any) -> int:
            return 12345

        def _stop(self: Any) -> int:
            return 0

        monkeypatch.setattr(daemon_mod.Daemon, "is_running", _is_running)
        monkeypatch.setattr(daemon_mod.Daemon, "start", _start)
        monkeypatch.setattr(daemon_mod.Daemon, "current_port", _current_port)
        monkeypatch.setattr(daemon_mod.Daemon, "stop", _stop)
        monkeypatch.setattr(
            cli_mod,
            "_exec_user_script",
            lambda *a, **kw: 0,
        )
        # 非 TTY ── 模拟 subagent / pipe / CI
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        return captured

    def test_script_with_screenshot_forces_gui_under_pipe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import argparse

        from godot_cli_control.cli import OUTPUT_JSON, cmd_run

        captured = self._mock_run_pipeline(monkeypatch)
        script = tmp_path / "s.py"
        script.write_text(
            "def run(bridge):\n    bridge.screenshot('/tmp/x.png')\n",
            encoding="utf-8",
        )
        ns = argparse.Namespace(
            script=str(script),
            record=False,
            movie_path=None,
            headless=False,
            gui=False,
            no_gui_auto=False,
            fps=30,
            port=0,
            idle_timeout="0",
            output_format=OUTPUT_JSON,
        )
        rc = cmd_run(ns)
        assert rc == 0
        # 非 TTY 默认 headless=True，但脚本含 screenshot → 翻转到 False
        assert captured["start_kwargs"]["headless"] is False

    def test_no_gui_auto_disables_detection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import argparse

        from godot_cli_control.cli import OUTPUT_JSON, cmd_run

        captured = self._mock_run_pipeline(monkeypatch)
        script = tmp_path / "s.py"
        script.write_text(
            "def run(bridge):\n    bridge.screenshot('/tmp/x.png')\n",
            encoding="utf-8",
        )
        ns = argparse.Namespace(
            script=str(script),
            record=False,
            movie_path=None,
            headless=False,
            gui=False,
            no_gui_auto=True,  # ← opt-out
            fps=30,
            port=0,
            idle_timeout="0",
            output_format=OUTPUT_JSON,
        )
        rc = cmd_run(ns)
        assert rc == 0
        # opt-out → 回到 isatty 默认（非 TTY = headless）
        assert captured["start_kwargs"]["headless"] is True

    def test_script_without_screenshot_keeps_headless_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import argparse

        from godot_cli_control.cli import OUTPUT_JSON, cmd_run

        captured = self._mock_run_pipeline(monkeypatch)
        script = tmp_path / "s.py"
        script.write_text(
            "def run(bridge):\n    bridge.click('/root/A')\n",
            encoding="utf-8",
        )
        ns = argparse.Namespace(
            script=str(script),
            record=False,
            movie_path=None,
            headless=False,
            gui=False,
            no_gui_auto=False,
            fps=30,
            port=0,
            idle_timeout="0",
            output_format=OUTPUT_JSON,
        )
        rc = cmd_run(ns)
        assert rc == 0
        # 脚本无 screenshot → 不触发翻转，按 isatty=False 走 headless
        assert captured["start_kwargs"]["headless"] is True


def test_run_rpc_tree_truncated_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """e2e：``cmd_tree`` → ``_run_rpc`` 必须把服务端 truncated 信号
    透传到 stdout JSON envelope 里，agent 才能据此分子树。"""
    import json as _json
    from unittest.mock import AsyncMock, patch

    from godot_cli_control.cli import (
        OUTPUT_JSON,
        RPC_BY_NAME,
        _run_rpc,
    )

    spec = RPC_BY_NAME["tree"]
    ns = __import__("argparse").Namespace(depth="3", max_nodes=200)

    truncated_payload = {
        "tree": {"name": "root", "type": "Node", "path": "/root"},
        "truncated": True,
        "total_nodes": 6000,
    }
    mock_client = AsyncMock()
    mock_client.get_scene_tree = AsyncMock(return_value=truncated_payload)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "godot_cli_control.cli.GameClient", return_value=mock_client
    ):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 0
    envelope = _json.loads(capsys.readouterr().out.strip())
    assert envelope["ok"] is True
    assert envelope["result"]["truncated"] is True
    assert envelope["result"]["total_nodes"] == 6000
    assert envelope["result"]["tree"]["name"] == "root"
    # cmd_tree 应当把 ns.max_nodes 透传给 client.get_scene_tree
    mock_client.get_scene_tree.assert_awaited_once_with(depth=3, max_nodes=200)


# ── Task A1: argparse 层用法错统一 -1003 + exit 64 ──────────────────────────


def test_argparse_missing_required_positional_exits_64_with_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`get` 缺位置参数 → SystemExit code 64 + stdout 单行 {"ok": false, "error": {"code": -1003, ...}} + usage 在 stderr。"""
    import json as _json

    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["get"])
    assert exc_info.value.code == 64

    captured = capsys.readouterr()
    # stdout 必须是单行 JSON 信封
    out_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(out_lines) == 1, f"stdout 不是单行: {captured.out!r}"
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    # usage 走 stderr
    assert "usage" in captured.err.lower() or "get" in captured.err


def test_argparse_invalid_choice_exits_64_with_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`wait-prop /n p 1 --op bogus`(非法 choices) → SystemExit code 64 + envelope。"""
    import json as _json

    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["wait-prop", "/root/Node", "scale", "1", "--op", "bogus"])
    assert exc_info.value.code == 64

    captured = capsys.readouterr()
    out_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(out_lines) == 1, f"stdout 不是单行: {captured.out!r}"
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


def test_argparse_unknown_subcommand_exits_64_with_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未知子命令 → SystemExit code 64 + envelope。"""
    import json as _json

    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["nonexistent-command"])
    assert exc_info.value.code == 64

    captured = capsys.readouterr()
    out_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(out_lines) == 1, f"stdout 不是单行: {captured.out!r}"
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


def test_argparse_rpc_subcommand_missing_arg_exits_64_with_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`set` 只给一个参数(缺 value) → SystemExit code 64 + envelope。三层 subparser 继承。"""
    import json as _json

    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["set", "/root/Node"])
    assert exc_info.value.code == 64

    captured = capsys.readouterr()
    out_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(out_lines) == 1, f"stdout 不是单行: {captured.out!r}"
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


def test_argparse_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """`--help` → SystemExit 0（正常帮助，不是错误）。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--help"])
    assert exc_info.value.code == 0


def test_argparse_daemon_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """`daemon --help` → SystemExit 0。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["daemon", "--help"])
    assert exc_info.value.code == 0


def test_argparse_text_mode_error_no_json_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--text` 模式用法错：stdout 无 JSON，人类可读错误走 stderr，仍 exit 64。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--text", "get"])
    assert exc_info.value.code == 64

    captured = capsys.readouterr()
    # stdout 不应有 JSON
    assert not captured.out.strip(), f"--text 模式 stdout 不应有 JSON: {captured.out!r}"
    # stderr 应有错误信息
    assert captured.err.strip(), "stderr 应有错误信息"


def test_argparse_literal_text_after_dashdash_still_emits_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`combo -- --text extra`：`--text` 是 `--` 之后的位置参数值，不是旗标。

    argparse 触发 unrecognized arguments 错误，此时 peek-parse 应把 `--text`
    视为位置参数（`--` 终止符之后），故 _is_text_mode=False，
    stdout 必须有单行 -1003 JSON 信封 + exit 64。
    """
    import json as _json

    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["combo", "--", "--text", "extra"])
    assert exc_info.value.code == 64

    captured = capsys.readouterr()
    out_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(out_lines) == 1, f"stdout 应为单行 JSON 信封（字面 --text 不是旗标）: {captured.out!r}"
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


def test_argparse_peek_parse_failure_no_extra_usage_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`get --text=x`（store_const 不接受显式值）：peek parser 自身解析失败时
    不得往 stderr 追加它自己的 usage/error 行，stderr 只保留真 parser 的
    单段 usage；stdout 的 -1003 信封契约不变（#118）。
    """
    import json as _json

    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["get", "--text=x", "/root/Foo", "name"])
    assert exc_info.value.code == 64

    captured = capsys.readouterr()
    out_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(out_lines) == 1, f"stdout 应为单行 JSON 信封: {captured.out!r}"
    payload = _json.loads(out_lines[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    # stderr 只允许真 parser 打的一段 usage；peek parser 的不该出现
    assert captured.err.count("usage:") == 1, (
        f"stderr 出现 peek parser 的冗余 usage/error 行: {captured.err!r}"
    )


def test_argparse_text_mode_real_bypass_no_json_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`get --text`（缺参）：真 --text 旗路 → stdout 空 + stderr 有错 + exit 64。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--text", "get"])
    assert exc_info.value.code == 64

    captured = capsys.readouterr()
    assert not captured.out.strip(), f"真 --text 旁路 stdout 不应有 JSON: {captured.out!r}"
    assert captured.err.strip(), "stderr 应有错误信息"


# ── Task A2: run/daemon 前置失败拆分 ─────────────────────────────────────────


def test_cmd_daemon_start_daemonerror_exits_infra_with_1006(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_daemon_start DaemonError → -1006 (PRECONDITION) + EXIT_INFRA_ERROR(2)（#92）。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_INFRA_ERROR, OUTPUT_JSON, cmd_daemon_start

    (tmp_path / "project.godot").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def _fake_start(self: Any, **__: Any) -> None:
        raise daemon_mod.DaemonError("cannot find godot binary")

    monkeypatch.setattr(daemon_mod.Daemon, "start", _fake_start)
    monkeypatch.setattr("godot_cli_control.daemon.find_godot_binary", lambda: None)

    ns = __import__("argparse").Namespace(
        record=False,
        movie_path=None,
        headless=True,
        gui=False,
        fps=30,
        port=0,
        idle_timeout="0",
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_start(ns)
    out = capsys.readouterr().out.strip()
    assert rc == EXIT_INFRA_ERROR
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1006
    assert "cannot find godot binary" in payload["error"]["message"]


def test_cmd_daemon_stop_single_project_daemonerror_exits_infra_with_1006(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_daemon_stop 单项目 DaemonError → -1006 + EXIT_INFRA_ERROR(2)（#92）。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_INFRA_ERROR, OUTPUT_JSON, cmd_daemon_stop

    def _fake_stop(self: Any) -> int:
        raise daemon_mod.DaemonError("pid file not found")

    monkeypatch.setattr(daemon_mod.Daemon, "stop", _fake_stop)

    ns = __import__("argparse").Namespace(
        all=False,
        project=None,
        output_format=OUTPUT_JSON,
    )
    monkeypatch.chdir(tmp_path)
    rc = cmd_daemon_stop(ns)
    out = capsys.readouterr().out.strip()
    assert rc == EXIT_INFRA_ERROR
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1006
    assert "pid file not found" in payload["error"]["message"]
