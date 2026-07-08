"""CLI 单元测试 —— 覆盖 ``_exec_user_script`` 的脚本加载边界。

不实际启动 Godot：用 monkeypatch 替换 ``GameBridge``，只验证 importer 行为。
"""

from __future__ import annotations

import asyncio
import json
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


def test_init_subcommand_accepts_keep_addon_flag() -> None:
    """--keep-addon：已存在 addon 时跳过插件复制的逃生口。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--keep-addon"])
    assert ns.keep_addon is True
    assert ns.force is False


def test_init_subcommand_force_still_accepted_as_noop() -> None:
    """--force 降为兼容 no-op：必须仍被 argparse 接受。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--force"])
    assert ns.force is True
    assert ns.keep_addon is False


def test_init_subcommand_rejects_force_with_keep_addon() -> None:
    """--force 与 --keep-addon 互斥（mutually_exclusive_group）。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["init", "--force", "--keep-addon"])


@pytest.mark.parametrize(
    ("keep_addon", "expected_clobber"),
    [(True, False), (False, True)],
    ids=["keep-addon", "default-clobber"],
)
def test_cmd_init_wires_keep_addon_to_clobber_addon(
    keep_addon: bool,
    expected_clobber: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_init 的接线两个方向都钉死：--keep-addon → clobber_addon=False，
    默认（keep_addon=False）→ clobber_addon=True（本次行为翻转的主线）。"""
    import argparse

    from godot_cli_control.cli import OUTPUT_JSON, cmd_init

    captured: dict = {}

    def fake_run_init(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("godot_cli_control.init_cmd.run_init", fake_run_init)
    ns = argparse.Namespace(
        path=None,
        force=False,
        keep_addon=keep_addon,
        no_skills=False,
        skills_only=False,
        skills_no_clobber=False,
        no_gitignore=False,
        output_format=OUTPUT_JSON,
    )
    assert cmd_init(ns) == 0
    capsys.readouterr()
    assert captured["clobber_addon"] is expected_clobber
    assert "force" not in captured


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
        always_on_top=True,
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
        keep_addon=False,
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
    # Task 5 起 result 含 instance 字段（默认 "default"）
    assert payload["ok"] is True
    result = payload["result"]
    assert result["state"] == "running"
    assert result["pid"] == 4242
    assert result["port"] == 9877
    assert "instance" in result  # 新增字段，默认 "default"


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
    # Task 1 后状态文件移入 instances/default/（spec 2026-06-07）
    control_dir = tmp_path / ".cli_control" / "instances" / "default"
    control_dir.mkdir(parents=True)
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
    # Task 1 后状态文件移入 instances/default/（spec 2026-06-07）
    control_dir = tmp_path / ".cli_control" / "instances" / "default"
    control_dir.mkdir(parents=True)
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
    # Task 1 后状态文件移入 instances/default/（spec 2026-06-07）
    control_dir = tmp_path / ".cli_control" / "instances" / "default"
    control_dir.mkdir(parents=True)
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
        always_on_top=True,
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
        always_on_top=True,
        output_format=OUTPUT_TEXT,
    )
    assert cmd_daemon_start(ns) == 0
    assert capsys.readouterr().out == ""


def _restart_ns(**overrides: Any) -> Any:
    """cmd_daemon_restart 的最小 Namespace（name 显式给定以跳过实例扫描）。"""
    base: dict[str, Any] = dict(
        record=False,
        movie_path=None,
        headless=True,
        fps=30,
        port=9877,
        always_on_top=True,
        name="default",
        output_format="json",
    )
    base.update(overrides)
    return __import__("argparse").Namespace(**base)


def test_daemon_restart_stops_then_starts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """restart 编排：stop 先于 start；信封带 restarted/was_running/stop_rc/pid。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import cmd_daemon_restart

    calls: list[str] = []
    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: True)
    monkeypatch.setattr(
        daemon_mod.Daemon, "stop", lambda self: (calls.append("stop"), 0)[1]
    )
    monkeypatch.setattr(
        daemon_mod.Daemon, "start", lambda self, **kw: calls.append("start")
    )
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9901)
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: 4321)

    rc = cmd_daemon_restart(_restart_ns())
    assert rc == 0
    assert calls == ["stop", "start"]
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["result"]["restarted"] is True
    assert payload["result"]["was_running"] is True
    assert payload["result"]["stop_rc"] == 0
    assert payload["result"]["pid"] == 4321


def test_daemon_restart_tolerates_not_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """未运行时 restart 不报错：stop 容忍（返回 0）后照常 start，was_running=false。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import cmd_daemon_restart

    started: list[bool] = []
    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: False)
    monkeypatch.setattr(daemon_mod.Daemon, "stop", lambda self: 0)
    monkeypatch.setattr(
        daemon_mod.Daemon, "start", lambda self, **kw: started.append(True)
    )
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9901)
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: None)

    rc = cmd_daemon_restart(_restart_ns())
    assert rc == 0
    assert started == [True]
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["result"]["was_running"] is False


def test_daemon_restart_stop_hard_failure_aborts_without_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """stop 硬失败（进程在但停不掉）必须中止：不带着旧进程再起新的。-1006/exit 2。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import cmd_daemon_restart

    def _boom(self: Any) -> int:
        raise daemon_mod.DaemonError("PID 99 进程名不像 Godot")

    started: list[bool] = []
    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: True)
    monkeypatch.setattr(daemon_mod.Daemon, "stop", _boom)
    monkeypatch.setattr(
        daemon_mod.Daemon, "start", lambda self, **kw: started.append(True)
    )

    rc = cmd_daemon_restart(_restart_ns())
    assert rc == 2
    assert started == []
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1006


def test_daemon_restart_transcode_soft_fail_restarts_with_rc4(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """stop 仅转码失败（rc=4，AVI 保留）不阻断重启：start 照常，最终 exit 4 透出。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import cmd_daemon_restart

    started: list[bool] = []
    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: True)
    monkeypatch.setattr(daemon_mod.Daemon, "stop", lambda self: 4)
    monkeypatch.setattr(
        daemon_mod.Daemon, "start", lambda self, **kw: started.append(True)
    )
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9901)
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: 4321)

    rc = cmd_daemon_restart(_restart_ns())
    assert rc == 4
    assert started == [True]
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["result"]["restarted"] is True
    assert payload["result"]["stop_rc"] == 4


