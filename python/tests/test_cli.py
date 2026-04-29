"""CLI 单元测试 —— 覆盖 ``_exec_user_script`` 的脚本加载边界。

不实际启动 Godot：用 monkeypatch 替换 ``GameBridge``，只验证 importer 行为。
"""

from __future__ import annotations

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
