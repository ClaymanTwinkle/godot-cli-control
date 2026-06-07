"""--instance all 广播（issue #145）CLI 层单测。

覆盖：保留名 all 的 CLI 面拒绝、顶层 --instance all 放行、daemon/run 路径拒绝、
{instance} 占位符替换、screenshot preflight 守卫、_run_rpc_broadcast 聚合信封
与退出码矩阵、main() 接线。daemon.py 层的保留名校验在 test_daemon.py。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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
