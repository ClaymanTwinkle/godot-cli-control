"""skills_install 单元测试 —— 渲染 + 落盘行为，纯函数零 IO 副作用边界。

skill 是多文件结构（SKILL.md 核心 + references/ 细节，渐进披露）：
- 核心契约（信封 / 退出码 / shell canonical / 顶级 pitfalls / 路由表）锁在 SKILL.md；
- 细节契约（录制管线 / pytest fixtures / combo schema …）锁在对应 reference 文件。
测试按「内容住在哪个文件」断言，防止未来重构把契约改没或挪丢。
"""

from __future__ import annotations

from pathlib import Path


# ── render_skill（SKILL.md 核心） ──


def test_render_skill_substitutes_version() -> None:
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="1.2.3")

    assert "1.2.3" in out
    assert "{{version}}" not in out
    # 旧 {{cli_help}} 注入已删——模板不该再有这个占位符
    assert "{{cli_help}}" not in out


def test_render_skill_keeps_frontmatter_intact() -> None:
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="0.0.0")

    assert out.splitlines()[0] == "---"
    assert "name: godot-cli-control" in out


def test_skill_description_covers_recording_keywords() -> None:
    """frontmatter ``description`` 是 agent 触发本 skill 的唯一信号源。
    用户问"录制 / 录屏 / 演示 / 录像"时，主流 agent 走的是 description 的语义
    匹配——锚点词缺失会漏触发。锁住几个非冗余的英文关键词，保护未来文案重写
    时不会把这些词改没。"""
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="x")
    desc_block = out.split("---", 2)[1]  # 第二段就是 frontmatter

    for kw in ("record", "video", "capture", "movie", "demo"):
        assert kw in desc_block.lower(), (
            f"description 缺关键词 {kw!r}，可能导致用户问录屏/录像时 agent 漏触发"
        )


def test_render_skill_core_contract() -> None:
    """SKILL.md 核心必须锁住的字面契约，防止后续改文案时回退：

    1. JSON 信封（默认 JSON + ok true/false 两形态）与退出码表。
    2. shell 是 canonical surface。
    3. node path 绝对约束（最常见 agent 错误）。
    4. ``daemon status`` 在 Quickstart 露出。
    5. 指向 references/ 的路由（渐进披露入口）与 `-h` 现场查帮助的指引。
    """
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="x")

    assert '"ok": true' in out or '"ok":true' in out
    assert '"ok": false' in out or '"ok":false' in out
    assert "Exit codes" in out
    assert "shell" in out.lower()
    assert "/root/" in out
    assert "daemon status" in out
    assert "references/" in out
    assert "-h" in out


def test_render_skill_core_stays_lean() -> None:
    """核心文件的体积上限是本次拆分的目标本身（每次触发整体进上下文）。
    上限给宽到 400 行——超过说明有人把细节写回核心了，应挪去 references/。"""
    from godot_cli_control.skills_install import render_skill

    n_lines = len(render_skill(version="x").splitlines())
    assert n_lines <= 400, (
        f"SKILL.md 核心已 {n_lines} 行（>400）——细节请放 references/*.md，"
        "核心只留契约 + 目录 + 路由"
    )


# ── skill_files（完整文件集） ──


def test_skill_files_contains_core_and_references() -> None:
    from godot_cli_control.skills_install import skill_files

    out = skill_files(version="1.0")

    assert "SKILL.md" in out
    ref_names = {k for k in out if k.startswith("references/")}
    # 六个主题文件一个都不能少；新增可以，减少/改名必须同步 SKILL.md 路由表
    assert {
        "references/commands.md",
        "references/error-codes.md",
        "references/daemon-multi-instance.md",
        "references/recording.md",
        "references/python-and-pytest.md",
        "references/pitfalls.md",
    } <= ref_names


def test_skill_routing_table_covers_every_reference() -> None:
    """SKILL.md 核心的路由表必须指到每一个实际存在的 reference 文件，
    否则 agent 不知道该文件存在——渐进披露就断链了。"""
    from godot_cli_control.skills_install import skill_files

    out = skill_files(version="x")
    core = out["SKILL.md"]
    for rel in out:
        if rel == "SKILL.md":
            continue
        name = rel.removeprefix("references/")
        assert name in core, f"SKILL.md 路由表漏了 {rel}"