def test_daemon_restart_parses_start_flags() -> None:
    """restart 接受与 start 同一套 flags（共享注册函数，防两处漂移）。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(
        ["daemon", "restart", "--headless", "--name", "server", "--time-scale", "5"]
    )
    assert ns.cmd == "daemon"
    assert ns.action == "restart"
    assert ns.name == "server"
    assert ns.time_scale == 5.0


def test_command_groups_cover_every_spec() -> None:
    """顶层 help 分组表必须恰好覆盖全部命令：RPC_SPECS 全集 + daemon/run/init，
    无重复无遗漏——新增子命令忘了归组、或删了命令忘了清组，这里红。"""
    from godot_cli_control.cli import RPC_SPECS, _COMMAND_GROUPS

    grouped = [n for _, names in _COMMAND_GROUPS for n in names]
    assert len(grouped) == len(set(grouped)), "分组表存在重复命令"
    expected = {s.name for s in RPC_SPECS} | {"daemon", "run", "init"}
    assert set(grouped) == expected


def test_top_help_shows_grouped_overview() -> None:
    """顶层 --help 输出分组总览：每个命令以缩进行出现（替代旧的平铺全描述列表）。"""
    from godot_cli_control.cli import RPC_SPECS, build_parser

    help_text = build_parser().format_help()
    assert "命令总览" in help_text
    for spec in RPC_SPECS:
        assert f"\n    {spec.name} " in help_text, f"分组总览缺 {spec.name}"
    # 生成的总览区（RawDescription 不回卷）：摘要不残留未闭合的全角括号
    overview = help_text.split("命令总览", 1)[1].split("输出契约", 1)[0]
    for line in overview.splitlines():
        assert line.count("（") <= line.count("）"), f"摘要残留未闭合括号: {line}"


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
        always_on_top=True,
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
    assert "uid://" in payload["error"]["message"]


def test_scene_specs_registered() -> None:
    from godot_cli_control.cli import RPC_BY_NAME

    for name in ("scene-reload", "scene-change"):
        spec = RPC_BY_NAME[name]
        assert spec.preflight is not None


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

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：scene-reload 非法 timeout 时不应连 daemon")

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "scene-reload", "--timeout", bad_timeout])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "timeout" in payload["error"]["message"]


@pytest.mark.parametrize("bad_timeout", ["-1", "0"])
def test_scene_change_preflight_rejects_bad_timeout(
    bad_timeout: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """scene-change --timeout <=0 必须在连 daemon 之前报 EXIT_USAGE。"""
    import json as _json

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：scene-change 非法 timeout 时不应连 daemon")

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(
        sys, "argv", ["godot-cli-control", "scene-change", "res://ok.tscn", "--timeout", bad_timeout]
    )

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "timeout" in payload["error"]["message"]


# ── Time 命令组测试（#102）──


def test_time_specs_registered() -> None:
    """time-scale / pause / unpause / step-frames 四个 spec 在 RPC_BY_NAME；
    time-scale 和 step-frames 带 preflight。"""
    from godot_cli_control.cli import RPC_BY_NAME

    for name in ("time-scale", "pause", "unpause", "step-frames"):
        assert name in RPC_BY_NAME, f"{name} 未注册"
    assert RPC_BY_NAME["time-scale"].preflight is not None, "time-scale 需要 preflight"
    assert RPC_BY_NAME["step-frames"].preflight is not None, "step-frames 需要 preflight"
    # pause / unpause 无参，不需要 preflight
    assert RPC_BY_NAME["pause"].preflight is None
    assert RPC_BY_NAME["unpause"].preflight is None


def test_time_text_formatters() -> None:
    """time-scale / pause / unpause / step-frames 的 text_formatter 输出符合预期。"""
    from godot_cli_control.cli import RPC_BY_NAME

    assert RPC_BY_NAME["time-scale"].text_formatter({"time_scale": 2.5}) == "time_scale = 2.5"
    assert RPC_BY_NAME["pause"].text_formatter({"paused": True}) == "paused: True"
    assert RPC_BY_NAME["unpause"].text_formatter({"paused": False}) == "paused: False"
    assert RPC_BY_NAME["step-frames"].text_formatter({"stepped": 5, "paused": True}) == \
        "stepped 5 frames (still paused)"


@pytest.mark.parametrize("bad_value", ["0", "-1", "101", "abc"])
def test_time_scale_preflight_rejects_bad_value(
    bad_value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """time-scale 非法 value 必须在连 daemon 之前报 EXIT_USAGE（-1003）。"""
    import json as _json

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：time-scale 非法 value 时不应连 daemon")

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "time-scale", bad_value])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "value" in payload["error"]["message"]


@pytest.mark.parametrize("bad_frames", ["0", "3601", "abc"])
def test_step_frames_preflight_rejects_bad_frames(
    bad_frames: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """step-frames 非法 frames 必须在连 daemon 之前报 EXIT_USAGE（-1003）。"""
    import json as _json

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：step-frames 非法 frames 时不应连 daemon")

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "step-frames", bad_frames])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "frames" in payload["error"]["message"]


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
    ns = __import__("argparse").Namespace(output_path=str(bad_path), node=None)

    mock_client = AsyncMock()
    # cmd_screenshot 走 screenshot_raw（issue #101 起，为透出 region），
    # 服务端响应是 {"image": <base64>}，写盘前解码。
    mock_client.screenshot_raw = AsyncMock(
        return_value={
            "image": __import__("base64").b64encode(b"fake png bytes").decode()
        }
    )
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


def test_screenshot_server_side_write_reports_daemon_result(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """screenshot 落盘走 daemon 直写（issue #149）：CLI 建好父目录、把绝对
    路径递给 RPC，信封字节数取自 daemon 回报，base64 不再过 WS。"""
    import json as _json

    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["screenshot"]
    out = tmp_path / "sub" / "shot.png"
    ns = __import__("argparse").Namespace(output_path=str(out), node=None)

    mock_client = AsyncMock()
    mock_client.screenshot_raw = AsyncMock(
        return_value={"path": str(out.resolve()), "bytes": 7}
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["result"] == {"path": str(out), "bytes": 7}
    # 路径必须绝对化（daemon 的 CWD 是项目目录，相对路径会写错地方）
    mock_client.screenshot_raw.assert_awaited_once_with(
        None, path=str(out.resolve())
    )
    # 父目录由 CLI 创建（同机），daemon 端只管写文件
    assert out.parent.is_dir()


def test_screenshot_server_side_write_node_region_passthrough(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--node 裁剪在 daemon 直写协议下 region/node 信封字段不丢（issue #149）。"""
    import json as _json

    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["screenshot"]
    out = tmp_path / "crop.png"
    ns = __import__("argparse").Namespace(output_path=str(out), node="/root/S")

    mock_client = AsyncMock()
    mock_client.screenshot_raw = AsyncMock(
        return_value={"path": str(out.resolve()), "bytes": 3, "region": [1, 2, 3, 4]}
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["result"] == {
        "path": str(out),
        "bytes": 3,
        "node": "/root/S",
        "region": [1, 2, 3, 4],
    }
    mock_client.screenshot_raw.assert_awaited_once_with(
        "/root/S", path=str(out.resolve())
    )


def test_screenshot_legacy_addon_base64_fallback(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """旧 addon 不认 path 参数、照旧回 {"image": base64}：CLI 必须本地解码
    落盘（版本错位窗口的优雅降级，issue #149；addon 一跑 init 即同步）。"""
    import base64 as _b64
    import json as _json

    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["screenshot"]
    out = tmp_path / "legacy.png"
    ns = __import__("argparse").Namespace(output_path=str(out), node=None)

    mock_client = AsyncMock()
    mock_client.screenshot_raw = AsyncMock(
        return_value={"image": _b64.b64encode(b"fake png bytes").decode()}
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
        rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))

    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["result"] == {"path": str(out), "bytes": len(b"fake png bytes")}
    assert out.read_bytes() == b"fake png bytes"


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
    """--all 中至少一条 stop 抛 DaemonError → rc=EXIT_PARTIAL（与 ffmpeg 转码失败 rc=4 区分）。"""
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

    from godot_cli_control.cli import cmd_daemon_stop, OUTPUT_JSON, EXIT_PARTIAL, EXIT_INFRA_ERROR, EXIT_TRANSCODE_FAILED
    import argparse
    ns = argparse.Namespace(all=True, project=None, output_format=OUTPUT_JSON)
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_PARTIAL
    assert EXIT_PARTIAL not in (EXIT_INFRA_ERROR, EXIT_TRANSCODE_FAILED), (
        "EXIT_PARTIAL 必须与 infra(2) / 单项目转码失败(4) 都区分，避免撞码"
    )


def test_daemon_stop_all_transcode_failure_does_not_promote_to_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """单条 stop 返回 4（ffmpeg 转码失败但 daemon 已停）不算 --all 失败 —— rc 应为 0。"""
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    proj = tmp_path / "p"
    proj.mkdir()
    import os
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x", log_path="y")

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.daemon import STOP_RC_TRANSCODE_FAILED
    monkeypatch.setattr(daemon_mod.Daemon, "stop", lambda self: STOP_RC_TRANSCODE_FAILED)

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

    # Task 5 起 --all --project 是允许的（"停某项目下所有实例"），
    # --all 与 --name 的互斥改为 cmd_daemon_stop 内显式校验，argparse 层不拦。
    ns = build_parser().parse_args(["daemon", "stop", "--all", "--project", "/tmp/x"])
    assert ns.all is True
    assert ns.project == Path("/tmp/x")


def test_daemon_start_idle_timeout_flag_parses() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "start", "--idle-timeout", "30m"])
    assert ns.idle_timeout == "30m"


def test_daemon_start_idle_timeout_default_is_zero() -> None:
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "start"])
    assert ns.idle_timeout == "0"


def test_daemon_start_time_scale_flag_parses() -> None:
    """daemon start --time-scale 2.5 → ns.time_scale == 2.5（float）。"""
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "start", "--time-scale", "2.5"])
    assert ns.time_scale == 2.5


def test_daemon_start_time_scale_default_is_none() -> None:
    """不传 --time-scale 时 ns.time_scale is None。"""
    from godot_cli_control.cli import build_parser
    ns = build_parser().parse_args(["daemon", "start"])
    assert ns.time_scale is None


@pytest.mark.parametrize("bad_value", ["abc", "0", "-1", "101"])
def test_daemon_start_time_scale_rejects_bad_value(
    bad_value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon start --time-scale <bad> → exit 64 + -1003 信封（argparse type 校验）。"""
    import json as _json
    from godot_cli_control.cli import EXIT_USAGE, main

    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "daemon", "start", "--time-scale", bad_value])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


@pytest.mark.parametrize("bad_path", ["demo.mp4", "demo.mov", "demo"])
def test_daemon_start_movie_path_rejects_bad_extension(
    bad_path: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon start --movie-path 非 .avi/.png → exit 64 + -1003 信封（#152）。

    Godot Movie Maker 只认 .avi/.png；传 .mp4 时 Godot 静默不录、exit 0 假成功，
    所以必须在启动前 argparse type 校验挡掉，错误信息要指路 .avi + 自动转码。"""
    import json as _json
    from godot_cli_control.cli import EXIT_USAGE, main

    monkeypatch.setattr(
        sys,
        "argv",
        ["godot-cli-control", "daemon", "start", "--record", "--movie-path", bad_path],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert ".avi" in payload["error"]["message"]


def test_run_movie_path_rejects_bad_extension(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run 与 daemon start 共用 _add_daemon_flags，同样的扩展名校验（#152）。

    断言信息含 .avi——必须在 argparse 层挡住，不能靠后面的「找不到脚本」
    凑出同样的 64/-1003 假通过。"""
    import json as _json
    from godot_cli_control.cli import EXIT_USAGE, main

    monkeypatch.setattr(
        sys,
        "argv",
        ["godot-cli-control", "run", "script.py", "--record", "--movie-path", "demo.mp4"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert ".avi" in payload["error"]["message"]


@pytest.mark.parametrize("good_path", ["demo.avi", "DEMO.AVI", "frames.png"])
def test_daemon_start_movie_path_accepts_avi_png(good_path: str) -> None:
    """.avi/.png（大小写不敏感）通过 argparse 校验，值原样保留。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(
        ["daemon", "start", "--record", "--movie-path", good_path]
    )
    assert ns.movie_path == good_path


def test_tree_accepts_max_nodes_flag() -> None:
    from godot_cli_control.cli import _preflight_tree, build_parser

    parser = build_parser()
    ns = parser.parse_args(["tree", "3", "--max-nodes", "50"])
    _preflight_tree(ns)
    assert ns._tree_depth == 3
    assert ns.max_nodes == 50
    assert ns._tree_path is None


def test_tree_max_nodes_default_is_200() -> None:
    from godot_cli_control.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["tree"])
    assert ns.max_nodes == 200


# ── find：服务端节点搜索（issue #153）──


def test_find_parser_defaults() -> None:
    """无过滤 flag 时全部 None、limit 默认 20；--from 落 ns.from_path
    （``from`` 是 Python 关键字，不能做属性名）。精确档叫 --exact 而非
    --text——全局输出格式 flag（--json/--text）注入每个子命令，撞名。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["find", "--type", "Button"])
    assert ns.type == "Button"
    assert ns.exact is None
    assert ns.contains is None
    assert ns.name_pattern is None
    assert ns.from_path is None
    assert ns.limit == 20


def test_find_parser_accepts_all_flags() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args([
        "find", "--from", "/root/GameUI", "--type", "Label",
        "--contains", "开始", "--name-pattern", "Inv*", "--limit", "5",
    ])
    assert ns.from_path == "/root/GameUI"
    assert ns.type == "Label"
    assert ns.contains == "开始"
    assert ns.name_pattern == "Inv*"
    assert ns.limit == 5


def test_find_exact_does_not_shadow_global_text_flag() -> None:
    """``find --exact X --text`` 同时可用：--text 仍是全局输出格式 flag。"""
    from godot_cli_control.cli import OUTPUT_TEXT, build_parser

    ns = build_parser().parse_args(["find", "--exact", "开始", "--text"])
    assert ns.exact == "开始"
    assert ns.output_format == OUTPUT_TEXT


def test_find_preflight_requires_at_least_one_filter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """全空过滤器 = tree 的活，必须在连 daemon **之前**报 EXIT_USAGE（契约 #5）。"""
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：find 无过滤器时不应连 daemon")

    import godot_cli_control.cli as cli_mod

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "find"])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "过滤器" in payload["error"]["message"]


