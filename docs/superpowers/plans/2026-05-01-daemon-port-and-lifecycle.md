# Daemon Port & Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 daemon 的端口选号烦恼与孤儿守护进程 — 默认端口由 OS 自动分配；新增全局注册表与 `daemon ls` / `daemon stop --all` / `daemon stop --project`；可选 `--idle-timeout` 让 Godot 端空闲自动 shutdown。

**Architecture:** 三组互相独立的小改动。A 改 daemon 内部端口分配函数与 CLI 默认值；B 新增 `registry.py` 模块 + 跨项目子命令；C 在 GDScript 端加 Timer + Python 端加 duration 解析。各组都可独立 commit + 上线，互不阻塞。

**Tech Stack:** Python 3.10+ 标准库（`socket` / `pathlib` / `json` / `hashlib`）+ pytest；GDScript（Godot 4 Timer）。无新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-05-01-daemon-port-and-lifecycle-design.md`

**File Structure（新增 / 修改）：**

| 文件 | 责任 |
|---|---|
| `python/godot_cli_control/daemon.py` | 新增 `_allocate_port`；`start` 接受 `port=0` / `idle_timeout`；`start` / `stop` 调注册表 |
| `python/godot_cli_control/registry.py` | **新文件** — 全局守护进程注册表读写 + 死记录探活清理 |
| `python/godot_cli_control/cli.py` | `--port` 默认 0；新增 `daemon ls` / `daemon stop --all/--project` / `--idle-timeout` |
| `python/godot_cli_control/_duration.py` | **新文件** — `parse_duration("30m") -> 1800`；纯函数 + 单测 |
| `addons/godot_cli_control/bridge/game_bridge.gd` | 解析 `--game-bridge-idle-timeout`，加 Timer + `_last_activity_ms` 更新点 |
| `python/tests/test_daemon.py` | 追加 `_allocate_port` 测试 |
| `python/tests/test_registry.py` | **新文件** — registry 单测 |
| `python/tests/test_duration.py` | **新文件** — duration parser 单测 |
| `python/tests/test_cli.py` | 追加 `daemon ls` / `--all` / `--project` / `--idle-timeout` 测试 |
| `python/README.md` 与 `addons/godot_cli_control/SKILL.md` 与 init 落盘 SKILL 模板 | 文档同步默认端口变更 |

---

## Task 1: `_allocate_port` 函数（替代 `_check_port_available`）

**Files:**
- Modify: `python/godot_cli_control/daemon.py:399-416`
- Test: `python/tests/test_daemon.py`

- [ ] **Step 1.1: 写失败测试 — port=0 应返回 OS 分配的高位端口**

追加到 `python/tests/test_daemon.py`：

```python
from godot_cli_control.daemon import _allocate_port

def test_allocate_port_zero_returns_os_assigned() -> None:
    port = _allocate_port(0)
    assert 1024 < port < 65536

def test_allocate_port_specific_returns_same() -> None:
    # 用 OS 分配一个端口然后立即放掉，再要这个端口应该能拿到
    free = _allocate_port(0)
    assert _allocate_port(free) == free

def test_allocate_port_raises_when_occupied() -> None:
    import socket as _s
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    occupied = sock.getsockname()[1]
    try:
        with pytest.raises(DaemonError, match="already in use"):
            _allocate_port(occupied)
    finally:
        sock.close()
