"""SKILL.md 模板渲染与落盘。

设计要点：
* `render_skill` 是纯函数：入版本号 + CLI help 文本，出渲染好的 markdown 字符串。
  不读环境、不算版本、不调 cli — 调用方注入，便于测试。
* `install_skills` 只关心 IO：把渲染结果写到 `.claude/skills/...` 与 `.codex/skills/...`
  两条相对路径。`force=False` 时跳过已存在的目标，适合"幂等保护用户改动"的语义；
  `force=True` 是 init 的默认（spec 决定）。
* 模板用 `importlib.resources.files` 取，wheel/editable install 都能命中。
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

CLAUDE_REL = Path(".claude/skills/godot-cli-control/SKILL.md")
CODEX_REL = Path(".codex/skills/godot-cli-control/SKILL.md")


def render_skill(version: str, cli_help: str) -> str:
    """读模板 → 占位符替换 → 返回最终文本。纯函数。"""
    template = (
        files("godot_cli_control.templates.skill") / "SKILL.md"
    ).read_text(encoding="utf-8")
    return template.replace("{{version}}", version).replace(
        "{{cli_help}}", cli_help
    )


def install_skills(
    project_root: Path,
    *,
    version: str,
    cli_help: str,
    force: bool = True,
) -> list[Path]:
    """把渲染好的 SKILL.md 写到 Claude / Codex 两条目标路径。

    返回实际写入的绝对路径列表（被 force=False 跳过的不计入）。
    """
    content = render_skill(version, cli_help)
    written: list[Path] = []
    for rel in (CLAUDE_REL, CODEX_REL):
        dst = project_root / rel
        if dst.exists() and not force:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        written.append(dst)
    return written
