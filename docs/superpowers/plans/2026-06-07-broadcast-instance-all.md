# `--instance all` 广播多实例 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--instance all` 让任一 RPC 子命令对 cwd 项目全部活实例并发执行并聚合输出（issue #145）。

**Architecture:** 纯 Python 客户端编排，GDScript 零改动。`main()` 在 RPC 路径上分流：`ns.instance == "all"` → `_run_rpc_broadcast`（枚举活实例 → `asyncio.gather` 并发跑同一 `RpcSpec.handler` → 聚合信封）。`all` 成为保留实例名；`{instance}` 占位符逐实例替换；screenshot 加 preflight 守卫。退出码复用 `EXIT_PARTIAL`(3)。

**Tech Stack:** Python ≥3.10 / asyncio / argparse / pytest（mock GameClient）/ 真 Godot e2e。

**Spec:** `docs/superpowers/specs/2026-06-07-broadcast-instance-all-design.md`

---

## 执行环境须知（先读）

- 仓库根：`/Users/kesar/Projects/godot-cli-control`；venv：`.venv/bin/python`（Python 3.14，已装 pytest / 本包 editable）。
- **先建分支**：`git checkout -b feat/145-broadcast-instance-all main`（本仓不用 stacked PR，单分支串行）。
- 单个测试文件直接 `.venv/bin/python -m pytest <file> -q` 跑；**全量套件**（Task 8）必须 `coverage run -m pytest`（不是 `pytest --cov`，原因见 pyproject 注释），且按仓规派 subagent（model sonnet、**禁 run_in_background**）执行、只回传精简结论。
- e2e（Task 6）需要真 Godot：`godot` 在 `~/.local/bin`（PATH 里）。本机必须真跑，不许以缺 GODOT_BIN 为由跳过。
- SKILL.md 仓内渲染副本必须用 **Python 3.12 + COLUMNS=80** 重渲染（Task 7），本机有 `/usr/local/bin/python3.12`。
- 行文/注释风格：中文注释、解释"为什么"，对齐 `cli.py` / `daemon.py` 现有密度。

## File Structure

| 文件 | 职责 / 改动 |
|---|---|
| `python/godot_cli_control/daemon.py` | `validate_instance_name` 拒绝保留名 `all`；`list_live_instances` 跳过存量 `all` 目录 |
| `python/godot_cli_control/cli.py` | 顶层 `--instance` 放行 `all`；daemon/run 路径拒绝；`_rpc_failure_envelope` 提取；`{instance}` 替换；screenshot preflight；`_run_rpc_broadcast` + `main()` 接线 |
| `python/tests/test_daemon.py` | 保留名 / 目录跳过单测（追加） |
| `python/tests/test_broadcast.py` | **新建**：本 feature 全部 CLI 层单测 |
| `python/tests/test_e2e_multi_instance.py` | 追加广播 e2e（真 Godot 双实例） |
| `python/godot_cli_control/templates/skill/SKILL.md` | broadcast 文档段 + 退出码表 |
| `.claude/skills/godot-cli-control/SKILL.md` | 重渲染副本 |
| `CLAUDE.md` | 退出码 3 措辞 |
| `addons/godot_cli_control/CHANGELOG.md` | `[Unreleased]` 记 feature |

---

### Task 1: 保留实例名 `all`（daemon.py）

**Files:**
- Modify: `python/godot_cli_control/daemon.py:52-58`（`validate_instance_name`）、`daemon.py:494-520`（`list_live_instances` 过滤）
- Test: `python/tests/test_daemon.py`（追加）、`python/tests/test_broadcast.py`（新建）

- [ ] **Step 1: 写失败测试（daemon 层）**

追加到 `python/tests/test_daemon.py` 末尾：

```python
# ── #145 广播保留名 all ──


def test_validate_instance_name_rejects_reserved_all() -> None:
    """'all' 是广播保留名（#145）：validate_instance_name 必须拒绝并解释原因。"""
    from godot_cli_control.daemon import DaemonError, validate_instance_name

    with pytest.raises(DaemonError, match="广播保留名"):
        validate_instance_name("all")


def test_list_live_instances_skips_stale_all_dir(tmp_path: Path) -> None:
    """存量 instances/all/ 目录（老版本可能创建）必须静默跳过，
    不能让 Daemon() 构造时的保留名校验抛 DaemonError 炸掉枚举。"""
    from godot_cli_control.daemon import list_live_instances

    base = tmp_path / ".cli_control" / "instances"
    (base / "all").mkdir(parents=True)
    (base / "all" / "godot.pid").write_text("999999999", encoding="utf-8")
    assert list_live_instances(tmp_path) == []
```

