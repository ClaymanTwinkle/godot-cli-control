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