def test_find_preflight_text_and_contains_exclusive() -> None:
    """--exact（精确）与 --contains（子串）互斥；单独给任一个都合法。"""
    from godot_cli_control.cli import _preflight_find, build_parser

    parser = build_parser()
    ns = parser.parse_args(["find", "--exact", "a", "--contains", "b"])
    with pytest.raises(ValueError, match="互斥"):
        _preflight_find(ns)
    _preflight_find(parser.parse_args(["find", "--exact", "a"]))  # 不抛即通过
    _preflight_find(parser.parse_args(["find", "--contains", "b"]))


def test_click_preflight_path_and_filters_exclusive() -> None:
    """click 定位方式二选一：path 与过滤器（含 --from）同给 / 都不给 都是用法错。"""
    from godot_cli_control.cli import _preflight_click, build_parser

    parser = build_parser()
    # path + 过滤器 → 互斥
    with pytest.raises(ValueError, match="互斥"):
        _preflight_click(
            parser.parse_args(["click", "/root/Btn", "--contains", "开始"])
        )
    # path + --from（单独）也互斥——--from 只在过滤器定位时有意义
    with pytest.raises(ValueError, match="互斥"):
        _preflight_click(
            parser.parse_args(["click", "/root/Btn", "--from", "/root/UI"])
        )
    # 都不给 → 用法错
    with pytest.raises(ValueError, match="过滤器"):
        _preflight_click(parser.parse_args(["click"]))
    # --from 不算独立过滤器
    with pytest.raises(ValueError, match="过滤器"):
        _preflight_click(parser.parse_args(["click", "--from", "/root/UI"]))
    # --exact / --contains 互斥（与 find 同款）
    with pytest.raises(ValueError, match="互斥"):
        _preflight_click(
            parser.parse_args(["click", "--exact", "a", "--contains", "b"])
        )
    # 合法形态：纯 path / 纯过滤器 / 过滤器 + --from
    _preflight_click(parser.parse_args(["click", "/root/Btn"]))
    _preflight_click(parser.parse_args(["click", "--contains", "开始"]))
    _preflight_click(
        parser.parse_args(["click", "--type", "Button", "--from", "/root/UI"])
    )


def test_cmd_click_passes_filters_to_client() -> None:
    """cmd_click 过滤器形态：全部 flag 透传 client.click；path 传 None。"""
    from unittest.mock import AsyncMock

    from godot_cli_control.cli import cmd_click

    mock_client = AsyncMock()
    mock_client.click = AsyncMock(return_value={"success": True, "path": "/root/B"})
    ns = __import__("argparse").Namespace(
        node_path=None, type="BaseButton", exact=None, contains="开始",
        name_pattern=None, from_path="/root/UI",
    )
    result = asyncio.run(cmd_click(mock_client, ns))
    assert result == {"success": True, "path": "/root/B"}
    mock_client.click.assert_awaited_once_with(
        None, node_type="BaseButton", text=None, text_contains="开始",
        name_pattern=None, from_path="/root/UI",
    )


def test_cmd_click_legacy_namespace_without_filter_fields() -> None:
    """老调用方只给 node_path 的 Namespace（缺过滤器字段）不能 AttributeError
    ——cmd_click 用 getattr 兜底（test_cli Namespace mock 缺字段坑的既往回归）。"""
    from unittest.mock import AsyncMock

    from godot_cli_control.cli import cmd_click

    mock_client = AsyncMock()
    mock_client.click = AsyncMock(return_value={"success": True})
    ns = __import__("argparse").Namespace(node_path="/root/Btn")
    result = asyncio.run(cmd_click(mock_client, ns))
    assert result == {"success": True}
    mock_client.click.assert_awaited_once_with(
        "/root/Btn", node_type=None, text=None, text_contains=None,
        name_pattern=None, from_path=None,
    )


def test_cmd_find_passes_namespace_args() -> None:
    """cmd_find 必须把 ns 的全部 flag 透传给 client.find_nodes。"""
    from unittest.mock import AsyncMock

    from godot_cli_control.cli import cmd_find

    mock_client = AsyncMock()
    mock_client.find_nodes = AsyncMock(return_value={"matches": []})
    ns = __import__("argparse").Namespace(
        type="Button", exact=None, contains="开始",
        name_pattern=None, from_path="/root/UI", limit=7,
    )
    result = asyncio.run(cmd_find(mock_client, ns))
    assert result == {"matches": []}
    mock_client.find_nodes.assert_awaited_once_with(
        node_type="Button", text=None, text_contains="开始",
        name_pattern=None, from_path="/root/UI", limit=7,
    )


def test_find_exit_code_follows_match_presence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """退出码语义对齐 exists：0=有匹配, 1=零匹配——shell ``if`` 可直接用。"""
    import json as _json

    from godot_cli_control.cli import OUTPUT_JSON, RPC_BY_NAME, _run_rpc
    from unittest.mock import AsyncMock, patch

    spec = RPC_BY_NAME["find"]
    ns = __import__("argparse").Namespace(
        type="Button", exact=None, contains=None,
        name_pattern=None, from_path=None, limit=20,
    )

    for matches, expected_rc in ([{"path": "/root/A", "type": "Button"}], 0), ([], 1):
        mock_client = AsyncMock()
        mock_client.find_nodes = AsyncMock(return_value={"matches": list(matches)})
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch("godot_cli_control.cli.GameClient", return_value=mock_client):
            rc = asyncio.run(_run_rpc(spec, ns, port=9999, fmt=OUTPUT_JSON))
        assert rc == expected_rc, f"matches={matches} 应给 rc={expected_rc}"
        payload = _json.loads(capsys.readouterr().out.strip())
        assert payload["ok"] is True