（若文件顶部没有 `from pathlib import Path` / `import pytest`，按既有 import 区补。）

新建 `python/tests/test_broadcast.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest python/tests/test_daemon.py -k "reserved_all or stale_all" -q` 与
`.venv/bin/python -m pytest python/tests/test_broadcast.py -q`

Expected: `test_validate_instance_name_rejects_reserved_all` FAIL（DaemonError 未抛）；
`test_list_live_instances_skips_stale_all_dir` 可能 PASS（pid 999999999 不存活）——
没关系，它钉的是「实现保留名校验后不炸」，留着防回归；test_broadcast 两条 FAIL
（message 无"广播保留名"）。

- [ ] **Step 3: 实现**

`python/godot_cli_control/daemon.py` 的 `validate_instance_name`（line 52）改为：

```python
def validate_instance_name(name: str) -> str:
    """校验实例名合法性，合法则原样返回，不合法抛 DaemonError（spec 2026-06-07）。"""
    if name == "all":
        # #145：'all' 是广播保留名——顶层 `--instance all` 对全部活实例广播 RPC。
        # 在创建链路的咽喉（Daemon 构造也走这里）拒绝，CLI 与 pytest 工厂同享约束。
        raise DaemonError(
            "'all' 是广播保留名（顶层 --instance all 对全部活实例广播 RPC；"
            "停全部实例用 daemon stop --all），不能用作实例名"
        )
    if not _INSTANCE_NAME_RE.fullmatch(name):
        raise DaemonError(
            f"非法 instance 名 {name!r}：只允许 [A-Za-z0-9_-]，长度 1-32"
        )
    return name
```

`list_live_instances` 的 return 生成式加一行过滤（在 `p.is_dir()` 之后）：

```python
    return sorted(
        p.name
        for p in base.iterdir()
        if p.is_dir()
        and p.name != "all"  # #145 广播保留名：存量 all 目录静默跳过，防 Daemon() 构造抛错
        and _INSTANCE_NAME_RE.fullmatch(p.name)  # 过滤非法目录名，避免 DaemonError 污染
        and Daemon(root, instance=p.name).is_running()
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest python/tests/test_daemon.py python/tests/test_broadcast.py -q`
Expected: 全 PASS。再跑 `.venv/bin/python -m pytest python/tests/test_cli.py -q` 确认无既有用例因保留名回归（不应有——既有用例没用 "all" 当实例名）。

- [ ] **Step 5: Commit**

```bash
git add python/godot_cli_control/daemon.py python/tests/test_daemon.py python/tests/test_broadcast.py
git commit -m "feat(daemon): 'all' 成为广播保留实例名（#145）"
```

---

### Task 2: 顶层 `--instance all` 放行 + daemon/run 路径拒绝（cli.py）

**Files:**
- Modify: `python/godot_cli_control/cli.py:2037-2068`（校验器区）、`cli.py:2270-2278`（顶层 `--instance`）、`cli.py:2071-2103`（`_resolve_daemon_instance`）、`cli.py:1273-1285`（`cmd_daemon_start`）
- Test: `python/tests/test_broadcast.py`

- [ ] **Step 1: 写失败测试**

追加到 `python/tests/test_broadcast.py`：

```python
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
    本条当下就应绿（cli.py cmd_daemon_stop 行 1350 已覆盖），作回归钉。"""
    from godot_cli_control.cli import EXIT_USAGE, OUTPUT_JSON, cmd_daemon_stop

    ns = argparse.Namespace(
        all=True, name=None, instance="all", project=None, output_format=OUTPUT_JSON
    )
    rc = cmd_daemon_stop(ns)
    assert rc == EXIT_USAGE
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == -1003
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest python/tests/test_broadcast.py -q`
Expected: `test_top_level_instance_accepts_all` FAIL（Task 1 后 `--instance all` 被
`validate_instance_name` 拒绝、SystemExit 64）；resolve/start/run 三条 FAIL；
`test_stop_all_with_top_level_instance_all_is_usage_error` PASS（回归钉）。

- [ ] **Step 3: 实现**

`python/godot_cli_control/cli.py`，在 `_instance_name_arg`（line 2037）之后加：

```python
def _instance_arg_allow_all(value: str) -> str:
    """顶层 ``--instance`` 的 argparse type：放行广播哨兵 ``all``（#145），
    其余走常规实例名校验。``--name`` 不放行——'all' 不可用作实例名。"""
    if value == "all":
        return value
    return _instance_name_arg(value)
```

在 `_merge_instance_flags`（line 2058）上方加模块级常量：