```

- [ ] **Step 1.2: 跑测试，确认 `_allocate_port` 不存在导致 ImportError**

```bash
cd /Users/kesar/Projects/godot-cli-control && uv run pytest python/tests/test_daemon.py::test_allocate_port_zero_returns_os_assigned -v
```
Expected: `ImportError: cannot import name '_allocate_port'`

- [ ] **Step 1.3: 实现 `_allocate_port`，删除 `_check_port_available`**

替换 `python/godot_cli_control/daemon.py:399-416`：

```python
def _allocate_port(requested: int) -> int:
    """返回可用端口。requested=0 → OS 分配；requested>0 → 校验未被占用后返回原值。

    bind 后立刻关闭与 Godot 子进程实际 listen 之间存在极小 race window，但内核
    短期内不会立即重用同一端口；真撞上时 Godot listen 失败仍会通过日志被发现。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            sock.bind(("127.0.0.1", requested))
        except OSError as e:
            raise DaemonError(
                f"port {requested} already in use ({e}). 另一个进程正在占用该端口，"
                f"Godot 起不来。请 stop 旧 daemon 或换 --port。"
            ) from e
        return sock.getsockname()[1]
    finally:
        sock.close()
```

- [ ] **Step 1.4: 跑测试确认通过**

```bash
uv run pytest python/tests/test_daemon.py -v -k allocate_port
```
Expected: 3 passed

- [ ] **Step 1.5: Commit**

```bash
git add python/godot_cli_control/daemon.py python/tests/test_daemon.py
git commit -m "feat(daemon): _allocate_port 支持 port=0 自动分配"
```

---

## Task 2: 串接 `_allocate_port` 到 `Daemon.start`

**Files:**
- Modify: `python/godot_cli_control/daemon.py:78,112,132,139` (start 方法)

- [ ] **Step 2.1: 写失败测试 — start 用 port=0 时 port_file 写入实际分配的端口**

追加到 `test_daemon.py`，照 `test_start_rejects_*` 类似的 mock 写法：

```python
def test_start_with_port_zero_writes_actual_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """port=0 时 .cli_control/port 落盘的应是 OS 分配的实际端口，不是 0。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "godot"
    fake_bin.write_text("#!/bin/sh\nsleep 60\n")
    fake_bin.chmod(0o755)

    monkeypatch.setattr("godot_cli_control.daemon._ensure_imported", lambda *a, **k: None)
    monkeypatch.setattr("godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True)

    daemon = Daemon(tmp_path)
    daemon.start(godot_bin=str(fake_bin), port=0)
    try:
        written = int(daemon.port_file.read_text().strip())
        assert written != 0
        assert 1024 < written < 65536
    finally:
        daemon.stop()
```

- [ ] **Step 2.2: 跑测试确认失败（默认 0 还没改，仍写 0 进去）**

```bash
uv run pytest python/tests/test_daemon.py::test_start_with_port_zero_writes_actual_port -v
```
Expected: FAIL（assert 0 != 0）

- [ ] **Step 2.3: 改 `Daemon.start` 串接 `_allocate_port`**

修改 `python/godot_cli_control/daemon.py:78` 默认值：

```python
        port: int = 0,  # 0 = OS 自动分配；显式 --port 9877 仍可固定
```

修改 `daemon.py:112` 调用：

```python
        actual_port = _allocate_port(port)
```

修改 `daemon.py:132` 写文件：

```python
        self.port_file.write_text(str(actual_port))
```

修改 `daemon.py:139` 传给 Godot：

```python
            f"--game-bridge-port={actual_port}",
```

并把后续 `_wait_port_ready(port, ...)` 与日志 `f"...port {port}..."` 全部改用 `actual_port`。

- [ ] **Step 2.4: 跑测试**

```bash
uv run pytest python/tests/test_daemon.py -v
```
Expected: 全部 pass（含旧测试，因为 `port=0` 是新默认）

- [ ] **Step 2.5: 改 CLI 默认端口**

修改 `python/godot_cli_control/cli.py:952-957`：

```python
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="GameBridge 监听端口（默认 0 = OS 自动分配；写入 .cli_control/port）",
    )
```

- [ ] **Step 2.6: 跑全套测试**

```bash
uv run pytest python/tests/ -v
```
Expected: 全部 pass

- [ ] **Step 2.7: Commit**

```bash
git add python/godot_cli_control/daemon.py python/godot_cli_control/cli.py python/tests/test_daemon.py
git commit -m "feat(daemon): 默认 port=0 由 OS 分配，避免多项目并发冲突

破坏性变更：daemon start 不再默认 9877。.cli_control/port 是
source of truth；CLI 子命令自动适配。如需固定旧端口，显式 --port 9877。"
```

---

## Task 3: `_duration.py` — duration 字符串解析

**Files:**
- Create: `python/godot_cli_control/_duration.py`
- Test: `python/tests/test_duration.py`

- [ ] **Step 3.1: 写失败测试**

新建 `python/tests/test_duration.py`：

```python
from __future__ import annotations