def test_fmt_find_text_lists_matches_and_truncation() -> None:
    """text 模式：每行 path + [type] + text；truncated 时附提示行；空给 no matches。"""
    from godot_cli_control.cli import _fmt_find_text

    rendered = _fmt_find_text({
        "matches": [
            {"path": "/root/UI/@Button@12", "type": "Button", "text": "开始游戏"},
            {"path": "/root/UI/Panel", "type": "Control"},
        ],
        "truncated": True,
    })
    lines = rendered.splitlines()
    assert "/root/UI/@Button@12" in lines[0]
    assert "Button" in lines[0]
    assert "开始游戏" in lines[0]
    assert "/root/UI/Panel" in lines[1]
    assert "truncated" in lines[-1]

    assert _fmt_find_text({"matches": []}) == "no matches"


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

    def test_detects_screenshot_in_sibling_via_from_import(
        self, tmp_path: Path
    ) -> None:
        """issue #151：主脚本不含 screenshot，但 from-import 的同目录 helper
        含 bridge.screenshot(...) → 仍判 True（一层 import 闭包）。"""
        from godot_cli_control.cli import _script_likely_uses_screenshot

        (tmp_path / "helpers.py").write_text(
            "def shoot(bridge):\n    bridge.screenshot('/tmp/x.png')\n", "utf-8"
        )
        script = tmp_path / "s.py"
        script.write_text(
            "from helpers import shoot\n\ndef run(bridge):\n    shoot(bridge)\n",
            "utf-8",
        )
        assert _script_likely_uses_screenshot(script) is True

    def test_detects_screenshot_in_sibling_via_plain_import(
        self, tmp_path: Path
    ) -> None:
        """``import helpers`` 形式同样命中（取顶层模块名映射同目录 .py）。"""
        from godot_cli_control.cli import _script_likely_uses_screenshot

        (tmp_path / "helpers.py").write_text(
            "def shoot(bridge):\n    bridge.screenshot('/tmp/x.png')\n", "utf-8"
        )
        script = tmp_path / "s.py"
        script.write_text(
            "import helpers\n\ndef run(bridge):\n    helpers.shoot(bridge)\n",
            "utf-8",
        )
        assert _script_likely_uses_screenshot(script) is True

    def test_no_match_when_sibling_clean(self, tmp_path: Path) -> None:
        """主脚本与被 import 的 helper 都不含 screenshot → False
        （守护：闭包扫描不得退化成无脑 True）。"""
        from godot_cli_control.cli import _script_likely_uses_screenshot

        (tmp_path / "helpers.py").write_text(
            "def helper(bridge):\n    bridge.click('/root/A')\n", "utf-8"
        )
        script = tmp_path / "s.py"
        script.write_text(
            "from helpers import helper\n\ndef run(bridge):\n    helper(bridge)\n",
            "utf-8",
        )
        assert _script_likely_uses_screenshot(script) is False

    def test_ignores_stdlib_import_without_sibling(self, tmp_path: Path) -> None:
        """``import os`` 等无同目录 .py 的 import 被安全跳过，不读 stdlib、不崩。"""
        from godot_cli_control.cli import _script_likely_uses_screenshot

        script = tmp_path / "s.py"
        script.write_text(
            "import os\nimport json\n\ndef run(bridge):\n    bridge.click('/root/A')\n",
            "utf-8",
        )
        assert _script_likely_uses_screenshot(script) is False

    def test_only_one_level_deep_no_recursion(self, tmp_path: Path) -> None:
        """只扫一层：helper 再 import 的 grandchild 含 screenshot 不算
        （issue #151 明确一层即可，避免递归扫描爆开）。"""
        from godot_cli_control.cli import _script_likely_uses_screenshot

        (tmp_path / "grandchild.py").write_text(
            "def shoot(bridge):\n    bridge.screenshot('/tmp/x.png')\n", "utf-8"
        )
        (tmp_path / "helpers.py").write_text(
            "from grandchild import shoot\n", "utf-8"
        )
        script = tmp_path / "s.py"
        script.write_text(
            "from helpers import shoot\n\ndef run(bridge):\n    shoot(bridge)\n",
            "utf-8",
        )
        assert _script_likely_uses_screenshot(script) is False

    def test_syntax_error_in_main_does_not_raise(self, tmp_path: Path) -> None:
        """主脚本语法错时 ast 解析失败也不抛 —— 闭包扫描静默放弃，
        子串检测仍在前面跑过（含 screenshot 子串仍能命中）。"""
        from godot_cli_control.cli import _script_likely_uses_screenshot

        script = tmp_path / "s.py"
        script.write_text("def run(bridge:\n    oops(\n", "utf-8")
        assert _script_likely_uses_screenshot(script) is False


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
            always_on_top=True,
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
            always_on_top=True,
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
            always_on_top=True,
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
    # issue #150: _tree_depth/_tree_path 由 _preflight_tree 填入；直接构造时手动设置
    ns = __import__("argparse").Namespace(
        tree_arg1="3", tree_arg2=None, max_nodes=200,
        _tree_depth=3, _tree_path=None,
    )

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
    # cmd_tree 应当把 ns._tree_depth / ns.max_nodes / ns._tree_path 透传给 client
    mock_client.get_scene_tree.assert_awaited_once_with(depth=3, max_nodes=200, path=None)


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
        always_on_top=True,
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


# ---------------------------------------------------------------------------
# daemon logs（issue #103）
# ---------------------------------------------------------------------------


def test_daemon_logs_subcommand_parses() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["daemon", "logs", "--tail", "20"])
    assert ns.cmd == "daemon" and ns.action == "logs"
    assert ns.tail == 20


def test_daemon_logs_tail_default_50() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["daemon", "logs"])
    assert ns.tail == 50


def test_daemon_logs_tail_out_of_range_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["daemon", "logs", "--tail", "0"])
    cap = capsys.readouterr()
    # 错误信息进 -1003 JSON 信封（stdout）；usage 行在 stderr（#82/#111 机制）
    assert "1..1000" in (cap.out + cap.err)


def test_daemon_logs_returns_tail_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """有日志文件 → JSON 信封带最后 N 行 + 路径，exit 0。"""
    import json as _json

    from godot_cli_control.cli import EXIT_OK, cmd_daemon_logs

    # Task 1 后日志文件移入 instances/default/（spec 2026-06-07）
    control = tmp_path / ".cli_control" / "instances" / "default"
    control.mkdir(parents=True)
    (control / "godot.log").write_text(
        "\n".join(f"line{i}" for i in range(100)), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    ns = __import__("argparse").Namespace(tail=10)
    rc = cmd_daemon_logs(ns)
    assert rc == EXIT_OK
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["result"]["lines"] == [f"line{i}" for i in range(90, 100)]
    assert payload["result"]["returned"] == 10
    assert payload["result"]["path"].endswith("godot.log")
    assert payload["result"]["instance"] == "default"


def test_daemon_logs_missing_file_is_infra_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """无 godot.log（daemon 从未启动）→ -1006，exit 2。"""
    import json as _json

    from godot_cli_control.cli import (
        CLIENT_CODE_PRECONDITION,
        EXIT_INFRA_ERROR,
        cmd_daemon_logs,
    )

    monkeypatch.chdir(tmp_path)
    ns = __import__("argparse").Namespace(tail=50)
    rc = cmd_daemon_logs(ns)
    assert rc == EXIT_INFRA_ERROR
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == CLIENT_CODE_PRECONDITION


# ---------------------------------------------------------------------------
# Task 5: daemon 子命令 --name 选靶 + stop 矩阵 + ls instance 列
# ---------------------------------------------------------------------------


def test_daemon_subcommands_accept_name() -> None:
    """start/run/status/logs/stop 五个子命令均接受 --name，
    且 status/logs/stop 不长出 start 专属 flag（如 --record）。"""
    from godot_cli_control.cli import build_parser

    parser = build_parser()
    # status / logs / stop 接受 --name
    for argv in (
        ["daemon", "status", "--name", "server"],
        ["daemon", "logs", "--name", "server"],
        ["daemon", "stop", "--name", "server"],
    ):
        ns = parser.parse_args(argv)
        assert ns.name == "server", f"argv={argv} ns.name 应为 'server'"

    # start 接受 --name
    ns = parser.parse_args(["daemon", "start", "--name", "server"])
    assert ns.name == "server"

    # run 接受 --name（Task 6 才真正用它；这里只校验注册）
    ns = parser.parse_args(["run", "x.py", "--name", "server"])
    assert ns.name == "server"

    # status 不应有 start 专属 --record
    with pytest.raises(SystemExit):
        parser.parse_args(["daemon", "status", "--record"])


def test_invalid_instance_name_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """非法实例名（含 /）→ argparse type 校验失败 → exit 64 + -1003 信封。"""
    import json as _json

    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["daemon", "start", "--name", "a/b"])
    assert exc_info.value.code == 64
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


