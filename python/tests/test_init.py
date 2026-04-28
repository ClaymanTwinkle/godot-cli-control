"""init_cmd 单元测试 —— project.godot patch 与端到端接入流程。"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_cli_control.init_cmd import (
    _ensure_in_packed_array,
    _ensure_kv_in_section,
    locate_plugin_source,
    run_init,
)


# ── _ensure_kv_in_section ──


def test_kv_adds_section_when_missing() -> None:
    text = "config_version=5\n\n[application]\nconfig/name=\"x\"\n"
    out, changed = _ensure_kv_in_section(text, "autoload", "Foo", 'Foo="*res://x"')
    assert changed is True
    assert "[autoload]" in out
    assert 'Foo="*res://x"' in out


def test_kv_appends_when_section_exists_without_key() -> None:
    text = '[autoload]\nOther="*res://other.gd"\n'
    out, changed = _ensure_kv_in_section(
        text, "autoload", "GameBridgeNode", 'GameBridgeNode="*res://gb.gd"'
    )
    assert changed is True
    assert 'Other="*res://other.gd"' in out
    assert 'GameBridgeNode="*res://gb.gd"' in out


def test_kv_idempotent_when_key_exists() -> None:
    text = '[autoload]\nGameBridgeNode="*res://existing.gd"\n'
    out, changed = _ensure_kv_in_section(
        text, "autoload", "GameBridgeNode", 'GameBridgeNode="*res://NEW.gd"'
    )
    assert changed is False
    assert out == text  # 不动


# ── _ensure_in_packed_array ──


def test_packed_array_adds_section_when_missing() -> None:
    text = "config_version=5\n"
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/x/plugin.cfg"
    )
    assert changed is True
    assert (
        'enabled=PackedStringArray("res://addons/x/plugin.cfg")' in out
    )


def test_packed_array_appends_to_existing_array() -> None:
    text = (
        "[editor_plugins]\n"
        'enabled=PackedStringArray("res://addons/foo/plugin.cfg")\n'
    )
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/bar/plugin.cfg"
    )
    assert changed is True
    assert "res://addons/foo/plugin.cfg" in out
    assert "res://addons/bar/plugin.cfg" in out


def test_packed_array_idempotent_when_value_present() -> None:
    text = (
        "[editor_plugins]\n"
        'enabled=PackedStringArray("res://addons/x/plugin.cfg")\n'
    )
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/x/plugin.cfg"
    )
    assert changed is False
    assert out == text


def test_packed_array_creates_key_when_section_has_other_keys() -> None:
    text = "[editor_plugins]\nsomething=else\n"
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/x/plugin.cfg"
    )
    assert changed is True
    assert "something=else" in out
    assert 'enabled=PackedStringArray("res://addons/x/plugin.cfg")' in out


# ── 端到端 run_init ──


def _minimal_project(tmp_path: Path) -> Path:
    """造一个最小 Godot 项目根。"""
    (tmp_path / "project.godot").write_text(
        "config_version=5\n\n"
        "[application]\n"
        'config/name="dummy"\n'
        'config/features=PackedStringArray("4.4", "GL Compatibility")\n'
    )
    return tmp_path


def test_run_init_rejects_non_godot_dir(tmp_path: Path, capsys) -> None:
    rc = run_init(tmp_path)
    assert rc == 1
    assert "project.godot" in capsys.readouterr().err


def test_run_init_creates_addons_and_patches_project_godot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快乐路径：在干净项目上跑一次，断言所有产物。"""
    # 让 init 找不到 GODOT_BIN，避免污染本地 .cli_control
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )

    project = _minimal_project(tmp_path)
    rc = run_init(project)
    assert rc == 0

    plugin_dst = project / "addons" / "godot_cli_control"
    assert (plugin_dst / "plugin.cfg").is_file()
    assert (plugin_dst / "bridge" / "game_bridge.gd").is_file()

    pg = (project / "project.godot").read_text()
    assert "[autoload]" in pg
    assert 'GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"' in pg
    assert "[editor_plugins]" in pg
    assert 'enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")' in pg


def test_run_init_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复跑 init 不能破坏 project.godot 或抛错。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )

    project = _minimal_project(tmp_path)
    assert run_init(project) == 0
    pg_after_first = (project / "project.godot").read_text()

    # 第二次：应该 noop
    assert run_init(project) == 0
    pg_after_second = (project / "project.godot").read_text()
    assert pg_after_first == pg_after_second


def test_run_init_writes_godot_bin_when_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检测到 Godot 时应写 .cli_control/godot_bin。"""
    fake_bin = tmp_path / "godot_fake"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary",
        lambda: str(fake_bin),
    )

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    project = _minimal_project(proj_dir)
    rc = run_init(project)
    assert rc == 0
    saved = (project / ".cli_control" / "godot_bin").read_text().strip()
    assert saved == str(fake_bin)


def test_locate_plugin_source_finds_repo_addons() -> None:
    """editable install / 源码模式下应能找到仓库顶层的 addons。"""
    src = locate_plugin_source()
    assert src is not None
    assert (src / "plugin.cfg").is_file()
    assert (src / "bridge" / "game_bridge.gd").is_file()
