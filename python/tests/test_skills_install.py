"""skills_install 单元测试 —— 渲染 + 落盘行为，纯函数零 IO 副作用边界。"""

from __future__ import annotations

from pathlib import Path

import pytest


# ── render_skill ──


def test_render_skill_substitutes_placeholders() -> None:
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="1.2.3", cli_help="USAGE: foo")

    assert "1.2.3" in out
    assert "USAGE: foo" in out
    assert "{{version}}" not in out
    assert "{{cli_help}}" not in out


def test_render_skill_keeps_frontmatter_intact() -> None:
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="0.0.0", cli_help="x")

    assert out.splitlines()[0] == "---"
    assert "name: godot-cli-control" in out


# ── install_skills ──


def test_install_skills_writes_both_paths(tmp_path: Path) -> None:
    from godot_cli_control.skills_install import (
        CLAUDE_REL,
        CODEX_REL,
        install_skills,
        render_skill,
    )

    written = install_skills(
        tmp_path, version="9.9.9", cli_help="HELPTEXT_MARKER"
    )

    claude = tmp_path / CLAUDE_REL
    codex = tmp_path / CODEX_REL
    assert claude.exists()
    assert codex.exists()
    expected = render_skill(version="9.9.9", cli_help="HELPTEXT_MARKER")
    assert claude.read_text(encoding="utf-8") == expected
    assert codex.read_text(encoding="utf-8") == expected
    assert set(written) == {claude, codex}


def test_install_skills_creates_parent_dirs(tmp_path: Path) -> None:
    """空 tmp_path 下也能成功 —— 验证 mkdir(parents=True)。"""
    from godot_cli_control.skills_install import install_skills

    # 不预建任何目录
    install_skills(tmp_path, version="0", cli_help="")

    assert (tmp_path / ".claude" / "skills" / "godot-cli-control").is_dir()
    assert (tmp_path / ".codex" / "skills" / "godot-cli-control").is_dir()


def test_install_skills_force_true_overwrites(tmp_path: Path) -> None:
    from godot_cli_control.skills_install import (
        CLAUDE_REL,
        CODEX_REL,
        install_skills,
    )

    # 预先写脏内容
    for rel in (CLAUDE_REL, CODEX_REL):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("DIRTY", encoding="utf-8")

    install_skills(tmp_path, version="1.0", cli_help="x", force=True)

    assert "DIRTY" not in (tmp_path / CLAUDE_REL).read_text(encoding="utf-8")
    assert "DIRTY" not in (tmp_path / CODEX_REL).read_text(encoding="utf-8")


def test_install_skills_force_false_skips_existing(tmp_path: Path) -> None:
    from godot_cli_control.skills_install import (
        CLAUDE_REL,
        CODEX_REL,
        install_skills,
    )

    # 预先写脏内容到 CLAUDE_REL；CODEX_REL 留空
    claude = tmp_path / CLAUDE_REL
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text("KEEP_ME", encoding="utf-8")

    written = install_skills(
        tmp_path, version="1.0", cli_help="x", force=False
    )

    assert claude.read_text(encoding="utf-8") == "KEEP_ME"
    assert (tmp_path / CODEX_REL).exists()
    assert claude not in written
    assert (tmp_path / CODEX_REL) in written