def test_stop_all_with_name_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--all --name 组合非法 → exit 64 + -1003 信封。"""
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, OUTPUT_JSON, cmd_daemon_stop

    ns = __import__("argparse").Namespace(
        all=True,
        name="server",
        project=None,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


def test_stop_ambiguous_without_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cwd 有两个活实例但未传 --name → exit 64 + -1003，message 含两个实例名。"""
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, OUTPUT_JSON, cmd_daemon_stop

    # 铺两个活实例目录（pid=os.getpid() 确保探活通过）
    import os

    for name in ("server", "client1"):
        inst_dir = tmp_path / ".cli_control" / "instances" / name
        inst_dir.mkdir(parents=True)
        (inst_dir / "godot.pid").write_text(str(os.getpid()))

    monkeypatch.chdir(tmp_path)
    ns = __import__("argparse").Namespace(
        all=False,
        name=None,
        project=None,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    # message 应包含两个实例名
    msg = payload["error"]["message"]
    assert "server" in msg
    assert "client1" in msg


def test_stop_all_threads_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--all 全局停止时，以每条记录的 instance 字段构造 Daemon（防止永远打 default 回归）。"""
    import json as _json

    import godot_cli_control.registry as reg_mod
    from godot_cli_control.cli import EXIT_OK, OUTPUT_JSON, cmd_daemon_stop
    from godot_cli_control.registry import DaemonRecord

    # 两条不同实例的注册记录
    records = [
        DaemonRecord(
            project_root=str(tmp_path),
            pid=99991,
            port=9991,
            started_at="2026-01-01T00:00:00+00:00",
            godot_bin="/usr/bin/godot",
            log_path=str(tmp_path / "a.log"),
            instance="server",
        ),
        DaemonRecord(
            project_root=str(tmp_path),
            pid=99992,
            port=9992,
            started_at="2026-01-01T00:00:00+00:00",
            godot_bin="/usr/bin/godot",
            log_path=str(tmp_path / "b.log"),
            instance="client1",
        ),
    ]
    monkeypatch.setattr(reg_mod, "list_all", lambda: records)

    # 收集 Daemon.stop() 收到的 (project_root, instance)
    stop_calls: list[tuple[str, str]] = []

    import godot_cli_control.daemon as daemon_mod

    def _fake_stop(self: Any) -> int:
        stop_calls.append((str(self.project_root), self.instance))
        return 0

    monkeypatch.setattr(daemon_mod.Daemon, "stop", _fake_stop)

    ns = __import__("argparse").Namespace(
        all=True,
        name=None,
        project=None,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_OK
    # 两个实例各自被独立 stop
    assert sorted(stop_calls) == sorted(
        [(str(tmp_path), "server"), (str(tmp_path), "client1")]
    ), f"stop_calls={stop_calls}"
    # 信封包含 instance 字段
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    stopped = payload["result"]["stopped"]
    assert {e["instance"] for e in stopped} == {"server", "client1"}


def test_stop_all_project_combination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--all --project <tmp>：tmp 下两个活实例都被 stop。"""
    import json as _json
    import os

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_OK, OUTPUT_JSON, cmd_daemon_stop

    # 铺两个活实例目录
    for name in ("alpha", "beta"):
        inst_dir = tmp_path / ".cli_control" / "instances" / name
        inst_dir.mkdir(parents=True)
        (inst_dir / "godot.pid").write_text(str(os.getpid()))

    stop_calls: list[tuple[str, str]] = []

    def _fake_stop(self: Any) -> int:
        stop_calls.append((str(self.project_root), self.instance))
        return 0

    monkeypatch.setattr(daemon_mod.Daemon, "stop", _fake_stop)

    ns = __import__("argparse").Namespace(
        all=True,
        name=None,
        project=tmp_path,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_OK
    assert sorted(i for _, i in stop_calls) == ["alpha", "beta"]
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True


def test_stop_all_project_empty_emits_no_running(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--all --project <空项目>：无活实例且无 legacy daemon 时不伪造 default 条目（#144）。

    JSON 输出空数组，与 --all 全局空注册表的先例形状一致。
    """
    import json as _json

    from godot_cli_control.cli import EXIT_OK, OUTPUT_JSON, cmd_daemon_stop

    ns = __import__("argparse").Namespace(
        all=True,
        name=None,
        project=tmp_path,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_OK
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["result"] == {"stopped": [], "rc": 0}


def test_stop_all_project_empty_text_no_fake_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--all --project <空项目> text 模式：输出 (no running daemons) 而非 1/1 stopped（#144）。"""
    from godot_cli_control.cli import EXIT_OK, OUTPUT_TEXT, cmd_daemon_stop

    ns = __import__("argparse").Namespace(
        all=True,
        name=None,
        project=tmp_path,
        output_format=OUTPUT_TEXT,
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "(no running daemons)" in out
    assert "stopped" not in out.replace("(no running daemons)", "")


def test_stop_all_project_legacy_daemon_still_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--all --project：instances/ 为空但 legacy 平铺 daemon 在跑 → 仍回退 default 停掉它。"""
    import os

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_OK, OUTPUT_JSON, cmd_daemon_stop

    # legacy 平铺布局：.cli_control/godot.pid（无 instances/ 目录）
    legacy = tmp_path / ".cli_control"
    legacy.mkdir()
    (legacy / "godot.pid").write_text(str(os.getpid()))

    stop_calls: list[str] = []

    def _fake_stop(self: Any) -> int:
        stop_calls.append(self.instance)
        return 0

    monkeypatch.setattr(daemon_mod.Daemon, "stop", _fake_stop)

    ns = __import__("argparse").Namespace(
        all=True,
        name=None,
        project=tmp_path,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_OK
    assert stop_calls == ["default"]
    capsys.readouterr()


def test_ls_json_includes_instance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon ls JSON 模式：每条记录含 instance 字段。"""
    import json as _json

    import godot_cli_control.registry as reg_mod
    from godot_cli_control.cli import OUTPUT_JSON, cmd_daemon_ls
    from godot_cli_control.registry import DaemonRecord

    records = [
        DaemonRecord(
            project_root="/tmp/proj",
            pid=1234,
            port=9999,
            started_at="2026-01-01T00:00:00+00:00",
            godot_bin="/usr/bin/godot",
            log_path="/tmp/proj/.cli_control/instances/server/godot.log",
            instance="server",
        )
    ]
    monkeypatch.setattr(reg_mod, "list_all", lambda: records)

    ns = __import__("argparse").Namespace(output_format=OUTPUT_JSON)
    rc = cmd_daemon_ls(ns)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    daemons = payload["result"]["daemons"]
    assert len(daemons) == 1
    assert daemons[0]["instance"] == "server"


def test_ls_text_has_instance_column(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon ls text 模式：行格式含 instance 列（pid\\tport\\tinstance\\tproject_root\\tstarted_at）。"""
    import godot_cli_control.registry as reg_mod
    from godot_cli_control.cli import OUTPUT_TEXT, cmd_daemon_ls
    from godot_cli_control.registry import DaemonRecord

    records = [
        DaemonRecord(
            project_root="/tmp/proj",
            pid=5678,
            port=9988,
            started_at="2026-01-01T00:00:00+00:00",
            godot_bin="/usr/bin/godot",
            log_path="/tmp/proj/.cli_control/instances/server/godot.log",
            instance="server",
        )
    ]
    monkeypatch.setattr(reg_mod, "list_all", lambda: records)

    ns = __import__("argparse").Namespace(output_format=OUTPUT_TEXT)
    rc = cmd_daemon_ls(ns)
    assert rc == 0
    out = capsys.readouterr().out
    # 行格式：pid\tport\tinstance\tproject_root\tstarted_at
    assert "server" in out
    cols = out.strip().split("\t")
    assert cols[2] == "server", f"第 3 列应为 instance，实际: {cols}"


def test_daemon_start_json_includes_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """daemon start 成功时 JSON 信封 result 包含 instance 字段。"""
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import OUTPUT_JSON, cmd_daemon_start

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon_mod.Daemon, "start", lambda self, **kw: None)
    monkeypatch.setattr(daemon_mod.Daemon, "is_running", lambda self: True)
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9876)
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: 12345)

    ns = __import__("argparse").Namespace(
        name="server",
        record=False,
        movie_path=None,
        headless=True,
        gui=False,
        fps=30,
        port=0,
        idle_timeout="0",
        time_scale=None,
        always_on_top=True,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_start(ns)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["result"]["instance"] == "server"
    assert payload["result"]["started"] is True


def test_status_auto_selects_single_named_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """只有一个命名实例在跑时，daemon status 不传 --name 也能自动选中并正确报 running。"""
    import json as _json
    import os

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import OUTPUT_JSON, cmd_daemon_status

    # 铺 instances/server 活实例目录（pid=os.getpid()）
    inst_dir = tmp_path / ".cli_control" / "instances" / "server"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(os.getpid()))
    (inst_dir / "port").write_text("9876")

    monkeypatch.chdir(tmp_path)
    # 不 patch is_running，让真实 list_live_instances 探活（pid=os.getpid() 自身在跑）
    monkeypatch.setattr(daemon_mod.Daemon, "read_pid", lambda self: os.getpid())
    monkeypatch.setattr(daemon_mod.Daemon, "current_port", lambda self: 9876)

    ns = __import__("argparse").Namespace(
        name=None,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_daemon_status(ns)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["result"]["state"] == "running"
    assert payload["result"].get("instance") == "server"


# ── Task 6：顶层 --instance 选靶 + cmd_run 实例解析 ──


def test_top_level_instance_and_port_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--instance x --port 1 tree`` → SystemExit(64) + -1003 信封。

    argparse mutually_exclusive_group 报错经 _EnvelopeArgumentParser.error()
    落进 JSON 信封，exit code 64。
    """
    import json as _json

    from godot_cli_control.cli import EXIT_USAGE, build_parser

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--instance", "x", "--port", "1", "tree"])
    assert exc.value.code == EXIT_USAGE
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


def _make_two_live_instances(tmp_path: Path) -> None:
    """在 tmp_path 下铺两个活实例 server + client1（pid = os.getpid()）。"""
    import os

    for name in ("server", "client1"):
        d = tmp_path / ".cli_control" / "instances" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "godot.pid").write_text(str(os.getpid()))
        (d / "port").write_text("7001" if name == "server" else "7002")


def test_rpc_ambiguity_is_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """两实例活、无 --instance → preflight exit 64/-1003，不发网络连接。

    discover_port 在本地 FS 即可判定歧义（N≥2），CLI 必须在 _run_rpc 之前
    报错，不让 agent 等 30s connection retry（CLAUDE.md 契约 #5）。

    驱动真实 main()，而非复现其内部逻辑——防止 port-discovery 分支重构后测试失联。
    """
    import json as _json

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    _make_two_live_instances(tmp_path)
    monkeypatch.chdir(tmp_path)

    # 哨兵：_run_rpc 一旦被调就 fail——preflight 必须在它之前短路
    async def _sentinel(*_a: Any, **_kw: Any) -> int:
        pytest.fail("_run_rpc 不应被调用：preflight 应在 InstanceAmbiguityError 时短路")
        return 0  # 永不到达，满足类型检查

    monkeypatch.setattr(cli_mod, "_run_rpc", _sentinel)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "tree"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_USAGE
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "server" in payload["error"]["message"]
    assert "client1" in payload["error"]["message"]


def test_rpc_explicit_instance_not_running_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """只有 server 活、--instance nope → 64/-1003，message 含运行中实例名。

    驱动真实 main()，而非复现其内部逻辑——防止 port-discovery 分支重构后测试失联。
    """
    import json as _json
    import os

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    inst_dir = tmp_path / ".cli_control" / "instances" / "server"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(os.getpid()))
    (inst_dir / "port").write_text("7042")
    monkeypatch.chdir(tmp_path)

    # 哨兵：_run_rpc 一旦被调就 fail
    async def _sentinel(*_a: Any, **_kw: Any) -> int:
        pytest.fail("_run_rpc 不应被调用：--instance nope 应在连接前报错")
        return 0

    monkeypatch.setattr(cli_mod, "_run_rpc", _sentinel)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "--instance", "nope", "tree"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_USAGE
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "server" in payload["error"]["message"]


def test_rpc_single_instance_auto_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """唯一实例 server（port 7042）→ main() 经 discover_port 把 port=7042 传给 _run_rpc。

    驱动真实 main()，而非复现其内部逻辑——防止 port-discovery 分支重构后测试失联。
    """
    import os

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import main

    inst_dir = tmp_path / ".cli_control" / "instances" / "server"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(os.getpid()))
    (inst_dir / "port").write_text("7042")
    monkeypatch.chdir(tmp_path)

    captured_port: list[int] = []

    async def _fake_run_rpc(spec: Any, ns: Any, port: int, fmt: str) -> int:
        captured_port.append(port)
        return 0

    monkeypatch.setattr(cli_mod, "_run_rpc", _fake_run_rpc)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "tree"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert captured_port == [7042], f"应选 7042，实际 {captured_port}"


def test_rpc_explicit_instance_resolves_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """两实例活，--instance client1（port 7002）→ main() 把 port=7002 传给 _run_rpc。

    驱动真实 main()，验证 --instance 选靶经过完整 CLI 路径正确解析到 7002。
    """
    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import main

    _make_two_live_instances(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured_port: list[int] = []

    async def _fake_run_rpc(spec: Any, ns: Any, port: int, fmt: str) -> int:
        captured_port.append(port)
        return 0

    monkeypatch.setattr(cli_mod, "_run_rpc", _fake_run_rpc)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", "--instance", "client1", "tree"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert captured_port == [7002], f"应选 7002，实际 {captured_port}"


def test_run_ambiguous_without_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """两实例活，run x.py 无 --name → 64/-1003，不启 daemon。"""
    import argparse
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_USAGE, OUTPUT_JSON, cmd_run

    _make_two_live_instances(tmp_path)
    monkeypatch.chdir(tmp_path)

    # daemon start/is_running 不应被调用（断言不会 raise）
    _daemon_start_called: list[bool] = []

    def _boom_start(self: Any, **__: Any) -> None:
        _daemon_start_called.append(True)
        raise AssertionError("不应启动 daemon")

    monkeypatch.setattr(daemon_mod.Daemon, "start", _boom_start)

    script = tmp_path / "x.py"
    script.write_text("def run(bridge): pass\n", encoding="utf-8")

    ns = argparse.Namespace(
        script=str(script),
        name=None,  # 无 --name
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
    assert rc == EXIT_USAGE
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert not _daemon_start_called, "不应调用 daemon.start"


def test_run_auto_selects_single_named_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """唯一 server 实例活 → cmd_run 用 instance='server'，auto_started=False。"""
    import argparse
    import os

    import godot_cli_control.daemon as daemon_mod
    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import OUTPUT_JSON, cmd_run

    inst_dir = tmp_path / ".cli_control" / "instances" / "server"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(os.getpid()))
    (inst_dir / "port").write_text("7042")
    monkeypatch.chdir(tmp_path)

    # 桩掉 _exec_user_script，捕获收到的 port
    captured_port: list[int] = []

    def _fake_exec(script_path: Any, port: int, **kw: Any) -> int:
        captured_port.append(port)
        return 0

    monkeypatch.setattr(cli_mod, "_exec_user_script", _fake_exec)

    # daemon.start 不应被调用（auto_started 应为 False）
    _start_called: list[bool] = []

    def _boom_start(self: Any, **__: Any) -> None:
        _start_called.append(True)
        raise AssertionError("不应启动 daemon（实例已在跑）")

    monkeypatch.setattr(daemon_mod.Daemon, "start", _boom_start)

    script = tmp_path / "x.py"
    script.write_text("def run(bridge): pass\n", encoding="utf-8")

    ns = argparse.Namespace(
        script=str(script),
        name=None,  # 不传 --name，自动选唯一实例
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
    assert rc == 0, f"脚本应成功退出，got {rc}"
    assert captured_port == [7042], f"应连 port 7042，实际 {captured_port}"
    assert not _start_called, "不应调用 daemon.start（server 实例已在跑）"


# ── I1 修复：顶层 --instance 对 run/daemon 子命令生效 ──


def test_top_level_instance_works_for_daemon_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """两实例活、``--instance server daemon stop``（无 --name）→ 停 server，不报歧义。

    顶层 --instance 是 --name 的等价写法，在 daemon 子命令路径也必须生效。
    若不生效，_resolve_daemon_instance 会看到 N≥2 且无 --name，报歧义 exit 64。
    驱动真实 main()，验证完整 CLI 路径不静默吞掉 --instance。
    """
    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_OK, main

    # 铺两个活实例，若 --instance 被吞 _resolve_daemon_instance 会报歧义
    _make_two_live_instances(tmp_path)
    monkeypatch.chdir(tmp_path)

    stopped_instances: list[str] = []

    def _fake_stop(self: Any) -> int:
        stopped_instances.append(self.instance)
        return 0  # cmd_daemon_stop 把此返回值作为 exit code

    monkeypatch.setattr(daemon_mod.Daemon, "stop", _fake_stop)

    monkeypatch.setattr(
        sys, "argv", ["godot-cli-control", "--instance", "server", "daemon", "stop"]
    )
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_OK, f"期望 exit 0，实际 {exc.value.code}"
    assert stopped_instances == ["server"], (
        f"顶层 --instance 应选靶 server，实际 stop 收到 {stopped_instances}"
    )


def test_top_level_instance_works_for_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--instance server run x.py`` → cmd_run 用 Daemon(instance='server')。

    顶层 --instance 在 run 子命令路径（_resolve_daemon_instance）也必须生效。
    驱动真实 main()，防止重构失联。
    """
    import os

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_OK, main

    inst_dir = tmp_path / ".cli_control" / "instances" / "server"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(os.getpid()))
    (inst_dir / "port").write_text("7001")
    monkeypatch.chdir(tmp_path)

    script = tmp_path / "x.py"
    script.write_text("def run(bridge): pass\n", encoding="utf-8")

    captured_exec: list[tuple[Any, int]] = []

    def _fake_exec(script_path: Any, port: int, **kw: Any) -> int:
        captured_exec.append((script_path, port))
        return 0

    monkeypatch.setattr(cli_mod, "_exec_user_script", _fake_exec)

    monkeypatch.setattr(
        sys,
        "argv",
        ["godot-cli-control", "--instance", "server", "run", str(script)],
    )
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_OK, f"期望 exit 0，实际 {exc.value.code}"
    assert captured_exec, "cmd_run 应调用 _exec_user_script"
    assert captured_exec[0][1] == 7001, (
        f"应连 server 实例的 port 7001，实际 {captured_exec[0][1]}"
    )


def test_name_and_instance_conflict_is_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--instance a daemon stop --name b`` → exit 64/-1003，message 含两名字。

    顶层 --instance 与子命令 --name 同时传且值不同，属于用法错误，
    必须在连 daemon 之前报错并以 JSON 信封返回。
    """
    import json as _json
    import os

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    inst_dir = tmp_path / ".cli_control" / "instances" / "a"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(os.getpid()))
    (inst_dir / "port").write_text("7001")
    monkeypatch.chdir(tmp_path)

    stop_called: list[bool] = []

    def _boom_stop(self: Any) -> None:
        stop_called.append(True)
        raise AssertionError("不应调用 stop：冲突应在 preflight 报错")

    monkeypatch.setattr(daemon_mod.Daemon, "stop", _boom_stop)

    monkeypatch.setattr(
        sys,
        "argv",
        ["godot-cli-control", "--instance", "a", "daemon", "stop", "--name", "b"],
    )
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_USAGE, f"期望 exit 64，实际 {exc.value.code}"
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "a" in payload["error"]["message"], "message 应含 --instance 值 a"
    assert "b" in payload["error"]["message"], "message 应含 --name 值 b"
    assert not stop_called, "不应调用 stop"


def test_run_parses_time_scale():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["run", "script.py", "--time-scale", "5"])
    assert ns.time_scale == 5.0


def test_run_passes_time_scale_to_daemon_start(tmp_path, monkeypatch, capsys):
    """cmd_run 必须把 --time-scale 透传进 daemon.start（与 daemon start 对称）。"""
    from godot_cli_control import cli

    script = tmp_path / "s.py"
    script.write_text("def run(bridge):\n    pass\n")

    captured = {}

    class _FakeDaemon:
        def __init__(self, *a, **k):
            pass

        def is_running(self):
            return False

        def start(self, **kwargs):
            captured.update(kwargs)
            return 1

        def current_port(self):
            return 12345

        def stop(self):
            return 0

    import godot_cli_control.daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "Daemon", _FakeDaemon)
    monkeypatch.setattr(cli, "_exec_user_script", lambda *a, **k: 0)

    ns = cli.build_parser().parse_args(["run", str(script), "--time-scale", "5"])
    rc = cli.cmd_run(ns)
    assert rc == 0
    assert captured.get("time_scale") == 5.0


def test_name_and_instance_same_value_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--instance server daemon stop --name server`` → 正常，不报冲突。

    相同值的冗余传参是合法写法（脚本模板可能同时传两个），不应报错。
    驱动真实 main()。
    """
    import os

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control.cli import EXIT_OK, main

    inst_dir = tmp_path / ".cli_control" / "instances" / "server"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(os.getpid()))
    (inst_dir / "port").write_text("7001")
    monkeypatch.chdir(tmp_path)

    stopped_instances: list[str] = []

    def _fake_stop(self: Any) -> int:
        stopped_instances.append(self.instance)
        return 0  # cmd_daemon_stop 把此返回值作为 exit code

    monkeypatch.setattr(daemon_mod.Daemon, "stop", _fake_stop)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "godot-cli-control",
            "--instance",
            "server",
            "daemon",
            "stop",
            "--name",
            "server",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_OK, f"期望 exit 0，实际 {exc.value.code}"
    assert stopped_instances == ["server"], (
        f"相同值时应正常停 server，实际 {stopped_instances}"
    )


# ── I1 回归：stop --all 拦截顶层 --instance ──


def test_stop_all_with_top_level_instance_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``--instance server daemon stop --all`` 必须被拦截为用法错误 (exit 64 / -1003)。

    顶层 ``--instance`` 是实例选靶语义，与 ``--all`` 互斥。
    原先只查 ``ns.name``，导致顶层 ``--instance`` 被静默吞掉——全局停掉所有实例。
    修复后应立即报错，且 Daemon.stop 和 registry.list_all 均不应被调用。
    """
    import json as _json

    import godot_cli_control.daemon as daemon_mod
    from godot_cli_control import registry
    from godot_cli_control.cli import EXIT_USAGE, main

    # 哨兵：任何 stop / list_all 调用都算断言失败
    def _should_not_stop(self: Any) -> int:
        raise AssertionError("stop --all 拦截失效：Daemon.stop 不应被调用")

    def _should_not_list_all() -> list:
        raise AssertionError("stop --all 拦截失效：registry.list_all 不应被调用")

    monkeypatch.setattr(daemon_mod.Daemon, "stop", _should_not_stop)
    monkeypatch.setattr(registry, "list_all", _should_not_list_all)

    monkeypatch.setattr(
        sys,
        "argv",
        ["godot-cli-control", "--instance", "server", "daemon", "stop", "--all"],
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_USAGE, f"期望 exit 64，实际 {exc.value.code}"
    out = capsys.readouterr().out.strip()
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    # 消息应提到互斥语义，便于 agent 理解
    msg = payload["error"]["message"]
    assert "--all" in msg, f"message 应含 '--all'，实际：{msg!r}"


# ── issue #150: tree [path] [depth] 启发式位置参数 + preflight 消歧 ──────────


@pytest.mark.parametrize(
    "argv,expect_path,expect_depth",
    [
        (["tree"], None, 3),
        (["tree", "0"], None, 0),
        (["tree", "2"], None, 2),
        (["tree", "/root/GameUI"], "/root/GameUI", 3),
        (["tree", "/root/GameUI", "2"], "/root/GameUI", 2),
    ],
)
def test_tree_preflight_disambiguates(
    argv: list[str], expect_path: str | None, expect_depth: int
) -> None:
    """issue #150：/ 前缀当 path，否则当 depth；结果 stash 到 ns。"""
    from godot_cli_control.cli import _preflight_tree, build_parser

    ns = build_parser().parse_args(argv)
    _preflight_tree(ns)
    assert ns._tree_path == expect_path
    assert ns._tree_depth == expect_depth


@pytest.mark.parametrize(
    "bad_argv",
    [
        ["tree", "GameUI"],   # 漏斜杠 → 当 depth → int 失败（fail-loud）
        ["tree", "2", "3"],   # depth-only 形式带多余尾随 token
        ["tree", "abc"],      # 非法 depth
    ],
)
def test_tree_preflight_rejects_usage_errors(
    bad_argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #150：tree 用法错必须在连 daemon 之前报 EXIT_USAGE（-1003）。"""
    import json as _json

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：tree 用法错时不应连 daemon")

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", *bad_argv])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


@pytest.mark.asyncio
async def test_cmd_tree_forwards_path_and_depth() -> None:
    """issue #150：cmd_tree 把 preflight 解析出的 path/depth 透传给 client。"""
    from godot_cli_control.cli import _preflight_tree, build_parser, cmd_tree

    captured: dict = {}

    class _FakeClient:
        async def get_scene_tree(
            self, depth: int, max_nodes: int | None = None, path: str | None = None
        ) -> dict:
            captured.update(depth=depth, max_nodes=max_nodes, path=path)
            return {"tree": {}}

    ns = build_parser().parse_args(["tree", "/root/GameUI", "2"])
    _preflight_tree(ns)
    await cmd_tree(_FakeClient(), ns)
    assert captured == {"depth": 2, "max_nodes": 200, "path": "/root/GameUI"}


# ── issue #154：click-at / mouse-move 参数解析 + preflight + handler ──


@pytest.mark.parametrize(
    "argv,expected",
    [
        (
            ["click-at", "320", "240"],
            {"x": "320", "y": "240", "node": None, "button": "left", "double": False},
        ),
        (
            ["click-at", "--node", "/root/Foo", "--button", "right"],
            {"x": None, "y": None, "node": "/root/Foo", "button": "right", "double": False},
        ),
        (["click-at", "10", "20", "--double"], {"x": "10", "y": "20", "double": True}),
        (["click-at", "5", "6", "--button", "middle"], {"button": "middle"}),
        (["mouse-move", "100", "120"], {"x": "100", "y": "120", "node": None}),
        (["mouse-move", "--node", "/root/Bar"], {"x": None, "y": None, "node": "/root/Bar"}),
    ],
)
def test_click_at_mouse_move_parse(
    argv: list[str], expected: dict[str, Any]
) -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(argv)
    assert ns.cmd == argv[0]
    for attr, val in expected.items():
        assert getattr(ns, attr) == val, (
            f"{argv[0]}: ns.{attr} 期望 {val!r}，实际 {getattr(ns, attr)!r}"
        )


@pytest.mark.parametrize(
    "argv,err_match",
    [
        (["click-at", "10", "20", "--node", "/root/Foo"], "互斥"),
        (["click-at"], "坐标"),
        (["click-at", "10"], "坐标"),  # 只给 x，y 缺
        (["click-at", "abc", "def"], "数字"),
        (["mouse-move", "5", "10", "--node", "/root/Bar"], "互斥"),
        (["mouse-move"], "坐标"),
    ],
)
def test_click_at_mouse_move_preflight_usage_errors(
    argv: list[str], err_match: str
) -> None:
    from godot_cli_control.cli import RPC_SPECS, build_parser

    spec = next(s for s in RPC_SPECS if s.name == argv[0])
    assert spec.preflight is not None, f"{argv[0]} 应有 preflight"
    ns = build_parser().parse_args(argv)
    with pytest.raises(ValueError, match=err_match):
        spec.preflight(ns)


@pytest.mark.parametrize(
    "argv",
    [
        ["click-at", "10", "20"],
        ["click-at", "--node", "/root/Foo"],
        ["mouse-move", "100", "120"],
        ["mouse-move", "--node", "/root/Bar"],
    ],
)
def test_click_at_mouse_move_preflight_ok(argv: list[str]) -> None:
    from godot_cli_control.cli import RPC_SPECS, build_parser

    spec = next(s for s in RPC_SPECS if s.name == argv[0])
    ns = build_parser().parse_args(argv)
    spec.preflight(ns)  # 合法用法不应抛


@pytest.mark.asyncio
async def test_cmd_click_at_passes_coords_to_client() -> None:
    from godot_cli_control.cli import build_parser, cmd_click_at

    captured: dict = {}

    class _FakeClient:
        async def click_at(self, x, y, *, node=None, button="left", double=False):
            captured.update(x=x, y=y, node=node, button=button, double=double)
            return {"x": x, "y": y}

    ns = build_parser().parse_args(["click-at", "320", "240", "--button", "right"])
    await cmd_click_at(_FakeClient(), ns)
    # 字面坐标走 float 转换
    assert captured == {
        "x": 320.0, "y": 240.0, "node": None, "button": "right", "double": False
    }


@pytest.mark.asyncio
async def test_cmd_click_at_node_mode() -> None:
    from godot_cli_control.cli import build_parser, cmd_click_at

    captured: dict = {}

    class _FakeClient:
        async def click_at(self, x, y, *, node=None, button="left", double=False):
            captured.update(node=node, button=button, double=double)
            return {"x": 1, "y": 2}

    ns = build_parser().parse_args(["click-at", "--node", "/root/Slot", "--double"])
    await cmd_click_at(_FakeClient(), ns)
    assert captured["node"] == "/root/Slot"
    assert captured["double"] is True


@pytest.mark.asyncio
async def test_cmd_mouse_move_passes_coords() -> None:
    from godot_cli_control.cli import build_parser, cmd_mouse_move

    captured: dict = {}

    class _FakeClient:
        async def mouse_move(self, x, y, *, node=None):
            captured.update(x=x, y=y, node=node)
            return {"x": x, "y": y, "relative": [x, y]}

    ns = build_parser().parse_args(["mouse-move", "100", "120"])
    await cmd_mouse_move(_FakeClient(), ns)
    assert captured == {"x": 100.0, "y": 120.0, "node": None}


# ── issue #154 P2：drag 参数解析 + preflight + handler ──
# CLI 面：``drag [x1 y1 x2 y2] [--from-node P] [--to-node Q] [--button] [--duration]
# [--steps]``。坐标用变长 coords 列表 + 内容消歧（tree 同款）：用 --from-node /
# --to-node 的那一端不占坐标，故 coords 个数 ∈ {0,2,4}。


@pytest.mark.parametrize(
    "argv,expected",
    [
        (
            ["drag", "0", "0", "100", "50"],
            {
                "coords": ["0", "0", "100", "50"],
                "from_node": None, "to_node": None,
                "button": "left", "duration": "0.3", "steps": "10",
            },
        ),
        (
            ["drag", "--from-node", "/root/A", "--to-node", "/root/B", "--button", "right"],
            {"coords": [], "from_node": "/root/A", "to_node": "/root/B", "button": "right"},
        ),
        (
            ["drag", "10", "20", "--to-node", "/root/B", "--duration", "0.5", "--steps", "20"],
            {"coords": ["10", "20"], "to_node": "/root/B", "duration": "0.5", "steps": "20"},
        ),
    ],
)
def test_drag_parse(argv: list[str], expected: dict[str, Any]) -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(argv)
    assert ns.cmd == "drag"
    for attr, val in expected.items():
        assert getattr(ns, attr) == val, (
            f"drag: ns.{attr} 期望 {val!r}，实际 {getattr(ns, attr)!r}"
        )


@pytest.mark.parametrize(
    "argv,err_match",
    [
        # 用 --from-node 的同时又给了 4 个坐标 → 起点端不该占坐标，个数对不上
        (["drag", "0", "0", "1", "1", "--from-node", "/root/A"], "坐标"),
        (["drag", "0", "0", "1", "1", "--to-node", "/root/B"], "坐标"),
        (["drag", "--from-node", "/root/A"], "坐标"),  # 终点端缺坐标
        (["drag", "0", "0"], "坐标"),                  # 终点端缺坐标
        (["drag", "abc", "def", "1", "1"], "数字"),     # 坐标非数字
        (["drag", "0", "0", "1", "1", "--steps", "0"], "steps"),
        (["drag", "0", "0", "1", "1", "--duration", "-1"], "duration"),
    ],
)
def test_drag_preflight_usage_errors(argv: list[str], err_match: str) -> None:
    from godot_cli_control.cli import RPC_SPECS, build_parser

    spec = next(s for s in RPC_SPECS if s.name == "drag")
    assert spec.preflight is not None
    ns = build_parser().parse_args(argv)
    with pytest.raises(ValueError, match=err_match):
        spec.preflight(ns)


@pytest.mark.parametrize(
    "argv",
    [
        ["drag", "0", "0", "100", "50"],                            # 起终都用坐标
        ["drag", "--from-node", "/root/A", "--to-node", "/root/B"],  # 起终都用节点
        ["drag", "10", "20", "--to-node", "/root/B"],               # 起点坐标 + 终点节点
        ["drag", "--from-node", "/root/A", "30", "40"],             # 起点节点 + 终点坐标
    ],
)
def test_drag_preflight_ok(argv: list[str]) -> None:
    from godot_cli_control.cli import RPC_SPECS, build_parser

    spec = next(s for s in RPC_SPECS if s.name == "drag")
    ns = build_parser().parse_args(argv)
    spec.preflight(ns)  # 合法用法不抛


@pytest.mark.asyncio
async def test_cmd_drag_literal_coords() -> None:
    from godot_cli_control.cli import build_parser, cmd_drag

    captured: dict = {}

    class _FakeClient:
        async def drag(self, x1, y1, x2, y2, *, from_node=None, to_node=None,
                       button="left", duration=0.3, steps=10):
            captured.update(
                x1=x1, y1=y1, x2=x2, y2=y2, from_node=from_node, to_node=to_node,
                button=button, duration=duration, steps=steps,
            )
            return {"from": [x1, y1], "to": [x2, y2]}

    ns = build_parser().parse_args(
        ["drag", "0", "0", "100", "50", "--button", "right", "--steps", "5"]
    )
    await cmd_drag(_FakeClient(), ns)
    # cmd_drag 自己经 _resolve_drag 把坐标转 float、duration/steps 归一
    assert captured == {
        "x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 50.0,
        "from_node": None, "to_node": None,
        "button": "right", "duration": 0.3, "steps": 5,
    }


@pytest.mark.asyncio
async def test_cmd_drag_node_endpoints() -> None:
    from godot_cli_control.cli import build_parser, cmd_drag

    captured: dict = {}

    class _FakeClient:
        async def drag(self, x1, y1, x2, y2, *, from_node=None, to_node=None,
                       button="left", duration=0.3, steps=10):
            captured.update(from_node=from_node, to_node=to_node)
            return {"from": [1, 2], "to": [3, 4]}

    ns = build_parser().parse_args(["drag", "--from-node", "/root/A", "--to-node", "/root/B"])
    await cmd_drag(_FakeClient(), ns)
    assert captured == {"from_node": "/root/A", "to_node": "/root/B"}


@pytest.mark.asyncio
async def test_cmd_drag_mixed_from_node_to_literal() -> None:
    from godot_cli_control.cli import build_parser, cmd_drag

    captured: dict = {}

    class _FakeClient:
        async def drag(self, x1, y1, x2, y2, *, from_node=None, to_node=None,
                       button="left", duration=0.3, steps=10):
            captured.update(x1=x1, y1=y1, x2=x2, y2=y2, from_node=from_node, to_node=to_node)
            return {}

    # 起点用节点，终点 30 40 由 coords 落到 to 端（内容消歧）
    ns = build_parser().parse_args(["drag", "--from-node", "/root/A", "30", "40"])
    await cmd_drag(_FakeClient(), ns)
    assert captured == {
        "x1": 0.0, "y1": 0.0, "x2": 30.0, "y2": 40.0,
        "from_node": "/root/A", "to_node": None,
    }


# ── #157 item2：RPC 子命令后置 --port / --instance ──

def test_rpc_subcommand_accepts_trailing_port():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["exists", "/root/Foo", "--port", "9999"])
    assert ns.port == 9999


def test_rpc_subcommand_leading_port_still_works():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["--port", "9999", "exists", "/root/Foo"])
    assert ns.port == 9999


def test_rpc_subcommand_trailing_instance_all():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["exists", "/root/Foo", "--instance", "all"])
    assert ns.instance == "all"


