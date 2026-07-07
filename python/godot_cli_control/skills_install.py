"""SKILL.md 模板渲染与落盘（多文件：SKILL.md 核心 + references/ 按需细节）。

设计要点：
* skill 是「渐进披露」结构：``SKILL.md`` 是 agent 触发时整体进上下文的核心契约
  （保持小），命令细节 / 错误码全表 / 录制配方等放 ``references/*.md``，由 agent
  按需 Read——避免把 ~35k tokens 一次性灌进每个会话（AI 友好契约第 6 条）。
* CLI 帮助不再内嵌（旧 ``{{cli_help}}`` 注入已删）：agent 直接跑
  ``godot-cli-control <cmd> -h`` 拿永远最新的 ground truth，也让渲染结果
  不再随 argparse 折行（COLUMNS / Python 小版本）漂移。
* `render_skill` 是纯函数：入版本号，出渲染好的 SKILL.md 字符串。
  不读环境、不算版本 — 调用方注入，便于测试。
* `skill_files` 枚举完整文件集（相对路径 → 内容）；`install_skills` 只关心 IO：
  把文件集写到 `.claude/skills/...` 与 `.codex/skills/...` 两条相对路径。
  `force=False` 时逐文件跳过已存在的目标（幂等保护用户改动）；
  `force=True` 是 init 的默认（spec 决定）。
* 模板用 `importlib.resources.files` 取，wheel/editable install 都能命中。
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

CLAUDE_DIR = Path(".claude/skills/godot-cli-control")
CODEX_DIR = Path(".codex/skills/godot-cli-control")
# 兼容旧名：主文件相对路径（测试 / 外部脚本曾引用）
CLAUDE_REL = CLAUDE_DIR / "SKILL.md"
CODEX_REL = CODEX_DIR / "SKILL.md"

REFERENCES_DIRNAME = "references"


def _template_root():
    return files("godot_cli_control.templates.skill")


def render_skill(version: str) -> str:
    """读 SKILL.md 模板 → 注入 ``{{version}}`` → 返回最终文本。纯函数。"""
    template = (_template_root() / "SKILL.md").read_text(encoding="utf-8")
    return template.replace("{{version}}", version)


def skill_files(version: str) -> dict[str, str]:
    """完整 skill 文件集：相对路径（str，POSIX 风格）→ 内容。

    ``SKILL.md`` 经 `render_skill` 渲染；``references/*.md`` 原样透传
    （无占位符，保持模板即产物，便于人工 review diff）。
    """
    out: dict[str, str] = {"SKILL.md": render_skill(version)}
    refs = _template_root() / REFERENCES_DIRNAME
    for entry in sorted(refs.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".md"):
            out[f"{REFERENCES_DIRNAME}/{entry.name}"] = entry.read_text(
                encoding="utf-8"
            )
    return out


def install_skills(
    project_root: Path,
    *,
    version: str,
    force: bool = True,
) -> list[Path]:
    """把 skill 文件集写到 Claude / Codex 两条目标路径。

    返回实际写入的绝对路径列表（被 force=False 逐文件跳过的不计入）。
    """
    content_by_rel = skill_files(version)
    written: list[Path] = []
    for skill_dir in (CLAUDE_DIR, CODEX_DIR):
        for rel, content in content_by_rel.items():
            dst = project_root / skill_dir / rel
            if dst.exists() and not force:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
            written.append(dst)
    return written


if __name__ == "__main__":  # pragma: no cover — dev/CI 辅助入口
    # `python -m godot_cli_control.skills_install [目标目录]`：把当前模板渲染到
    # 目标目录（默认本仓 agent 加载的 .claude/skills/godot-cli-control/）。
    # CI 的 skill-render-drift 用它渲染对照副本；改完模板本地跑一次即同步。
    import sys

    from . import _version

    _target = Path(
        sys.argv[1] if len(sys.argv) > 1 else CLAUDE_DIR
    )
    _ver = getattr(_version, "__version__", "unknown")
    for _rel, _content in skill_files(_ver).items():
        _dst = _target / _rel
        _dst.parent.mkdir(parents=True, exist_ok=True)
        _dst.write_text(_content, encoding="utf-8")
        print(_dst)