import pytest

from godot_cli_control._duration import parse_duration


def test_parse_seconds() -> None:
    assert parse_duration("30s") == 30

def test_parse_minutes() -> None:
    assert parse_duration("30m") == 1800

def test_parse_hours() -> None:
    assert parse_duration("2h") == 7200

def test_parse_zero_is_zero() -> None:
    assert parse_duration("0") == 0

def test_parse_bare_int_means_seconds() -> None:
    assert parse_duration("90") == 90

def test_parse_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("two minutes")
```

- [ ] **Step 3.2: 跑测试确认失败**

```bash
uv run pytest python/tests/test_duration.py -v
```
Expected: ImportError

- [ ] **Step 3.3: 实现 parse_duration**

新建 `python/godot_cli_control/_duration.py`：

```python
"""Parse human-friendly duration strings (e.g. "30m", "2h", "90s") to seconds."""
from __future__ import annotations

import re

_PATTERN = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> int:
    """Return total seconds. Bare integer = seconds. 0 means disabled."""
    m = _PATTERN.match(text)
    if not m:
        raise ValueError(f"invalid duration {text!r} (use 30s / 30m / 2h / 0)")
    return int(m.group(1)) * _UNITS[m.group(2)]
```

- [ ] **Step 3.4: 跑测试**

```bash
uv run pytest python/tests/test_duration.py -v
```
Expected: 6 passed

- [ ] **Step 3.5: Commit**

```bash
git add python/godot_cli_control/_duration.py python/tests/test_duration.py
git commit -m "feat: parse_duration 解析 30s/30m/2h 字符串"
```

---

## Task 4: `registry.py` — 全局守护进程注册表

**Files:**
- Create: `python/godot_cli_control/registry.py`
- Test: `python/tests/test_registry.py`

- [ ] **Step 4.1: 写失败测试 — register / list_all / unregister 基本路径**

新建 `python/tests/test_registry.py`：

```python
"""Global daemon registry tests — 不实际起 Godot，只验状态文件 + 探活逻辑。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from godot_cli_control import registry


@pytest.fixture
def reg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "registry")
    return tmp_path / "registry"


def test_register_creates_record(reg_dir: Path, tmp_path: Path) -> None:
    proj = tmp_path / "p1"
    proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=12345, godot_bin="/x/godot",
                      log_path=str(proj / ".cli_control/godot.log"))
    records = registry.list_all()
    assert len(records) == 1
    r = records[0]
    assert r.pid == os.getpid()
    assert r.port == 12345
    assert Path(r.project_root) == proj.resolve()


def test_unregister_removes_record(reg_dir: Path, tmp_path: Path) -> None:
    proj = tmp_path / "p1"; proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x", log_path="x")
    registry.unregister(proj)
    assert registry.list_all() == []


def test_list_all_prunes_dead_pids(reg_dir: Path, tmp_path: Path) -> None:
    proj = tmp_path / "p1"; proj.mkdir()
    # PID 1 几乎不会是当前用户的 Godot；用一个肯定死的高位 PID
    registry.register(proj, pid=2_000_000, port=1, godot_bin="x", log_path="x")
    assert registry.list_all() == []  # 探活后死记录被清掉
    # 注册表文件也应被删
    assert not list(reg_dir.glob("*.json"))


def test_list_all_also_cleans_project_state_for_dead(
    reg_dir: Path, tmp_path: Path
) -> None:
    proj = tmp_path / "p1"; proj.mkdir()
    ctrl = proj / ".cli_control"
    ctrl.mkdir()
    (ctrl / "godot.pid").write_text("2000000")
    (ctrl / "port").write_text("12345")
    registry.register(proj, pid=2_000_000, port=12345, godot_bin="x",
                      log_path=str(ctrl / "godot.log"))
    registry.list_all()
    assert not (ctrl / "godot.pid").exists()
    assert not (ctrl / "port").exists()


def test_project_hash_stable(tmp_path: Path) -> None:
    p = tmp_path / "p"; p.mkdir()
    h1 = registry.project_hash(p)
    h2 = registry.project_hash(p)
    assert h1 == h2 and len(h1) == 12
```

- [ ] **Step 4.2: 跑测试确认 ImportError**

```bash
uv run pytest python/tests/test_registry.py -v
```
Expected: ImportError

- [ ] **Step 4.3: 实现 `registry.py`**

新建 `python/godot_cli_control/registry.py`：

```python
"""Global cross-project daemon registry under ~/.local/state/godot-cli-control/daemons/.