def test_cross_position_port_and_instance_conflict(monkeypatch, capsys):
    """--instance 前置 + --port 后置：两个 mutex 都照不到，guard 兜成 usage 错。"""
    from godot_cli_control import cli
    monkeypatch.setattr(
        sys, "argv",
        ["godot-cli-control", "--instance", "client1", "exists", "/root/Foo", "--port", "5"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 64
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == -1003


# ── emit-signal 子命令（#157 item4）──


def test_emit_signal_parses_positionals_and_args():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["emit-signal", "/root/X", "item_selected", "0", "ok"])
    assert ns.node_path == "/root/X"
    assert ns.signal == "item_selected"
    assert ns.args == ["0", "ok"]


def test_emit_signal_handler_passes_decoded_args():
    import asyncio
    from godot_cli_control import cli
    captured = {}

    class _FakeClient:
        async def emit_signal(self, path, signal, args=None):
            captured.update(path=path, signal=signal, args=args)
            return {"emitted": True}

    ns = cli.build_parser().parse_args(["emit-signal", "/root/X", "ping", "42", "hi"])
    asyncio.run(cli.cmd_emit_signal(_FakeClient(), ns))
    assert captured == {"path": "/root/X", "signal": "ping", "args": [42, "hi"]}


# ── --allow-emit-signal 解析层测试（#157 item4）──


def test_daemon_start_parses_allow_emit_signal():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["daemon", "start", "--allow-emit-signal"])
    assert ns.allow_emit_signal is True