```python
# #145：--instance all 仅对 RPC 子命令广播；daemon / run 是单靶或已有 --all 语义。
_BROADCAST_NOT_FOR_DAEMON_MSG = (
    "--instance all 仅对 RPC 子命令广播；daemon/run 子命令请用 --name <inst> "
    "指定单实例（停全部实例用 daemon stop --all）"
)
```

顶层 `--instance`（line 2270-2278）的 `type` 换成 `_instance_arg_allow_all`，help 追加广播说明：

```python
    conn_grp.add_argument(
        "--instance",
        type=_instance_arg_allow_all,
        default=None,
        help=(
            "目标实例名；RPC 与 run/daemon 子命令通用（daemon 子命令的 --name 是等价写法）。"
            "多实例并行时必传；与 --port 互斥。传 all 对全部活实例广播（仅 RPC 子命令）。"
        ),
    )
```

`_resolve_daemon_instance`（line 2080 起），在 conflict 块之后、`if inst is not None` 之前插入：

```python
    if inst == "all":
        # #145：广播哨兵只在 RPC 路径有意义；daemon/run 单靶路径明确拒绝并指路。
        _emit_top_error(ns, code=CLIENT_CODE_USAGE, message=_BROADCAST_NOT_FOR_DAEMON_MSG)
        return None
```

`cmd_daemon_start`（line 1284 `inst = merged or "default"` 之后）插入：

```python
    if inst == "all":
        # #145：start 不经 _resolve_daemon_instance，需独立守卫顶层 --instance all。
        _emit_top_error(ns, code=CLIENT_CODE_USAGE, message=_BROADCAST_NOT_FOR_DAEMON_MSG)
        return EXIT_USAGE
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest python/tests/test_broadcast.py python/tests/test_cli.py -q`
Expected: 全 PASS（test_cli.py 钉住既有 --instance/--name 行为不回归）。

- [ ] **Step 5: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_broadcast.py
git commit -m "feat(cli): 顶层 --instance 放行广播哨兵 all，daemon/run 路径拒绝（#145）"
```

---

### Task 3: 提取 `_rpc_failure_envelope`（行为不变的重构）

**Files:**
- Modify: `python/godot_cli_control/cli.py:2577-2633`（`_run_rpc` except 链）
- Test: `python/tests/test_broadcast.py`（锁映射契约）；既有 `test_cli.py::test_run_rpc_*` 是安全网

- [ ] **Step 1: 写失败测试**

追加到 `python/tests/test_broadcast.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest python/tests/test_broadcast.py -k failure_envelope -q`
Expected: FAIL（ImportError：`_rpc_failure_envelope` 不存在）。

- [ ] **Step 3: 实现**

`python/godot_cli_control/cli.py`，在 `_emit_envelope_error`（line 2569）之后、`_run_rpc` 之前加：

```python
def _rpc_failure_envelope(e: Exception) -> tuple[int, str, int]:
    """异常 → ``(client code, message, exit code)``，_run_rpc 与广播路径共用。

    isinstance 判定顺序必须与原 except 链一致：ConnectionError 与（3.11+ 的）
    TimeoutError 都是 OSError 子类，先窄后宽，否则错码漂移。

    各分支语义（原样搬运自 _run_rpc，历史注释见 git blame）：
    * RpcError → 服务端业务/协议错，exit 1。
    * ConnectionError → -1001，exit 2。
    * asyncio.TimeoutError → -1002，exit 2。
    * OSError → socket 类算 connection（-1001）；文件 IO 类（screenshot 写盘
      失败常见）走 -1004，不让 agent 误以为 daemon 挂了。exit 2。
    * ValueError / JSONDecodeError → 用法错 -1003，恒 exit 64（#82）。
    * 其余 Exception → 客户端内部 bug：traceback 留给 stderr 帮人 debug，
      stdout 信封只带异常类名（-1099），exit 2。
    """
    if isinstance(e, RpcError):
        return e.code, e.message, EXIT_RPC_ERROR
    if isinstance(e, ConnectionError):
        return CLIENT_CODE_CONNECTION, str(e) or e.__class__.__name__, EXIT_INFRA_ERROR
    if isinstance(e, asyncio.TimeoutError):
        return CLIENT_CODE_TIMEOUT, str(e) or "timed out", EXIT_INFRA_ERROR
    if isinstance(e, OSError):
        code = CLIENT_CODE_CONNECTION if _is_network_oserror(e) else CLIENT_CODE_IO
        return code, str(e) or e.__class__.__name__, EXIT_INFRA_ERROR
    if isinstance(e, (ValueError, json.JSONDecodeError)):
        return CLIENT_CODE_USAGE, str(e), EXIT_USAGE
    traceback.print_exc(file=sys.stderr)
    return CLIENT_CODE_INTERNAL, f"{type(e).__name__}: {e}", EXIT_INFRA_ERROR