每个守护进程一份 ``<project_hash>.json``，记录 PID / 端口 / 项目路径 / 启动时刻。
``list_all()`` 顺手探活并清理死记录。无文件锁 —— 每条记录文件名以 project_hash
区分，写入幂等。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path

_REGISTRY_DIR = Path.home() / ".local" / "state" / "godot-cli-control" / "daemons"


@dataclass(frozen=True)
class DaemonRecord:
    project_root: str
    pid: int
    port: int
    started_at: str
    godot_bin: str
    log_path: str

    @property
    def record_file(self) -> Path:
        return _REGISTRY_DIR / f"{project_hash(Path(self.project_root))}.json"


def project_hash(project_root: Path) -> str:
    return hashlib.sha1(str(Path(project_root).resolve()).encode()).hexdigest()[:12]


def register(
    project_root: Path,
    *,
    pid: int,
    port: int,
    godot_bin: str,
    log_path: str,
) -> None:
    _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_root": str(Path(project_root).resolve()),
        "pid": pid,
        "port": port,
        "started_at": _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "godot_bin": godot_bin,
        "log_path": log_path,
    }
    target = _REGISTRY_DIR / f"{project_hash(project_root)}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def unregister(project_root: Path) -> None:
    target = _REGISTRY_DIR / f"{project_hash(project_root)}.json"
    target.unlink(missing_ok=True)


