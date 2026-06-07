"""--instance all 广播（issue #145）CLI 层单测。

覆盖：保留名 all 的 CLI 面拒绝、顶层 --instance all 放行、daemon/run 路径拒绝、
{instance} 占位符替换、screenshot preflight 守卫、_run_rpc_broadcast 聚合信封
与退出码矩阵、main() 接线。daemon.py 层的保留名校验在 test_daemon.py。
"""

from __future__ import annotations

import json

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