```

`_run_rpc` 的整条 except 链（line 2590-2628）替换为单分支（docstring 保留不动）：

```python
    try:
        async with GameClient(port=port) as client:
            result = await spec.handler(client, ns)
    except Exception as e:  # noqa: BLE001 — 全部收口成信封（契约 1）；KeyboardInterrupt/SystemExit 照常传播
        code, msg, rc = _rpc_failure_envelope(e)
        _emit_envelope_error(fmt, code, msg)
        return rc

    _emit_rpc_result(spec, fmt, result)
    if spec.exit_code_from is not None:
        return spec.exit_code_from(result)
    return EXIT_OK
```

- [ ] **Step 4: 跑测试确认通过（重构安全网）**

Run: `.venv/bin/python -m pytest python/tests/test_broadcast.py python/tests/test_cli.py -q`
Expected: 全 PASS——`test_cli.py` 里 `test_run_rpc_emits_*` 一族逐分支钉住了原行为，
有任何映射漂移会当场红。

- [ ] **Step 5: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_broadcast.py
git commit -m "refactor(cli): 提取 _rpc_failure_envelope 供广播路径复用（#145，行为不变）"
```

---

### Task 4: `{instance}` 占位符替换 + screenshot preflight 守卫

**Files:**
- Modify: `python/godot_cli_control/cli.py`（`_preflight_combo` 附近加 preflight；`_emit_rpc_result` 附近加替换工具；screenshot `RpcSpec` line 839-852 加 `preflight=`）
- Test: `python/tests/test_broadcast.py`

- [ ] **Step 1: 写失败测试**

追加到 `python/tests/test_broadcast.py`：

```python
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
    with pytest.raises(ValueError, match="{instance}"):
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
```

注意 `pytest.raises(..., match=...)` 的 pattern 是正则：`match="{instance}"` 里花括号
是字面量、可直接匹配（`{` 在 re 中非特殊字符起始时按字面处理；若 pytest 版本报
re 警告就改成 `match=r"\{instance\}"`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest python/tests/test_broadcast.py -k "substitut or screenshot" -q`
Expected: 前两条 ImportError FAIL；preflight 两条 FAIL（screenshot spec 的 preflight 是 None / main 没拦）。

- [ ] **Step 3: 实现**

`python/godot_cli_control/cli.py`，在 `_preflight_combo`（line 203-208）之后加：

```python
def _preflight_screenshot(ns: argparse.Namespace) -> None:
    """广播（--instance all）下 output_path 必须带 ``{instance}`` 占位符（#145）。

    多实例写同一路径会互相覆盖且不报错——典型静默坑，preflight 拦在连接前
    （契约 #5）。非广播模式零约束。
    """
    if getattr(ns, "instance", None) == "all" and "{instance}" not in ns.output_path:
        raise ValueError(
            "broadcast screenshot：output_path 必须包含 {instance} 占位符"
            "（如 shot-{instance}.png），否则各实例写同一文件互相覆盖"
        )
```

在 `_emit_rpc_result`（line 1966）之后加替换工具（与广播路径同区，靠近消费方）：

```python
def _instance_substituted(value: Any, instance: str) -> Any:
    """递归把字符串里的 ``{instance}`` 换成实例名（str/list/dict；其余原样）。

    通用机制：screenshot 路径、set/call 的 JSON 字面量、combo 缓存 steps 都吃
    同一规则——agent 只须记一条。无转义口子（YAGNI），SKILL.md pitfall 注明。
    """
    if isinstance(value, str):
        return value.replace("{instance}", instance)
    if isinstance(value, list):
        return [_instance_substituted(v, instance) for v in value]
    if isinstance(value, dict):
        return {k: _instance_substituted(v, instance) for k, v in value.items()}
    return value


def _namespace_for_instance(ns: argparse.Namespace, instance: str) -> argparse.Namespace:
    """广播：拷贝 namespace 并做 {instance} 替换，原 ns 不动（#145）。"""
    return argparse.Namespace(
        **{k: _instance_substituted(v, instance) for k, v in vars(ns).items()}
    )
