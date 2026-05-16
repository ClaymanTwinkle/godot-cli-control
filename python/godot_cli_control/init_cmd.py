"""``godot-cli-control init`` —— 一键接入。

在目标 Godot 项目根执行后：

1. 校验 ``project.godot`` 存在
2. 复制插件物料到 ``addons/godot_cli_control/``
3. patch ``project.godot``：注入 ``[autoload]`` 与 ``[editor_plugins]`` 两节，
   绕过 Godot Editor GUI 启用步骤
4. 自动检测 Godot 二进制，写入 ``.cli_control/godot_bin``，daemon 启动时
   会优先读取此文件

完成后用户只需 ``godot-cli-control daemon start`` 即可。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .daemon import find_godot_binary, reimport_project

PLUGIN_DIR_NAME = "godot_cli_control"
ADDONS_DIRNAME = "addons"

AUTOLOAD_KEY = "GameBridgeNode"
AUTOLOAD_VALUE = '"*res://addons/godot_cli_control/bridge/game_bridge.gd"'
PLUGIN_CFG_PATH = "res://addons/godot_cli_control/plugin.cfg"

# `run_init` 在 ``output_format='json'`` 模式下用此 key 把"预期错误"
# 的可读 message 回填到调用方传入的 ``result`` dict，由 ``cli.cmd_init``
# 取出来塞 JSON envelope。dunder 前缀显式声明"带外通道、非业务字段"，
# 避免与未来真实业务字段（如 `__init__` 之类）冲突。
INIT_RESULT_ERROR_KEY = "__init_error_message__"


def run_init(
    project_root: Path,
    force: bool = False,
    write_skills: bool = True,
    skills_only: bool = False,
    clobber_skills: bool = True,
    *,
    output_format: str = "text",
    result: dict[str, Any] | None = None,
) -> int:
    """实施接入流程。返回进程 exit code。

    ``skills_only=True``：跳过 1-4 步（插件复制 / project.godot patch /
    godot_bin 检测），只写 SKILL.md —— 用于 CLI 升级后单独刷新 skill。
    ``write_skills=False``：跳过第 5 步，给已自定义 skill 的用户留逃生口。
    两者由 cli.py 侧的 mutually_exclusive_group 保证不会同时为真。
    ``clobber_skills=False``：写 skill 时遇到已存在文件就跳过（用户改过
    SKILL.md、希望保留本地版又允许 init 把缺失那条补上时用）。与
    ``write_skills=False`` / ``skills_only=True`` 都兼容。

    ``output_format='json'``：抑制全部人类可读 print；调用方（cli.cmd_init）
    传入 ``result`` 字典，本函数会回填结构化字段，由 cli 侧封 JSON envelope。
    ``output_format='text'``（默认）保持旧行为，``result`` 可省略 / 也可传入
    并被回填，互不冲突。错误路径在 text 模式下打印到 stderr，json 模式下
    把 message 塞进 ``result[INIT_RESULT_ERROR_KEY]`` 供 envelope 取用。
    """
    quiet = output_format == "json"

    def _say(msg: str) -> None:
        if not quiet:
            print(msg)

    def _warn(msg: str) -> None:
        # 错误 / 警告：text 模式 stderr 可读；json 模式吞掉，由 envelope 负责。
        if not quiet:
            print(msg, file=sys.stderr)

    def _record(**kw: Any) -> None:
        if result is not None:
            result.update(kw)

    def _fail(message: str) -> int:
        _warn(f"错误：{message}")
        if result is not None:
            result[INIT_RESULT_ERROR_KEY] = message
        return 1

    _record(
        project_root=str(project_root),
        skills_only=skills_only,
        write_skills=write_skills,
        plugin_copied=False,
        plugin_overwritten=False,
        project_godot_changes=[],
        godot_bin=None,
        skills_written=[],
    )

    if not (project_root / "project.godot").is_file():
        return _fail(
            f"{project_root} 下没有 project.godot —— 不像 Godot 项目根。\n"
            "如果你确实在 Godot 项目内，请用 --path 指向项目根。"
        )

    if not skills_only:
        plugin_src = locate_plugin_source()
        if plugin_src is None:
            return _fail(
                "找不到插件源（addons/godot_cli_control/）。\n"
                "如果是从源码 editable install，请确保仓库布局完整；\n"
                "如果是从 wheel 安装，包资源可能损坏，请重装。"
            )

        addons_dir = project_root / ADDONS_DIRNAME
        plugin_dst = addons_dir / PLUGIN_DIR_NAME
        if plugin_dst.exists():
            if force:
                shutil.rmtree(plugin_dst)
                _copy_plugin(plugin_src, plugin_dst)
                _say(f"覆盖：{plugin_dst}")
                _record(plugin_copied=True, plugin_overwritten=True)
            else:
                _say(f"已存在：{plugin_dst}（用 --force 覆盖）")
        else:
            addons_dir.mkdir(parents=True, exist_ok=True)
            _copy_plugin(plugin_src, plugin_dst)
            _say(f"复制：{plugin_src} → {plugin_dst}")
            _record(plugin_copied=True)

        patched, changes = _patch_project_godot(project_root / "project.godot")
        if changes:
            _say(f"修改 project.godot：{', '.join(changes)}")
        elif patched:
            _say("project.godot 已配置好（未改动）")
        _record(project_godot_changes=changes)

        godot_bin = find_godot_binary()
        if godot_bin:
            control_dir = project_root / ".cli_control"
            control_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(control_dir, 0o700)
            except OSError:
                pass
            (control_dir / "godot_bin").write_text(godot_bin + "\n")
            _say(f"检测到 Godot：{godot_bin}（已写入 .cli_control/godot_bin）")
            _record(godot_bin=godot_bin)
            # 无条件重新导入：拷了新 .gd、改了 autoload，cache 必然 stale。
            # 不靠 daemon._ensure_imported 兜底是因为它要等 daemon start 才跑，
            # 用户这时已经看到 parse error；放在 init 里把"setup 完成"打到位，
            # 后续 daemon start 纯起服务、无隐性首次延迟。
            reimport_project(project_root, godot_bin)
        else:
            _warn(
                "警告：未自动检测到 Godot 二进制。\n"
                "请 `export GODOT_BIN=/path/to/godot` 或写到 "
                ".cli_control/godot_bin。\n"
                "（跳过资源重新导入；首次 daemon start 会兜底重建 cache）"
            )

    if write_skills:
        # lazy import：保持与 cli.cmd_init 那侧 `from .init_cmd import run_init`
        # 对称，两边都不在模块顶层 import，避免任一方将来改成顶层 import
        # 时形成循环 import。
        from . import _version, cli, skills_install

        # 用 format_full_help() 而非 build_parser().format_help()：让 SKILL.md
        # 内嵌完整 help 树（顶层 + 每个子命令、含 daemon 三动作），agent 不再需
        # 要为了看 combo -h / daemon start -h 再 shell 出去。
        cli_help = cli.format_full_help()
        version = getattr(_version, "__version__", "unknown")
        written = skills_install.install_skills(
            project_root,
            version=version,
            cli_help=cli_help,
            # 默认 clobber_skills=True 即覆盖（spec §4 决定，让 SKILL.md 跟随
            # 版本与 CLI 帮助自动同步）；用户加 --skills-no-clobber 时不动现
            # 有文件，只补缺。
            force=clobber_skills,
        )
        for p in written:
            _say(f"写入 skill：{p.relative_to(project_root)}")
        _record(skills_written=[str(p) for p in written])

    _say("")
    _say("已就绪。下一步：")
    _say("  godot-cli-control daemon start          # 启动 daemon")
    _say("  godot-cli-control tree 3                # 验证 RPC 通了")
    _say("  godot-cli-control daemon stop           # 停止")
    return 0


# ── 插件源定位 ──


def locate_plugin_source() -> Path | None:
    """按优先级查找 ``addons/godot_cli_control/``。

    1. wheel 内嵌：``godot_cli_control/_plugin/``（pyproject force-include）
    2. editable install：包目录上溯找 repo 顶层的 ``addons/godot_cli_control/``
    """
    pkg_dir = Path(__file__).resolve().parent

    bundled = pkg_dir / "_plugin"
    if (bundled / "plugin.cfg").is_file():
        return bundled

    for parent in pkg_dir.parents:
        candidate = parent / ADDONS_DIRNAME / PLUGIN_DIR_NAME
        if (candidate / "plugin.cfg").is_file():
            return candidate
    return None


def _copy_plugin(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)


# ── project.godot patch ──


def _patch_project_godot(path: Path) -> tuple[bool, list[str]]:
    """注入 ``[autoload]`` 与 ``[editor_plugins]`` 段；返回 (改动过, 变更列表)。

    用 ``read_bytes``/``write_bytes`` 透传 line endings：``Path.read_text`` 默认
    universal newlines 把 CRLF 折成 LF，再 write_text 又按平台转回，导致 Windows
    上原 CRLF 文件被改写成 LF，git diff 把全文标成改动。直接走 bytes 通道避免
    这层 platform 干扰；patch 只在我们追加的新行用 ``\\n``，对原有部分零影响。
    """
    raw = path.read_bytes()
    # decode 时把 \r\n 折成 \n 便于 regex 匹配；记下原文是否 CRLF 以便回写
    is_crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    changes: list[str] = []

    text, ch = _ensure_kv_in_section(
        text, "autoload", AUTOLOAD_KEY, f"{AUTOLOAD_KEY}={AUTOLOAD_VALUE}"
    )
    if ch:
        changes.append("autoload/GameBridgeNode")

    text, ch = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", PLUGIN_CFG_PATH
    )
    if ch:
        changes.append("editor_plugins/enabled")

    if changes:
        out = text.replace("\n", "\r\n") if is_crlf else text
        path.write_bytes(out.encode("utf-8"))
    return True, changes


def _find_section_bounds(text: str, section: str) -> tuple[int, int] | None:
    """返回 ``[section]`` body 的 (start, end)；section 不存在则 None。"""
    section_pat = re.compile(
        r"^\[" + re.escape(section) + r"\]\s*$", re.MULTILINE
    )
    m = section_pat.search(text)
    if not m:
        return None
    body_start = m.end()
    next_section = re.compile(r"^\[", re.MULTILINE)
    m2 = next_section.search(text, body_start)
    body_end = m2.start() if m2 else len(text)
    return body_start, body_end


def _ensure_kv_in_section(
    text: str, section: str, key: str, full_line: str
) -> tuple[str, bool]:
    """``[section]`` 下若已有 ``key=...`` 则不动；否则添加。"""
    bounds = _find_section_bounds(text, section)
    if bounds is None:
        suffix = f"\n\n[{section}]\n{full_line}\n"
        return text.rstrip() + suffix + "\n", True
    start, end = bounds
    body = text[start:end]
    key_pat = re.compile(r"^" + re.escape(key) + r"\s*=", re.MULTILINE)
    if key_pat.search(body):
        return text, False
    new_body = body.rstrip("\n") + "\n" + full_line + "\n"
    return text[:start] + new_body + text[end:], True


def _ensure_in_packed_array(
    text: str, section: str, key: str, value: str
) -> tuple[str, bool]:
    """``[section]`` 下保证 ``key=PackedStringArray(...)`` 含 ``value``。"""
    bounds = _find_section_bounds(text, section)
    if bounds is None:
        suffix = (
            f'\n\n[{section}]\n{key}=PackedStringArray("{value}")\n'
        )
        return text.rstrip() + suffix + "\n", True
    start, end = bounds
    body = text[start:end]
    # Godot 偶尔把长 PackedStringArray 折成多行（手工编辑 / 较长插件列表），
    # 用 [^)]* 跨行匹配；不能用 .*? 因为默认不跨 \n。
    line_pat = re.compile(
        r"^"
        + re.escape(key)
        + r"\s*=\s*PackedStringArray\((?P<inner>[^)]*)\)",
        re.MULTILINE,
    )
    lm = line_pat.search(body)
    if lm:
        inner = lm.group("inner")
        if f'"{value}"' in inner:
            return text, False
        existing = inner.strip()
        new_inner = (
            f'{existing}, "{value}"' if existing else f'"{value}"'
        )
        new_body = (
            body[: lm.start("inner")] + new_inner + body[lm.end("inner") :]
        )
        return text[:start] + new_body + text[end:], True
    new_body = (
        body.rstrip("\n") + "\n" + f'{key}=PackedStringArray("{value}")\n'
    )
    return text[:start] + new_body + text[end:], True
