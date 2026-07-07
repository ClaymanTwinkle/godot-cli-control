"""``godot-cli-control init`` —— 一键接入。

在目标 Godot 项目根执行后：

1. 校验 ``project.godot`` 存在
2. 复制插件物料到 ``addons/godot_cli_control/``（已存在则默认整目录刷新，``--keep-addon`` 跳过）
3. patch ``project.godot``：注入 ``[autoload]`` 与 ``[editor_plugins]`` 两节，
   绕过 Godot Editor GUI 启用步骤
4. 自动检测 Godot 二进制，写入 ``.cli_control/godot_bin``，daemon 启动时
   会优先读取此文件
5. 在项目根 ``.gitignore`` 追加 ``.cli_control/`` —— daemon 的机器本地状态
   目录（godot_bin 绝对路径 / pid / port / log / 录制中间产物），不应提交

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

GITIGNORE_NAME = ".gitignore"
# 需要 init 写进目标项目 .gitignore 的条目。用元组而非单值，给将来"增量加
# 忽略项"留口子（与 method_blacklist_extra 同样的增量哲学）；目前只有 daemon
# 的机器本地状态目录 .cli_control/。
GITIGNORE_ENTRIES: tuple[str, ...] = (".cli_control/",)

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
    clobber_addon: bool = True,
    write_skills: bool = True,
    skills_only: bool = False,
    clobber_skills: bool = True,
    write_gitignore: bool = True,
    *,
    output_format: str = "text",
    result: dict[str, Any] | None = None,
) -> int:
    """实施接入流程。返回进程 exit code。

    ``clobber_addon=False``（CLI ``--keep-addon``）：已存在
    ``addons/godot_cli_control/`` 时跳过插件复制，保留用户本地版本。
    默认 True：每次 init 都 rmtree+copy 刷新 addon，保证与当前 CLI 版本
    同步 —— 与 ``clobber_skills`` 默认覆盖同源的设计理由（CLI 升级后
    GDScript 侧必须跟上，否则两侧协议错位）。

    ``skills_only=True``：跳过 1-4 步（插件复制 / project.godot patch /
    godot_bin 检测），只写 SKILL.md —— 用于 CLI 升级后单独刷新 skill。
    ``write_skills=False``：跳过第 5 步，给已自定义 skill 的用户留逃生口。
    两者由 cli.py 侧的 mutually_exclusive_group 保证不会同时为真。
    ``clobber_skills=False``：写 skill 时遇到已存在文件就跳过（用户改过
    SKILL.md、希望保留本地版又允许 init 把缺失那条补上时用）。与
    ``write_skills=False`` / ``skills_only=True`` 都兼容。
    ``write_gitignore=False``：跳过往项目根 ``.gitignore`` 追加 ``.cli_control/``
    （不想让 init 碰 ``.gitignore`` 的用户逃生口）。该步骤属于"项目接入"阶段，
    ``skills_only=True`` 时本就跳过。

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
        gitignore_added=[],
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
            if clobber_addon:
                shutil.rmtree(plugin_dst)
                _copy_plugin(plugin_src, plugin_dst)
                _say(f"覆盖：{plugin_dst}")
                _record(plugin_copied=True, plugin_overwritten=True)
            else:
                _say(f"已存在：{plugin_dst}（--keep-addon 保留，未更新）")
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

        # .gitignore：无条件确保（不依赖 godot_bin 是否检测到——daemon 早晚
        # 会建 .cli_control/，提前忽略最稳）。属于"项目接入"阶段，故在
        # not skills_only 块内。
        if write_gitignore:
            added = _ensure_gitignore_entries(project_root)
            if added:
                _say(f"修改 .gitignore：忽略 {', '.join(added)}")
            _record(gitignore_added=added)

    if write_skills:
        # lazy import：保持与 cli.cmd_init 那侧 `from .init_cmd import run_init`
        # 对称，两边都不在模块顶层 import，避免任一方将来改成顶层 import
        # 时形成循环 import。
        from . import _version, skills_install

        # CLI 帮助不再注入 SKILL.md（agent 现场跑 `<cmd> -h` 拿最新版），
        # 所以这里不需要 cli.format_full_help() 了。
        version = getattr(_version, "__version__", "unknown")
        written = skills_install.install_skills(
            project_root,
            version=version,
            # 默认 clobber_skills=True 即覆盖（spec §4 决定，让 SKILL.md 跟随
            # 版本自动同步）；用户加 --skills-no-clobber 时不动现有文件，只补缺。
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


# ── .gitignore 维护 ──


def _gitignore_has_entry(text: str, entry: str) -> bool:
    """``.gitignore`` 文本里是否已忽略 ``entry``。

    宽松匹配：忽略前导 ``/`` 与尾随 ``/``、行首尾空白，所以 ``.cli_control``、
    ``.cli_control/``、``/.cli_control/`` 视为同一条，不重复加。注释行（``#`` 开头）
    不算数 —— 用户把它注释掉就是想让它失效，真条目仍要补。
    """
    target = entry.strip().strip("/")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.strip("/") == target:
            return True
    return False


def _add_gitignore_entries(
    text: str, entries: tuple[str, ...] | list[str]
) -> tuple[str, list[str]]:
    """纯函数：把缺失的 ``entries`` 追加到 ``.gitignore`` 文本末尾。

    返回 ``(新文本, 实际新增的条目列表)``。已存在的条目跳过。追加前确保末尾
    有换行，避免把新条目黏到原末行后面；空文本不留前导空行。行尾统一用
    ``\\n``，CRLF 回写交给 IO 包装层（与 ``_patch_project_godot`` 同构）。
    """
    added: list[str] = []
    out = text
    for entry in entries:
        if _gitignore_has_entry(out, entry):
            continue
        if out and not out.endswith("\n"):
            out += "\n"
        out += entry + "\n"
        added.append(entry)
    return out, added


def _ensure_gitignore_entries(
    project_root: Path, entries: tuple[str, ...] = GITIGNORE_ENTRIES
) -> list[str]:
    """确保项目根 ``.gitignore`` 忽略 ``entries``；返回实际新增的条目。

    文件不存在则创建（LF）。已存在则沿用 ``_patch_project_godot`` 的 bytes+CRLF
    思路：读 bytes、探测原文是否 CRLF、解码时折成 LF 便于匹配，仅在确有新增时
    回写并把行尾翻回原样 —— 保 Windows 上 git diff 不被整文件行尾翻转污染。
    """
    path = project_root / GITIGNORE_NAME
    if path.exists():
        raw = path.read_bytes()
        is_crlf = b"\r\n" in raw
        text = raw.decode("utf-8").replace("\r\n", "\n")
    else:
        is_crlf = False
        text = ""

    new_text, added = _add_gitignore_entries(text, entries)
    if added:
        out = new_text.replace("\n", "\r\n") if is_crlf else new_text
        path.write_bytes(out.encode("utf-8"))
    return added