```

screenshot `RpcSpec`（line 839-852）加一行 `preflight=_preflight_screenshot,`：

```python
    RpcSpec(
        name="screenshot",
        handler=cmd_screenshot,
        description=(
            "截屏并写 PNG 文件。**路径必填**（旧版本可省、把 base64 喷到 "
            "stdout —— 已删，避免撑爆 AI 上下文）。"
        ),
        positionals=(
            Positional("output_path", None, "PNG 输出路径（必填）"),
        ),
        example="screenshot out.png --node /root/Game/Player/Sprite",
        extra_args=_register_screenshot_args,
        text_formatter=_fmt_screenshot_text,
        preflight=_preflight_screenshot,
    ),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest python/tests/test_broadcast.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_broadcast.py
git commit -m "feat(cli): {instance} 占位符替换 + screenshot 广播 preflight 守卫（#145）"
```

---

### Task 5: `_run_rpc_broadcast` 聚合核心 + `main()` 接线

**Files:**
- Modify: `python/godot_cli_control/cli.py`（`_run_rpc` 之后加 `_run_rpc_broadcast`；`main()` line 2674 前分流；line 50 `EXIT_PARTIAL` 注释）
- Test: `python/tests/test_broadcast.py`

- [ ] **Step 1: 写失败测试**

追加到 `python/tests/test_broadcast.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest python/tests/test_broadcast.py -q`
Expected: 新增用例全 FAIL（ImportError：`_run_rpc_broadcast` 不存在）；旧用例 PASS。

- [ ] **Step 3: 实现**

`python/godot_cli_control/cli.py`：

line 50 注释更新（语义扩展）：

```python
EXIT_PARTIAL = 3  # 聚合操作（daemon stop --all / --instance all 广播）部分或全部目标失败
```

在 `_run_rpc`（line 2633 之后）加：

```python
async def _run_rpc_broadcast(
    spec: RpcSpec, ns: argparse.Namespace, fmt: str
) -> int:
    """``--instance all``（#145）：对 cwd 项目全部活实例并发执行同一 RPC，聚合信封。

    * 目标 = ``list_live_instances(cwd)``（与 ``daemon stop --all --project`` 同一
      枚举路径）；0 个活实例 → -1006 + exit 2。legacy 平铺 daemon 不在目标内
      （``instances/`` 不存在时枚举为空），与 default 实例的 legacy 探活语义一致。
    * 逐实例 entry 复刻单命令信封（``ok`` + ``result``|``error``）+ ``instance`` +
      ``rc``——agent 复用同一套解析。``rc`` 按原命令语义算（exit_code_from /
      RPC 错=1 / 连接错=2）。数组按实例名排序。
    * 聚合退出码：全 0 → 0；任一非 0 → EXIT_PARTIAL(3)。顶层 ``ok`` 恒 true
    （广播本身执行了就算 ok，沿 stop --all 先例），失败细节在 entry。
    * asyncio.gather 并发：「同时给 4 个 client 截图」≈ 同一时刻；wait-* 总耗时
      = 最慢实例而非求和。单实例异常逐个捕获，不拖死其他。
    """
    from .daemon import Daemon, list_live_instances

    root = Path.cwd()
    names = list_live_instances(root)
    if not names:
        _emit_envelope_error(
            fmt,
            CLIENT_CODE_PRECONDITION,
            "no live instances to broadcast —— --instance all 只对 "
            ".cli_control/instances/ 下的活实例生效（legacy 平铺 daemon 不算）；"
            "先 daemon start --name <inst>",
        )
        return EXIT_INFRA_ERROR

    async def _one(name: str) -> dict[str, Any]:
        inst_ns = _namespace_for_instance(ns, name)
        try:
            port = Daemon(root, instance=name).current_port()
            if port is None:
                # 探活与读端口之间实例死了 / 启动中：瞬态，按连接错处理。
                raise ConnectionError(f"instance {name!r}: port file not readable")
            async with GameClient(port=port) as client:
                result = await spec.handler(client, inst_ns)
        except Exception as e:  # noqa: BLE001 — 单实例失败落 entry，不拖死其他
            code, msg, rc = _rpc_failure_envelope(e)
            return {
                "instance": name,
                "ok": False,
                "error": {"code": code, "message": msg},
                "rc": rc,
            }
        rc = spec.exit_code_from(result) if spec.exit_code_from is not None else EXIT_OK
        return {"instance": name, "ok": True, "result": result, "rc": rc}

    entries = list(await asyncio.gather(*(_one(n) for n in names)))
    # list_live_instances 已排序、gather 保序；显式再排一次把输出契约钉死。
    entries.sort(key=lambda e: e["instance"])
    agg_rc = EXIT_OK if all(e["rc"] == EXIT_OK for e in entries) else EXIT_PARTIAL
    if fmt == OUTPUT_JSON:
        _emit_success_payload({"instances": entries, "rc": agg_rc})
    else:
        for e in entries:
            if e["ok"]:
                print(f"[{e['instance']}] {spec.text_formatter(e['result'])}")
            else:
                print(
                    f"[{e['instance']}] error: [{e['error']['code']}] "
                    f"{e['error']['message']}",
                    file=sys.stderr,
                )
    return agg_rc