def list_all() -> list[DaemonRecord]:
    """List live records. Dead PIDs are pruned (registry + project state files)."""
    if not _REGISTRY_DIR.exists():
        return []
    live: list[DaemonRecord] = []
    for f in sorted(_REGISTRY_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            r = DaemonRecord(**{k: data[k] for k in DaemonRecord.__dataclass_fields__})
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        if _process_alive(r.pid):
            live.append(r)
        else:
            _prune(r, f)
    return live


def _prune(record: DaemonRecord, registry_file: Path) -> None:
    registry_file.unlink(missing_ok=True)
    ctrl = Path(record.project_root) / ".cli_control"
    (ctrl / "godot.pid").unlink(missing_ok=True)
    (ctrl / "port").unlink(missing_ok=True)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user; treat as alive
    except OSError:
        return False
    return True
```

- [ ] **Step 4.4: 跑测试确认全 pass**

```bash
uv run pytest python/tests/test_registry.py -v
```
Expected: 5 passed

- [ ] **Step 4.5: Commit**

```bash
git add python/godot_cli_control/registry.py python/tests/test_registry.py
git commit -m "feat(registry): 全局守护进程注册表 + 死记录自动清理"
```

---

## Task 5: 把 registry 串到 `Daemon.start` / `Daemon.stop`

**Files:**
- Modify: `python/godot_cli_control/daemon.py` (start 末尾、stop 末尾)

- [ ] **Step 5.1: 写测试 — start 后注册表有一条**

追加到 `test_daemon.py`：

```python
def test_start_registers_in_global_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "godot"
    fake_bin.write_text("#!/bin/sh\nsleep 60\n"); fake_bin.chmod(0o755)
    monkeypatch.setattr("godot_cli_control.daemon._ensure_imported", lambda *a, **k: None)
    monkeypatch.setattr("godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True)

    daemon = Daemon(tmp_path)
    daemon.start(godot_bin=str(fake_bin), port=0)
    try:
        records = registry.list_all()
        assert len(records) == 1
        assert records[0].pid > 0
    finally:
        daemon.stop()
        assert registry.list_all() == []
```

- [ ] **Step 5.2: 跑确认失败**

```bash
uv run pytest python/tests/test_daemon.py::test_start_registers_in_global_registry -v
```
Expected: `assert 0 == 1` 或类似

- [ ] **Step 5.3: 接入 register / unregister**

`daemon.py` 顶部 import：

```python
from . import registry as _registry
```

`Daemon.start` 末尾、`return proc.pid` 之前追加：

```python
        _registry.register(
            self.project_root,
            pid=proc.pid,
            port=actual_port,
            godot_bin=bin_path,
            log_path=str(self.log_file),
        )
```

`Daemon.stop`：把 `_cleanup_state_files()` 后追加 `_registry.unregister(self.project_root)`（在两个 cleanup 路径都加；包括死 PID 自愈路径）。

- [ ] **Step 5.4: 跑全套**

```bash
uv run pytest python/tests/ -v
```
Expected: 全 pass

- [ ] **Step 5.5: Commit**

```bash
git add python/godot_cli_control/daemon.py python/tests/test_daemon.py
git commit -m "feat(daemon): start/stop 同步全局注册表"
```

---

## Task 6: `daemon ls` 子命令

**Files:**
- Modify: `python/godot_cli_control/cli.py` (新 cmd_daemon_ls + 子解析器)
- Test: `python/tests/test_cli.py`

- [ ] **Step 6.1: 写测试 — 调 ls 在空注册表上输出 JSON 信封 stopped/empty**

追加到 `test_cli.py`：

```python
def test_daemon_ls_empty(monkeypatch, tmp_path, capsys):
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    from godot_cli_control.cli import main
    rc = main(["daemon", "ls", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"daemons": []' in out

def test_daemon_ls_lists_active(monkeypatch, tmp_path, capsys):
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    proj = tmp_path / "p"; proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=12345,
                      godot_bin="x", log_path="y")
    from godot_cli_control.cli import main
    rc = main(["daemon", "ls", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "12345" in out
    assert str(proj.resolve()) in out
```

- [ ] **Step 6.2: 跑确认失败**

```bash
uv run pytest python/tests/test_cli.py -v -k daemon_ls
```
Expected: argparse "invalid choice: 'ls'"

- [ ] **Step 6.3: 实现 `cmd_daemon_ls` + parser**

在 `cli.py` 加函数（参考 `cmd_daemon_status` 风格）：

```python
def cmd_daemon_ls(ns: argparse.Namespace) -> int:
    from . import registry
    records = registry.list_all()
    payload = {
        "daemons": [
            {
                "project_root": r.project_root,
                "pid": r.pid,
                "port": r.port,
                "started_at": r.started_at,
                "godot_bin": r.godot_bin,
                "log_path": r.log_path,
            }
            for r in records
        ]
    }
    if ns.output_format == OUTPUT_JSON:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if not records:
            print("(no running daemons)")
        else:
            for r in records:
                print(f"{r.pid}\t{r.port}\t{r.project_root}\t{r.started_at}")
    return 0
```

在 `build_parser` 中 daemon_subs 处追加：

```python
    ls_p = daemon_subs.add_parser(
        "ls",
        help="列出所有正在运行的 daemon（跨项目）",
        description=(
            "扫描全局注册表 ~/.local/state/godot-cli-control/daemons/，"
            "列出所有探活通过的 daemon。死记录会被自动清理。"
        ),
    )
    _add_output_format_flags(ls_p)
```

并在 dispatch 表中加 `("daemon", "ls"): cmd_daemon_ls`（或与现有 dispatch 风格一致）。

- [ ] **Step 6.4: 跑测试**

```bash
uv run pytest python/tests/test_cli.py -v -k daemon_ls
```
Expected: 2 passed

- [ ] **Step 6.5: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py
git commit -m "feat(cli): daemon ls 跨项目列出运行中的 daemon"
```

---

## Task 7: `daemon stop --all` / `--project <path>`

**Files:**
- Modify: `python/godot_cli_control/cli.py` (cmd_daemon_stop + parser)
- Test: `python/tests/test_cli.py`

- [ ] **Step 7.1: 写测试**

追加到 `test_cli.py`：

```python
def test_daemon_stop_all_invokes_terminate(monkeypatch, tmp_path):
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    proj1 = tmp_path / "a"; proj1.mkdir()
    proj2 = tmp_path / "b"; proj2.mkdir()
    registry.register(proj1, pid=111, port=1, godot_bin="x", log_path="y")
    registry.register(proj2, pid=222, port=2, godot_bin="x", log_path="y")

    stopped: list[Path] = []
    def fake_stop(self):
        stopped.append(self.project_root)
        return 0
    monkeypatch.setattr("godot_cli_control.daemon.Daemon.stop", fake_stop)
    monkeypatch.setattr("godot_cli_control.daemon.Daemon.is_running", lambda self: True)

    from godot_cli_control.cli import main
    rc = main(["daemon", "stop", "--all"])
    assert rc == 0
    assert {p.resolve() for p in stopped} == {proj1.resolve(), proj2.resolve()}

def test_daemon_stop_project_path(monkeypatch, tmp_path):
    from godot_cli_control import registry
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "reg")
    target = tmp_path / "p"; target.mkdir()
    seen: list[Path] = []
    monkeypatch.setattr("godot_cli_control.daemon.Daemon.stop",
                        lambda self: seen.append(self.project_root) or 0)
    monkeypatch.setattr("godot_cli_control.daemon.Daemon.is_running", lambda self: True)
    from godot_cli_control.cli import main
    rc = main(["daemon", "stop", "--project", str(target)])
    assert rc == 0
    assert seen == [target.resolve()]
```

- [ ] **Step 7.2: 跑确认失败**

```bash
uv run pytest python/tests/test_cli.py -v -k "daemon_stop_all or daemon_stop_project"
```
Expected: argparse 错（unrecognized --all）

- [ ] **Step 7.3: 改 `cmd_daemon_stop` + parser**

`stop_p` 加两个互斥 flag：

```python
    stop_p = daemon_subs.add_parser("stop", help="停止 daemon", ...)
    grp = stop_p.add_mutually_exclusive_group()
    grp.add_argument("--all", action="store_true",
                     help="停止注册表中所有运行中的 daemon")
    grp.add_argument("--project", type=Path, default=None,
                     help="停止指定项目根的 daemon（绝对/相对路径均可）")
```

改 `cmd_daemon_stop`：

```python
def cmd_daemon_stop(ns: argparse.Namespace) -> int:
    from .daemon import Daemon, DaemonError
    from . import registry

    if ns.all:
        worst = 0
        for r in registry.list_all():
            try:
                rc = Daemon(Path(r.project_root)).stop()
                worst = max(worst, rc)
            except DaemonError as e:
                print(f"[{r.project_root}] {e}", file=sys.stderr)
                worst = max(worst, 1)
        return worst

    target = ns.project.resolve() if ns.project else Path.cwd()
    daemon = Daemon(target)
    try:
        return daemon.stop()
    except DaemonError as e:
        # 沿用既有错误信封
        ...
```

- [ ] **Step 7.4: 跑测试**

```bash
uv run pytest python/tests/test_cli.py -v -k "daemon_stop"
```
Expected: 全 pass（包含旧的 `daemon stop` 测试）

- [ ] **Step 7.5: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py
git commit -m "feat(cli): daemon stop --all / --project <path>"
```

---

## Task 8: GDScript 端 idle timeout

**Files:**
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`

- [ ] **Step 8.1: 加 `--game-bridge-idle-timeout=` 解析**

参考 `_parse_port_from_args`，在 `game_bridge.gd:262` 之后追加：

```gdscript
func _parse_idle_timeout_from_args() -> int:
	for arg: String in OS.get_cmdline_args():
		if arg.begins_with("--game-bridge-idle-timeout="):
			var parts: PackedStringArray = arg.split("=", false, 1)
			if parts.size() != 2 or not parts[1].is_valid_int():
				push_warning("GameBridge: Invalid idle-timeout %s, disabling" % arg)
				return 0
			var secs: int = parts[1].to_int()
			return secs if secs > 0 else 0
	return 0
```

- [ ] **Step 8.2: 加 `_last_activity_ms` 与看门狗 Timer**

在变量区追加：

```gdscript
var _idle_timeout_secs: int = 0
var _last_activity_ms: int = 0
```

在 `_ready()` 末尾（listen 成功后）追加：

```gdscript
	_idle_timeout_secs = _parse_idle_timeout_from_args()
	_last_activity_ms = Time.get_ticks_msec()
	if _idle_timeout_secs > 0:
		var t: Timer = Timer.new()
		t.wait_time = 1.0
		t.autostart = true
		t.process_mode = Node.PROCESS_MODE_ALWAYS
		t.timeout.connect(_check_idle)
		add_child(t)
		print("GameBridge: idle-timeout %ds enabled" % _idle_timeout_secs)
```

加新方法：

```gdscript
func _check_idle() -> void:
	var idle_ms: int = Time.get_ticks_msec() - _last_activity_ms
	if idle_ms / 1000 >= _idle_timeout_secs:
		print("GameBridge: idle for %ds, shutting down" % (idle_ms / 1000))
		get_tree().quit()
```

在 `_handle_message` 函数体首行加：

```gdscript
	_last_activity_ms = Time.get_ticks_msec()
```

- [ ] **Step 8.3: 手动验证（无 Godot 单测，做端到端冒烟）**

```bash
# 在测试 Godot 项目里
godot-cli-control daemon start --idle-timeout 5s
sleep 8
godot-cli-control daemon ls --json   # 应该看到死记录被清，输出 daemons: []
```

记录冒烟结果，复制 godot.log 末尾到 commit message。

- [ ] **Step 8.4: Commit**

```bash
git add addons/godot_cli_control/bridge/game_bridge.gd
git commit -m "feat(bridge): --game-bridge-idle-timeout 空闲自动 shutdown

GDScript 端实现：1s Timer 检查 (now - last_activity) > timeout 则
get_tree().quit()。每次 _handle_message 入口刷新 last_activity。
默认 0 = 关闭，行为与今天一致。"
```

---

## Task 9: Python 端 `--idle-timeout` flag

**Files:**
- Modify: `python/godot_cli_control/cli.py` (`_add_daemon_flags`)
- Modify: `python/godot_cli_control/daemon.py` (`Daemon.start` + Popen args)
- Test: `python/tests/test_daemon.py`

- [ ] **Step 9.1: 写测试 — `start(idle_timeout=10)` 应让 Popen 收到 `--game-bridge-idle-timeout=10`**

追加到 `test_daemon.py`：

```python
def test_start_passes_idle_timeout_to_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    class FakeProc:
        pid = 99999
        def poll(self): return None
        def wait(self, timeout=None): return 0
    def fake_popen(args, **kwargs):
        captured["args"] = args
        return FakeProc()

    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "godot"; fake_bin.write_text("x"); fake_bin.chmod(0o755)
    monkeypatch.setattr("godot_cli_control.daemon._ensure_imported", lambda *a, **k: None)
    monkeypatch.setattr("godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True)
    monkeypatch.setattr("godot_cli_control.daemon.subprocess.Popen", fake_popen)
    monkeypatch.setattr("godot_cli_control.daemon._process_alive", lambda pid: False)

    daemon = Daemon(tmp_path)
    daemon.start(godot_bin=str(fake_bin), port=0, idle_timeout=10)
    assert any(a == "--game-bridge-idle-timeout=10" for a in captured["args"])

def test_start_omits_idle_timeout_when_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 同上但 idle_timeout=0 时 args 里不该出现该 flag
    ...
```

- [ ] **Step 9.2: 跑确认失败**

```bash
uv run pytest python/tests/test_daemon.py -v -k idle_timeout
```
Expected: TypeError unexpected keyword

- [ ] **Step 9.3: `Daemon.start` 接受 `idle_timeout`**

`daemon.py` 签名加：

```python
        idle_timeout: int = 0,
```

构造 args 后追加：

```python
        if idle_timeout > 0:
            args.append(f"--game-bridge-idle-timeout={idle_timeout}")
```

- [ ] **Step 9.4: CLI flag**

`_add_daemon_flags` 末尾追加：

```python
    p.add_argument(
        "--idle-timeout",
        type=str,
        default="0",
        help="空闲超时（如 30m / 2h / 90s / 0=关闭）。开启后 Godot 端 Timer 自动 quit。",
    )
```

`cmd_daemon_start` / `cmd_run` 中：

```python
    from ._duration import parse_duration
    try:
        idle = parse_duration(ns.idle_timeout)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr); return EXIT_USAGE
    daemon.start(..., idle_timeout=idle)
```

- [ ] **Step 9.5: 跑全套**

```bash
uv run pytest python/tests/ -v
```
Expected: 全 pass

- [ ] **Step 9.6: Commit**

```bash
git add python/godot_cli_control/daemon.py python/godot_cli_control/cli.py python/tests/test_daemon.py
git commit -m "feat(cli): daemon start --idle-timeout 30m 透传 GDScript 端"
```

---

## Task 10: 文档同步

**Files:**
- Modify: `python/README.md`
- Modify: `addons/godot_cli_control/SKILL.md`
- Modify: 各 init 落盘的 SKILL 模板（在 `python/godot_cli_control/templates/` 或 `init_cmd.py` 引用处）

- [ ] **Step 10.1: 找 SKILL 模板源头**

```bash
ls /Users/kesar/Projects/godot-cli-control/python/godot_cli_control/templates/ 2>&1
grep -rn "9877\|DEFAULT_PORT" /Users/kesar/Projects/godot-cli-control/python/godot_cli_control/templates/ \
  /Users/kesar/Projects/godot-cli-control/addons/godot_cli_control/SKILL.md \
  /Users/kesar/Projects/godot-cli-control/python/README.md \
  /Users/kesar/Projects/godot-cli-control/README.md 2>/dev/null
```

- [ ] **Step 10.2: 改文档要点**
- 默认端口 9877 → "由 OS 自动分配，写入 .cli_control/port"
- 提示外部脚本连接 daemon 时**优先读 .cli_control/port**，而非 hardcode 9877
- README 加 changelog 条目，破坏性变更单独标注
- README 加 `daemon ls` / `daemon stop --all` / `--idle-timeout` 简短示例

- [ ] **Step 10.3: Commit**

```bash
git add python/README.md addons/godot_cli_control/SKILL.md python/godot_cli_control/templates/
git commit -m "docs: 同步默认端口变更与新子命令 ls / stop --all / --idle-timeout"
```

---

## Task 11: 端到端验证

无单测覆盖、需要真 Godot 的最后一关。

- [ ] **Step 11.1: 端口自动分配冒烟**
  - 在两个 Godot 测试项目分别 `godot-cli-control daemon start` 不带 --port
  - `cat .cli_control/port` 两个端口不同且都不是 9877
  - 各跑一次 `godot-cli-control screenshot /tmp/a.png`，确认 RPC 通

- [ ] **Step 11.2: 全局注册表冒烟**
  - 任意 cwd 下 `godot-cli-control daemon ls --json` 看到两条
  - `kill -9 <pid>` 一个，再 `daemon ls`，死记录 + 对应 .cli_control/godot.pid 都被清
  - `godot-cli-control daemon stop --all` 全停

- [ ] **Step 11.3: idle timeout 冒烟**
  - `godot-cli-control daemon start --idle-timeout 5s`
  - `sleep 8 && pgrep -f 'godot.*--cli-control'` 应找不到该 PID
  - `daemon ls` 自动清理后输出空

- [ ] **Step 11.4: 收尾 issue**
  - 遵循全局 CLAUDE.md「实施收尾必开 issue」：盘点 review 标记、未覆盖手测场景、spec/实现差异
  - 候选项：Windows 注册表路径合规性 / 项目级 idle 默认值 config / `daemon ls` 多用户区分

---

## Notes for Implementer

- **TDD 严格度**：Task 1–7、9 全部 TDD（先写测试、先看失败、再实现）。Task 8（GDScript）、Task 10（文档）、Task 11（端到端）无单测，靠手动冒烟。
- **commit 频率**：每个 Task 一次 commit；Task 内部可以多次 commit 但每个 commit 必须独立编译通过。
- **不要扩大范围**：本 plan **只**做 spec 里这三件事。途中发现的其他问题（命名不一致、文件过大、潜在 bug）记到 Task 11 的收尾 issue 列表里，**不要顺手改**。
- **执行顺序**：Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11。Task 3（duration parser）在 Task 9 才用到，但提前实现以解耦。