def test_daemon_start_allow_emit_signal_default_false():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["daemon", "start"])
    assert ns.allow_emit_signal is False


def test_run_parses_allow_emit_signal():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["run", "s.py", "--allow-emit-signal"])
    assert ns.allow_emit_signal is True


# ── wait-signal --trigger preflight（#155）──


class TestWaitSignalTriggerPreflight:
    def _ns(self, **kw):
        base = {"timeout": None, "trigger": None, "node_path": "/root/A",
                "signal_name": "ping"}
        base.update(kw)
        return type("NS", (), base)()

    def test_valid_trigger_caches_spec_and_ns(self) -> None:
        from godot_cli_control import cli
        ns = self._ns(trigger="tap interact")
        cli._preflight_wait_signal(ns)
        assert ns._trigger_spec.name == "tap"
        assert ns._trigger_ns.cmd == "tap"

    def test_empty_trigger_rejected(self) -> None:
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="   "))

    def test_non_rpc_trigger_rejected(self) -> None:
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="daemon start"))

    def test_nested_wait_trigger_rejected(self) -> None:
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="wait-signal /root/B x"))

    def test_trigger_subcommand_preflight_runs(self) -> None:
        # combo 无 steps → 其自身 preflight 抛 → 包成 wait-signal 的 ValueError
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="combo"))

    def test_trigger_none_no_error(self) -> None:
        from godot_cli_control import cli
        ns = self._ns(trigger=None)
        cli._preflight_wait_signal(ns)  # 不抛即通过
        assert not hasattr(ns, "_trigger_spec")

    def test_wait_signal_trigger_flag_parsed_by_build_parser(self) -> None:
        from godot_cli_control import cli
        ns = cli.build_parser().parse_args(
            ["wait-signal", "/root/A", "fired", "--trigger", "tap interact"]
        )
        assert ns.trigger == "tap interact"

    # M1：守护 stdout 不被 argparse help/error 污染（C1 + C2 回归）
    def test_trigger_help_does_not_pollute_stdout(self, capsys) -> None:
        """--trigger 'tap -h' 应抛 ValueError，stdout 必须干净（C1 回归）。"""
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="tap -h"))
        assert capsys.readouterr().out == ""

    def test_trigger_missing_arg_no_stdout_pollution(self, capsys) -> None:
        """--trigger 'tap'（缺必填 action）应抛 ValueError，stdout 不出现 JSON（C2 回归）。"""
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="tap"))
        assert capsys.readouterr().out == ""

    def test_empty_string_trigger_rejected(self) -> None:
        """I2：--trigger ''（空字符串）必须被 preflight 拒绝（if trigger is not None 修复）。"""
        from godot_cli_control import cli
        with pytest.raises(ValueError, match="不能为空"):
            cli._preflight_wait_signal(self._ns(trigger=""))
