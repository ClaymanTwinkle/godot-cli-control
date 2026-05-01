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
