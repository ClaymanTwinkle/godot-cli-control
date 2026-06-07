"""Global daemon registry tests — 不实际起 Godot，只验状态文件 + 探活逻辑。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from godot_cli_control import registry


@pytest.fixture
def reg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path / "registry")
    return tmp_path / "registry"


@pytest.fixture
def dead_pid() -> int:
    """一个 100% 已死、跨平台稳定的 PID。

    起一个立即退出的子进程并 reap（wait）掉，其 PID 此后保证不存活——
    比硬编码 magic PID（如 2_000_000）稳：后者依赖 OS 的 PID 上限，
    Linux pid_max=4194304 下长跑系统理论上可能真有进程占到该 PID 而 flaky。
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


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
    proj = tmp_path / "p1"
    proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x", log_path="x")
    registry.unregister(proj)
    assert registry.list_all() == []


def test_list_all_prunes_dead_pids(
    reg_dir: Path, tmp_path: Path, dead_pid: int
) -> None:
    proj = tmp_path / "p1"
    proj.mkdir()
    # 用一个已 reap 的子进程 PID —— 100% 死、跨平台稳
    registry.register(proj, pid=dead_pid, port=1, godot_bin="x", log_path="x")
    assert registry.list_all() == []  # 探活后死记录被清掉
    # 注册表文件也应被删
    assert not list(reg_dir.glob("*.json"))


def test_list_all_also_cleans_project_state_for_dead(
    reg_dir: Path, tmp_path: Path, dead_pid: int
) -> None:
    proj = tmp_path / "p1"
    proj.mkdir()
    ctrl = proj / ".cli_control"
    ctrl.mkdir()
    (ctrl / "godot.pid").write_text(str(dead_pid))
    (ctrl / "port").write_text("12345")
    registry.register(proj, pid=dead_pid, port=12345, godot_bin="x",
                      log_path=str(ctrl / "godot.log"))
    registry.list_all()
    assert not (ctrl / "godot.pid").exists()
    assert not (ctrl / "port").exists()


def test_project_hash_stable(tmp_path: Path) -> None:
    p = tmp_path / "p"
    p.mkdir()
    h1 = registry.project_hash(p)
    h2 = registry.project_hash(p)
    assert h1 == h2 and len(h1) == 12


def test_process_alive_branches(dead_pid: int) -> None:
    """直接覆盖 _process_alive 四个分支，避免未来 'simplify' 误改 PermissionError 含义。"""
    assert registry._process_alive(0) is False
    assert registry._process_alive(-1) is False
    assert registry._process_alive(os.getpid()) is True
    assert registry._process_alive(dead_pid) is False


# ── _user_state_dir 平台选址（#43）──


def test_user_state_dir_windows_uses_localappdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 上落到 %LOCALAPPDATA%\\godot-cli-control，而非 XDG 的 ~/.local/state。"""
    monkeypatch.setattr(registry.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\tester\AppData\Local")
    d = registry._user_state_dir()
    assert d == Path(r"C:\Users\tester\AppData\Local") / "godot-cli-control"


def test_user_state_dir_windows_falls_back_without_localappdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCALAPPDATA 缺失时回落到 ~/AppData/Local，不会误用 XDG 路径。"""
    monkeypatch.setattr(registry.sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    d = registry._user_state_dir()
    assert d == Path.home() / "AppData" / "Local" / "godot-cli-control"


def test_user_state_dir_posix_keeps_xdg_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux / macOS 维持既有 ~/.local/state 选址（不给已有用户搬家）。"""
    monkeypatch.setattr(registry.sys, "platform", "linux")
    assert registry._user_state_dir() == Path.home() / ".local" / "state" / "godot-cli-control"
    monkeypatch.setattr(registry.sys, "platform", "darwin")
    assert registry._user_state_dir() == Path.home() / ".local" / "state" / "godot-cli-control"


# ── 多实例支持（spec 2026-06-07）──


def test_register_with_instance_filename(reg_dir: Path, tmp_path: Path) -> None:
    """instance 参数应改变注册表文件名为 <hash>-<instance>.json。"""
    proj = tmp_path / "p1"
    proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x",
                      log_path="x", instance="server")
    files = list(reg_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == f"{registry.project_hash(proj)}-server.json"
    r = registry.list_all()[0]
    assert r.instance == "server"


def test_same_project_two_instances_coexist(reg_dir: Path, tmp_path: Path) -> None:
    """同一项目的两个实例可以同时在注册表中共存（各自独立的 JSON 文件）。"""
    proj = tmp_path / "p1"
    proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x",
                      log_path="x", instance="server")
    registry.register(proj, pid=os.getpid(), port=2, godot_bin="x",
                      log_path="x", instance="client1")
    assert {r.instance for r in registry.list_all()} == {"server", "client1"}


def test_legacy_record_read_as_default(reg_dir: Path, tmp_path: Path) -> None:
    """无 instance 字段的旧格式记录被读为 instance="default"（向后兼容）。"""
    import json as _json

    proj = tmp_path / "p1"
    proj.mkdir()
    reg_dir.mkdir(parents=True, exist_ok=True)
    legacy = reg_dir / f"{registry.project_hash(proj)}.json"
    legacy.write_text(_json.dumps({
        "project_root": str(proj.resolve()), "pid": os.getpid(), "port": 7,
        "started_at": "2026-01-01T00:00:00+00:00", "godot_bin": "x", "log_path": "x",
    }))  # 旧格式：无 instance 字段
    r = registry.list_all()[0]
    assert r.instance == "default"


def test_unregister_targets_instance(reg_dir: Path, tmp_path: Path) -> None:
    """unregister 精确删除指定实例；不存在的实例是 no-op；删存在的才清除记录。"""
    proj = tmp_path / "p1"
    proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x",
                      log_path="x", instance="server")
    registry.unregister(proj, instance="client1")  # 不存在的实例 → no-op
    assert len(registry.list_all()) == 1
    registry.unregister(proj, instance="server")
    assert registry.list_all() == []


def test_prune_dead_new_format_cleans_instance_dir(
    reg_dir: Path, tmp_path: Path, dead_pid: int
) -> None:
    """dead PID 的新格式记录被 prune 时，会清理 instances/<name>/ 下的状态文件。"""
    proj = tmp_path / "p1"
    proj.mkdir()
    inst_dir = proj / ".cli_control" / "instances" / "server"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(dead_pid))
    (inst_dir / "port").write_text("1")
    registry.register(proj, pid=dead_pid, port=1, godot_bin="x",
                      log_path="x", instance="server")
    assert registry.list_all() == []
    assert not (inst_dir / "godot.pid").exists()
    assert not (inst_dir / "port").exists()
