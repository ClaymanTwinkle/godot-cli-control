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


def test_skill_description_covers_recording_keywords() -> None:
    """frontmatter ``description`` 是 agent 触发本 skill 的唯一信号源。
    用户问"录制 / 录屏 / 演示 / 录像"时，主流 agent 走的是 description 的语义
    匹配——锚点词缺失会漏触发。锁住几个非冗余的英文关键词，保护未来文案重写
    时不会把这些词改没。"""
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="x", cli_help="x")
    desc_block = out.split("---", 2)[1]  # 第二段就是 frontmatter

    for kw in ("record", "video", "capture", "movie", "demo"):
        assert kw in desc_block.lower(), (
            f"description 缺关键词 {kw!r}，可能导致用户问录屏/录像时 agent 漏触发"
        )


def test_render_skill_explains_recording_pipeline() -> None:
    """SKILL.md 主体必须教 agent 如何录视频 —— 最少要点出
    ``--record`` + ``--movie-path`` + ffmpeg 转 mp4 这条链条，
    否则命中 skill 后还得 shell 出去翻 daemon start -h 才能下笔。"""
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="x", cli_help="x")

    assert "--record" in out
    assert "--movie-path" in out
    assert "ffmpeg" in out
    assert "mp4" in out


def test_render_skill_documents_pytest_plugin() -> None:
    """pytest 插件是项目的一等公民使用面（pyproject.toml 注册了 pytest11
    entry-point + 可选依赖组）。SKILL.md 必须教 agent 用 ``godot_daemon`` /
    ``bridge`` fixture，否则用户问"给这个 Godot 项目加 e2e 测试"时 agent 只
    会复述 ``def run(bridge):`` 脚本路径，错过零样板的 fixture 路径。"""
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="x", cli_help="x")

    assert "godot_daemon" in out
    assert "godot-cli-control[pytest]" in out
    # plugin 注册的三个 CLI 选项都要可被 agent 看到
    assert "--godot-cli-port" in out
    assert "--godot-cli-no-headless" in out
    assert "--godot-cli-project-root" in out
    # 兜底兼容路径（entry-point 不生效时的 conftest.py 写法）
    assert "pytest_plugins" in out


def test_render_skill_documents_module_entry_point() -> None:
    """``python -m godot_cli_control`` 与 console script 等价；当 PATH 未激活
    venv 时是唯一可用入口，文档必须留给 agent 这条退路。"""
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="x", cli_help="x")
    assert "python -m godot_cli_control" in out


def test_render_skill_documents_combo_schema_and_ai_contract() -> None:
    """SKILL.md 模板必须给 agent 锁住几件事，否则它会写错代码：

    1. combo step 的真实 schema（``action`` / ``wait`` keys，不是 press/release）。
    2. 0.2.0 输出契约：默认 JSON 信封 + 退出码语义。
    3. shell 是 canonical surface（agent 默认走 shell，不必动 Python）。
    4. ``daemon status`` 在 Quickstart 出现，agent 第一次跑能确认 daemon 状态。
    5. Python 桥仍在文档里，但定位调整成"跨步保持 client 连接"用。

    字面串作为契约锁住模板，防止后续改文案时回退。
    """
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="x", cli_help="<HELP>")

    # combo schema：action + wait（不是 press/release）
    assert '"action"' in out
    assert '"wait"' in out
    # JSON 输出契约必须明示
    assert '"ok": true' in out or '"ok":true' in out
    assert '"ok": false' in out or '"ok":false' in out
    # 退出码语义：shell-if 友好的几个命令必须列在 Exit codes 表里
    assert "Exit codes" in out
    # shell 优先论调
    assert "shell" in out.lower()
    # Python 桥的位置：可调用，但是次选
    assert "wait_for_node" in out
    assert "get_property" in out
    # daemon status 在 Quickstart 露出
    assert "daemon status" in out
    # node path 绝对约束（最常见的 agent 错误）
    assert "/root/" in out


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