def test_reference_recording_explains_pipeline() -> None:
    """录制细节住 references/recording.md —— 最少要点出
    ``--record`` + ``--movie-path`` + ffmpeg 转 mp4 这条链条。"""
    from godot_cli_control.skills_install import skill_files

    ref = skill_files(version="x")["references/recording.md"]

    assert "--record" in ref
    assert "--movie-path" in ref
    assert "ffmpeg" in ref
    assert "mp4" in ref


def test_reference_pytest_documents_plugin() -> None:
    """pytest 插件契约住 references/python-and-pytest.md：fixtures、可选依赖组、
    plugin 注册的 CLI 选项、entry-point 失效时的 conftest.py 退路。"""
    from godot_cli_control.skills_install import skill_files

    ref = skill_files(version="x")["references/python-and-pytest.md"]

    assert "godot_daemon" in ref
    assert "godot-cli-control[pytest]" in ref
    assert "--godot-cli-port" in ref
    assert "--godot-cli-no-headless" in ref
    assert "--godot-cli-project-root" in ref
    assert "pytest_plugins" in ref
    # python -m 等价入口（PATH 未激活 venv 时的唯一退路）
    assert "python -m godot_cli_control" in ref
    # Python 桥定位：跨步保持 client 连接时的次选
    assert "wait_for_node" in ref
    assert "get_property" in ref


def test_reference_commands_documents_combo_schema() -> None:
    """combo step 的真实 schema（``action`` / ``wait`` keys）住
    references/commands.md，agent 写错 schema 是历史高频错误。"""
    from godot_cli_control.skills_install import skill_files

    ref = skill_files(version="x")["references/commands.md"]

    assert '"action"' in ref
    assert '"wait"' in ref


# ── install_skills ──


def test_install_skills_writes_both_paths(tmp_path: Path) -> None:
    from godot_cli_control.skills_install import (
        CLAUDE_DIR,
        CLAUDE_REL,
        CODEX_REL,
        install_skills,
        render_skill,
        skill_files,
    )

    written = install_skills(tmp_path, version="9.9.9")

    claude = tmp_path / CLAUDE_REL
    codex = tmp_path / CODEX_REL
    assert claude.exists()
    assert codex.exists()
    expected = render_skill(version="9.9.9")
    assert claude.read_text(encoding="utf-8") == expected
    assert codex.read_text(encoding="utf-8") == expected
    # references/ 同步落盘，内容与模板一致
    n_files = len(skill_files(version="9.9.9"))
    assert len(written) == n_files * 2
    ref = tmp_path / CLAUDE_DIR / "references" / "commands.md"
    assert ref.exists()


def test_install_skills_creates_parent_dirs(tmp_path: Path) -> None:
    """空 tmp_path 下也能成功 —— 验证 mkdir(parents=True)。"""
    from godot_cli_control.skills_install import install_skills

    # 不预建任何目录
    install_skills(tmp_path, version="0")

    assert (tmp_path / ".claude" / "skills" / "godot-cli-control").is_dir()
    assert (
        tmp_path / ".codex" / "skills" / "godot-cli-control" / "references"
    ).is_dir()


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

    install_skills(tmp_path, version="1.0", force=True)

    assert "DIRTY" not in (tmp_path / CLAUDE_REL).read_text(encoding="utf-8")
    assert "DIRTY" not in (tmp_path / CODEX_REL).read_text(encoding="utf-8")


def test_install_skills_force_false_skips_existing(tmp_path: Path) -> None:
    """force=False 逐文件跳过：用户改过的 SKILL.md 保留，缺失的
    references/ 仍会补上（老版本单文件安装升级到多文件的路径）。"""
    from godot_cli_control.skills_install import (
        CLAUDE_DIR,
        CLAUDE_REL,
        CODEX_REL,
        install_skills,
    )

    # 预先写脏内容到 CLAUDE_REL；CODEX_REL 留空
    claude = tmp_path / CLAUDE_REL
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text("KEEP_ME", encoding="utf-8")

    written = install_skills(tmp_path, version="1.0", force=False)

    assert claude.read_text(encoding="utf-8") == "KEEP_ME"
    assert (tmp_path / CODEX_REL).exists()
    assert claude not in written
    assert (tmp_path / CODEX_REL) in written
    # 同目录缺失的 reference 被补上（force=False 只护已存在的文件）
    assert (tmp_path / CLAUDE_DIR / "references" / "pitfalls.md").exists()