```

`main()`（preflight 块之后、`port = ns.port` 之前，即 line 2674 前）插入：

```python
        # --instance all：广播路径（#145）——逐实例并发 + 聚合信封，
        # 不走单实例端口发现（preflight 已在上面跑过，screenshot 守卫等生效）。
        if ns.instance == "all":
            sys.exit(asyncio.run(_run_rpc_broadcast(spec, ns, fmt)))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest python/tests/test_broadcast.py python/tests/test_cli.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_broadcast.py
git commit -m "feat(cli): --instance all 广播多实例——并发执行 + 聚合信封 + rc 0/3（#145）"
```

---

### Task 6: e2e——真 Godot 双实例广播

**Files:**
- Modify: `python/tests/test_e2e_multi_instance.py`（文件末尾追加；复用 module 级 `multi_project` fixture 与 `_safe_stop`）

- [ ] **Step 1: 写测试**

追加到 `python/tests/test_e2e_multi_instance.py` 末尾：

```python
# ────────────────────────────────────────────────────────────────────────────
# Test 3：--instance all 广播（#145）——CLI 子进程全链路
# ────────────────────────────────────────────────────────────────────────────

def _cli(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """以 project 为 cwd 跑 CLI 子进程（广播按 cwd 枚举实例）。"""
    return subprocess.run(
        [sys.executable, "-m", "godot_cli_control.cli", *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_broadcast_exists_and_per_instance_values(multi_project: Path) -> None:
    """--instance all 全链路：双实例广播 exists rc=0；先各自 set 不同值再广播
    get，entry 值互不串台——证明 fan-out 真打到两个独立 daemon。"""
    project = multi_project
    a = Daemon(project, instance="server")
    b = Daemon(project, instance="client1")
    try:
        a.start(headless=True)
        b.start(headless=True)

        # 1. 广播 exists：双实例命中，聚合 rc=0、退出码 0，数组按名排序
        r = _cli(project, "--instance", "all", "exists", "/root/Main")
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is True
        insts = payload["result"]["instances"]
        assert [e["instance"] for e in insts] == ["client1", "server"]
        assert all(e["ok"] is True and e["result"] is True for e in insts)
        assert payload["result"]["rc"] == 0

        # 2. 各自 set 不同值（单靶路径），广播 get 读回互不串台
        assert _cli(project, "--instance", "server", "set",
                    "/root/Main", "value", "111").returncode == 0
        assert _cli(project, "--instance", "client1", "set",
                    "/root/Main", "value", "222").returncode == 0
        r = _cli(project, "--instance", "all", "get", "/root/Main", "value")
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        by_name = {
            e["instance"]: e for e in json.loads(r.stdout)["result"]["instances"]
        }
        # get 单属性的 result 形状是 {"value": ..., "type"?}（cli.py cmd_get docstring）
        assert by_name["server"]["result"]["value"] == 111
        assert by_name["client1"]["result"]["value"] == 222

        # 3. 广播缺失节点：两实例 result=false → 各 entry rc=1，聚合退出码 3
        r = _cli(project, "--instance", "all", "exists", "/root/Nope")
        assert r.returncode == 3, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        payload = json.loads(r.stdout)
        assert payload["result"]["rc"] == 3
        assert all(e["rc"] == 1 for e in payload["result"]["instances"])
    finally:
        _safe_stop(a, "server")
        _safe_stop(b, "client1")
```

文件顶部 import 区若缺 `import json`，补上。
（参数形状已核对：`get <node_path> <props...>`（cli.py:988-991）、
`set <node_path> <prop> <value>`（`_register_set_args`），无需调整。）

- [ ] **Step 2: 真跑 e2e**

Run: `GODOT_BIN=$(command -v godot || echo ~/.local/bin/godot) .venv/bin/python -m pytest python/tests/test_e2e_multi_instance.py -q`
Expected: 全 PASS（含既有两条；新条目首跑可能因 import 缓存慢，timeout 已留 120s）。
**本机必须真跑**，不许以缺 GODOT_BIN 为由跳过。

- [ ] **Step 3: Commit**

```bash
git add python/tests/test_e2e_multi_instance.py
git commit -m "test(e2e): --instance all 广播双实例全链路（#145）"
```

---

### Task 7: 文档同步（SKILL.md 模板 + 重渲染 + CLAUDE.md + CHANGELOG）

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（退出码表 line 55；multi-instance 节 line 92-144 末尾加 broadcast 小节）
- Modify: `.claude/skills/godot-cli-control/SKILL.md`（重渲染产物，勿手改）
- Modify: `CLAUDE.md`（退出码 3 措辞）
- Modify: `addons/godot_cli_control/CHANGELOG.md`（`[Unreleased]` → `### Added`）

- [ ] **Step 1: 改 SKILL.md 模板**

退出码表 `| 3 |` 行（line 55）替换为：

```markdown
| 3 | Aggregate partial/total failure: `daemon stop --all` (at least one daemon failed to stop) or an `--instance all` broadcast where at least one instance's per-instance `rc` was non-zero (RPC error, connection error, or a semantic false like `exists`). Per-target `rc` is in the JSON `result.stopped[]` / `result.instances[]`. |
```

multi-instance 节末尾（line 144 `**In pytest suites**...` 之前）插入：

```markdown
**Broadcasting one command to all instances (`--instance all`):**

```bash
godot-cli-control --instance all exists /root/Main          # assert on every instance
godot-cli-control --instance all screenshot /tmp/shot-{instance}.png
godot-cli-control --instance all get /root/Player position
```

- Targets every live instance of the cwd project, **concurrently** (asyncio); the result array is sorted by instance name.
- Envelope shape (top-level `ok` stays `true` — per-instance failures live in the entries, mirroring `daemon stop --all`):

```json
{"ok": true, "result": {"instances": [
  {"instance": "client1", "ok": true, "result": true, "rc": 0},
  {"instance": "server", "ok": false, "error": {"code": 1002, "message": "..."}, "rc": 1}
], "rc": 3}}
```

- Exit code: **0** if every instance's `rc` is 0, else **3**. So `if godot-cli-control --instance all exists /root/Foo; then …` means "exists on *all* instances".
- Every string argument has `{instance}` replaced with the instance name per target — required for `screenshot` (a path without `{instance}` is rejected pre-flight with `-1003` / exit 64, because all instances would overwrite the same file). The substitution applies to *all* string args (including `set`/`call` JSON values), with no escape hatch.
- `all` is a **reserved instance name**: `daemon start --name all` is rejected.
- Broadcast applies to RPC subcommands only: `--instance all` with `run` or `daemon` subcommands → `-1003` / exit 64 (to stop everything use `daemon stop --all`).
- 0 live instances → `-1006` / exit 2 (legacy flat-layout daemons are not broadcast targets — restart them as named instances).
```

- [ ] **Step 2: 改 CLAUDE.md 退出码 3 行**

`CLAUDE.md` 「退出码语义化」一节，`3 = daemon stop --all 部分失败（专用，避免与 2 撞）` 改为：

```markdown
   - 3 = 聚合操作部分/全部失败：`daemon stop --all` 至少一个目标失败，或 `--instance all` 广播至少一个实例 rc≠0（专用，避免与 2 撞）
```

- [ ] **Step 3: 记 CHANGELOG**

`addons/godot_cli_control/CHANGELOG.md` 的 `## [Unreleased]` → `### Added` 段追加：

```markdown
- **`--instance all` 一条命令广播全部活实例**（#145）：任一 RPC 子命令对 cwd 项目全部活实例并发执行，聚合信封 `{"instances":[{instance, ok, result|error, rc}...], "rc": 0|3}`（entry 复刻单命令信封）；退出码全 0→0、任一非 0→3（沿 `daemon stop --all` 先例）。字符串参数中的 `{instance}` 逐实例替换；广播 `screenshot` 的路径缺 `{instance}` 时 preflight 报 -1003/64。`all` 成为保留实例名（`daemon start --name all` 拒绝）；`run`/`daemon` 子命令不支持广播。
```

- [ ] **Step 4: 重渲染仓内 SKILL.md 副本（必须 Python 3.12 + COLUMNS=80）**

```bash
/usr/local/bin/python3.12 -m venv /tmp/gcc-render-venv
/tmp/gcc-render-venv/bin/pip -q install -e /Users/kesar/Projects/godot-cli-control
cd /Users/kesar/Projects/godot-cli-control && COLUMNS=80 /tmp/gcc-render-venv/bin/python -c "from godot_cli_control import cli; from godot_cli_control.skills_install import render_skill; from godot_cli_control._version import version; open('.claude/skills/godot-cli-control/SKILL.md','w').write(render_skill(version, cli.format_full_help()))"
```

然后 `git diff .claude/skills/` 肉眼确认 diff 只含本 feature 内容（broadcast 小节、
退出码表、`--instance` help 文案），没有无关折行漂移。

- [ ] **Step 5: 验证渲染与 init 注入**

Run: `.venv/bin/python -c "from godot_cli_control import cli; print(cli.format_full_help())" >/dev/null && .venv/bin/python -m pytest python/tests/test_skills_install.py -q`
Expected: help 渲染不崩；test_skills_install 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add python/godot_cli_control/templates/skill/SKILL.md .claude/skills/godot-cli-control/SKILL.md CLAUDE.md addons/godot_cli_control/CHANGELOG.md
git commit -m "docs: SKILL.md/CLAUDE.md/CHANGELOG 同步 --instance all 广播（#145）"
```

---

### Task 8: 全量验证 + PR

- [ ] **Step 1: 全量套件 + 覆盖率（派 subagent，model sonnet，禁 run_in_background）**

子代理执行（在仓库根）：

```bash
cd /Users/kesar/Projects/godot-cli-control && .venv/bin/coverage run -m pytest python/tests -q && .venv/bin/coverage report --fail-under=80
```

注意 e2e 会真启 Godot（PATH 有 godot），耗时数分钟属正常。只回传：通过/失败计数、
覆盖率百分比、失败用例名与一行原因。

- [ ] **Step 2: lint（本地 venv 没装 ruff，临时跑）**

```bash
cd /Users/kesar/Projects/godot-cli-control && /tmp/gcc-render-venv/bin/pip -q install ruff && /tmp/gcc-render-venv/bin/ruff check python/
```

Expected: no findings；有就修掉再跑一遍（pytest 绿 ≠ lint 干净）。

- [ ] **Step 3: GUT（GDScript 零改动，跳过有据）**

本 feature 未触碰 `addons/**/*.gd`（`git diff main --stat -- addons | grep -v CHANGELOG` 应为空），GUT 不必跑；CI 仍会全量跑。若 diff 非空则必须本地跑 `GODOT_BIN=$(command -v godot) python addons/godot_cli_control/tests/run_gut.py`。

- [ ] **Step 4: 开 PR（注意 gh 需绕本地代理）**

```bash
cd /Users/kesar/Projects/godot-cli-control && unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY && git push -u origin feat/145-broadcast-instance-all && gh pr create --title "feat: --instance all 一条命令广播多实例（#145）" --body "$(cat <<'EOF'
## Summary
- `--instance all` 让任一 RPC 子命令对 cwd 项目全部活实例并发执行并聚合输出（Closes #145）
- 逐实例 entry 复刻单命令信封 + instance + rc；聚合退出码全 0→0、任一非 0→3（沿 `daemon stop --all` 先例）
- `{instance}` 占位符逐实例替换；广播 `screenshot` 缺占位符 preflight 报 -1003/64
- `all` 成为保留实例名；`run`/`daemon` 子命令不支持广播（指路 `daemon stop --all`）
- 纯 Python 客户端编排，GDScript 零改动；SKILL.md / CLAUDE.md / CHANGELOG 已同步

Spec: docs/superpowers/specs/2026-06-07-broadcast-instance-all-design.md

## Test plan
- [x] 单测：保留名 / 占位符 / preflight / 聚合 rc 矩阵 / main 接线（test_broadcast.py）
- [x] e2e：真 Godot 双实例广播 exists/get + 退出码 3（test_e2e_multi_instance.py）
- [x] coverage ≥80% / ruff clean / skill-render-drift（py3.12 重渲染）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: auto-merge + 自检**

```bash
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY && gh pr merge --auto --squash && gh pr view --json autoMergeRequest --jq '.autoMergeRequest != null'
```

Expected: 输出 `true`（autoMergeRequest 非 null 才算挂上；main required check 锚定 ci-ok 聚合 job）。

---

## Self-Review 已核对

- **Spec 覆盖**：表面形式+保留名→T1/T2；选靶 0/1/N→T5；信封/退出码/text→T5；并发→T5（gather）；`{instance}`+screenshot 守卫→T4；run/daemon 拒绝→T2；e2e→T6；文档三件套→T7。无遗漏。
- **类型一致**：`_rpc_failure_envelope(e) -> (int, str, int)` 在 T3 定义、T5 消费；`_namespace_for_instance(ns, str) -> Namespace` 在 T4 定义、T5 消费；`_run_rpc_broadcast(spec, ns, fmt) -> int` 在 T5 定义并接线。
- **占位符/无 TBD**：每步含完整代码与命令。
