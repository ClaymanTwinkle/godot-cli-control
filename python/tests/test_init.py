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


def test_packed_array_handles_multiline_godot_format() -> None:
    """Godot 长插件列表 / 手工编辑可能折行；regex 必须跨行匹配。

    防 regression：单行 ``.*?`` 不带 DOTALL 会 miss 多行格式 → 落到 fallback
    分支追加新 ``enabled=`` 行，把原列表 last-wins 覆盖丢失。
    """
    text = (
        "[editor_plugins]\n"
        "enabled=PackedStringArray(\n"
        '    "res://addons/foo/plugin.cfg",\n'
        '    "res://addons/bar/plugin.cfg",\n'
        ")\n"
    )
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/baz/plugin.cfg"
    )
    assert changed is True
    # 三个值都在，且只有一个 enabled= 行
    assert out.count("enabled=") == 1, f"重复了 enabled 行：\n{out}"
    assert "res://addons/foo/plugin.cfg" in out
    assert "res://addons/bar/plugin.cfg" in out
    assert "res://addons/baz/plugin.cfg" in out


def test_packed_array_idempotent_on_multiline_when_value_present() -> None:
    text = (
        "[editor_plugins]\n"
        "enabled=PackedStringArray(\n"
        '    "res://addons/x/plugin.cfg"\n'
        ")\n"
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


def test_run_init_preserves_crlf_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 上的 CRLF 项目文件在 patch 后必须仍是 CRLF —— 防止 git diff
    把整个文件标成改动。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )

    project = tmp_path
    crlf_content = (
        "config_version=5\r\n"
        "\r\n"
        "[application]\r\n"
        'config/name="crlf-project"\r\n'
        'config/features=PackedStringArray("4.4")\r\n'
    )
    (project / "project.godot").write_bytes(crlf_content.encode("utf-8"))

    assert run_init(project) == 0
    after = (project / "project.godot").read_bytes()
    # 原 CRLF 行仍是 CRLF；新加的行 _patch_project_godot 也会写成 CRLF
    assert b"\r\nconfig_version=5\r\n" in (b"\r\n" + after) or after.startswith(
        b"config_version=5\r\n"
    )
    assert b"\r\n[autoload]\r\n" in after
    assert b"\r\nGameBridgeNode=" in after
    # 不应混入裸 LF
    bare_lf_count = after.count(b"\n") - after.count(b"\r\n")
    assert bare_lf_count == 0, f"出现裸 LF：{bare_lf_count} 处"


def test_locate_plugin_source_finds_repo_addons() -> None:
    """editable install / 源码模式下应能找到仓库顶层的 addons。"""
    src = locate_plugin_source()
    assert src is not None
    assert (src / "plugin.cfg").is_file()
    assert (src / "bridge" / "game_bridge.gd").is_file()


# ── init 与 skills 的集成 ──


def _make_min_godot_project(tmp_path: Path) -> Path:
    """造一个最小 project.godot —— 满足 run_init 的入口校验。"""
    (tmp_path / "project.godot").write_text(
        "config_version=5\n\n[application]\nconfig/name=\"x\"\n",
        encoding="utf-8",
    )
    return tmp_path


def test_init_writes_both_skills_by_default(tmp_path: Path) -> None:
    """默认 write_skills=True 时两条 SKILL.md 都生成。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    rc = run_init(proj)

    assert rc == 0
    assert (proj / CLAUDE_REL).is_file()
    assert (proj / CODEX_REL).is_file()


def test_init_no_skills_skips_skill_files(tmp_path: Path) -> None:
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    rc = run_init(proj, write_skills=False)

    assert rc == 0
    assert not (proj / CLAUDE_REL).exists()
    assert not (proj / CODEX_REL).exists()


def test_init_skills_only_skips_plugin_and_patch(tmp_path: Path) -> None:
    """skills_only=True：addons/ 不被建立、project.godot 不被改动、SKILL.md 写入。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    original = (proj / "project.godot").read_bytes()

    rc = run_init(proj, skills_only=True)

    assert rc == 0
    assert not (proj / "addons" / "godot_cli_control").exists()
    assert (proj / "project.godot").read_bytes() == original
    assert (proj / CLAUDE_REL).is_file()
    assert (proj / CODEX_REL).is_file()


def test_init_skills_only_still_validates_godot_project(tmp_path: Path) -> None:
    """skills_only 不能绕过 project.godot 入口校验 —— 否则会在非 Godot 目录乱写。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL

    # 不造 project.godot
    rc = run_init(tmp_path, skills_only=True)

    assert rc == 1
    assert not (tmp_path / CLAUDE_REL).exists()


def test_init_skills_no_clobber_preserves_existing(tmp_path: Path) -> None:
    """clobber_skills=False 遇到已存在的 SKILL.md 时保留原内容、补缺另一条。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    # 预先手写 .claude 那份；.codex 缺失
    (proj / CLAUDE_REL).parent.mkdir(parents=True, exist_ok=True)
    (proj / CLAUDE_REL).write_text("USER_CUSTOM_KEEP_ME", encoding="utf-8")

    rc = run_init(proj, skills_only=True, clobber_skills=False)

    assert rc == 0
    # 已存在那条不动
    assert (
        (proj / CLAUDE_REL).read_text(encoding="utf-8") == "USER_CUSTOM_KEEP_ME"
    )
    # 缺失那条补上
    assert (proj / CODEX_REL).is_file()
    assert "USER_CUSTOM_KEEP_ME" not in (
        proj / CODEX_REL
    ).read_text(encoding="utf-8")
