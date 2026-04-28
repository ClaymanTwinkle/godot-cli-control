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

from .daemon import find_godot_binary

PLUGIN_DIR_NAME = "godot_cli_control"
ADDONS_DIRNAME = "addons"

AUTOLOAD_KEY = "GameBridgeNode"
AUTOLOAD_VALUE = '"*res://addons/godot_cli_control/bridge/game_bridge.gd"'
PLUGIN_CFG_PATH = "res://addons/godot_cli_control/plugin.cfg"


def run_init(project_root: Path, force: bool = False) -> int:
    """实施接入流程。返回进程 exit code。"""
    if not (project_root / "project.godot").is_file():
        print(
            f"错误：{project_root} 下没有 project.godot —— 不像 Godot 项目根。\n"
            "如果你确实在 Godot 项目内，请用 --path 指向项目根。",
            file=sys.stderr,
        )
        return 1

    plugin_src = locate_plugin_source()
    if plugin_src is None:
        print(
            "错误：找不到插件源（addons/godot_cli_control/）。\n"
            "如果是从源码 editable install，请确保仓库布局完整；\n"
            "如果是从 wheel 安装，包资源可能损坏，请重装。",
            file=sys.stderr,
        )
        return 1

    addons_dir = project_root / ADDONS_DIRNAME
    plugin_dst = addons_dir / PLUGIN_DIR_NAME
    if plugin_dst.exists():
        if force:
            shutil.rmtree(plugin_dst)
            _copy_plugin(plugin_src, plugin_dst)
            print(f"覆盖：{plugin_dst}")
        else:
            print(f"已存在：{plugin_dst}（用 --force 覆盖）")
    else:
        addons_dir.mkdir(parents=True, exist_ok=True)
        _copy_plugin(plugin_src, plugin_dst)
        print(f"复制：{plugin_src} → {plugin_dst}")

    patched, changes = _patch_project_godot(project_root / "project.godot")
    if changes:
        print(f"修改 project.godot：{', '.join(changes)}")
    elif patched:
        print("project.godot 已配置好（未改动）")

    godot_bin = find_godot_binary()
    if godot_bin:
        control_dir = project_root / ".cli_control"
        control_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(control_dir, 0o700)
        except OSError:
            pass
        (control_dir / "godot_bin").write_text(godot_bin + "\n")
        print(f"检测到 Godot：{godot_bin}（已写入 .cli_control/godot_bin）")
    else:
        print(
            "警告：未自动检测到 Godot 二进制。\n"
            "请 `export GODOT_BIN=/path/to/godot` 或写到 "
            ".cli_control/godot_bin。",
            file=sys.stderr,
        )

    print()
    print("已就绪。下一步：")
    print("  godot-cli-control daemon start          # 启动 daemon")
    print("  godot-cli-control tree 3                # 验证 RPC 通了")
    print("  godot-cli-control daemon stop           # 停止")
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
    """注入 ``[autoload]`` 与 ``[editor_plugins]`` 段；返回 (改动过, 变更列表)。"""
    text = path.read_text()
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
        path.write_text(text)
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
    line_pat = re.compile(
        r"^"
        + re.escape(key)
        + r"\s*=\s*PackedStringArray\((?P<inner>.*?)\)\s*$",
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
