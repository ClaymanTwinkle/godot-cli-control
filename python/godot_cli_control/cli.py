"""CLI commands for godot-cli-control.

子命令分三组：

* **Daemon 管理**：``daemon start`` / ``daemon stop`` / ``daemon status`` /
  ``run <script>`` —— 跨平台的 Godot 进程启停。
* **接入**：``init`` —— 在 Godot 项目根一键复制插件、patch ``project.godot``、
  检测 GODOT_BIN，并写出 .claude/.codex 的 SKILL.md。
* **RPC 单发**：12+ 子命令，覆盖客户端全部能力 —— 让 shell-only AI agent
  无需写 Python 脚本就能完成读 / 写 / 等待 / 发现操作。

输出契约（**默认 JSON**，AI 友好）：

* 成功：``{"ok": true, "result": <data>}`` 单行写 stdout，exit 0。
* 失败：``{"ok": false, "error": {"code": N, "message": "..."}}`` 单行写
  stdout，exit 1（RPC error）/ 2（连接、超时、IO）/ 64（用法错误）。
* ``--text`` / ``--no-json`` 切回旧的人类可读输出（CHANGELOG 提及兼容）。

子命令的退出码由各 spec 的 ``exit_code_from`` 决定（如 ``exists`` 0=true /
1=false / 2=error），未指定则 0=success / 1=rpc-error / 2=infra-error。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import DEFAULT_PORT, GameClient, RpcError

# ── 输出格式与退出码 ──

OUTPUT_JSON = "json"
OUTPUT_TEXT = "text"

EXIT_OK = 0
EXIT_RPC_ERROR = 1
EXIT_INFRA_ERROR = 2  # 连接 / 超时 / 用户输入解析失败
EXIT_USAGE = 64  # 命令组合无效（如 combo 既无文件又无 --steps-json）

# RPC 错误统一信封（无论 --json 还是 --text）的连接/超时占位 code。GD 端
# 自身的 code 都是正整数（-32601 等 JSON-RPC 标准码也用上了），所以用负数
# 区分客户端侧错误：避免 agent 拿到 code 时与服务端 code 撞概念。
CLIENT_CODE_CONNECTION = -1001
CLIENT_CODE_TIMEOUT = -1002
CLIENT_CODE_USAGE = -1003
CLIENT_CODE_IO = -1004  # 本地文件 IO 错误（screenshot 写盘等），与连接错误分开
CLIENT_CODE_INTERNAL = -1099  # 兜底：客户端内部异常（理论上不该到这里，但兜住契约）


# ── RpcSpec：声明一个 RPC 子命令 ──


@dataclass(frozen=True)
class Positional:
    """RPC 子命令的位置参数描述（喂给 argparse + 帮助文本）。"""

    name: str  # ns 上的属性名
    nargs: str | None  # None=必填一个；"?"=可选；"*"=0..N
    help: str


@dataclass(frozen=True)
class RpcSpec:
    """声明一个走 GameClient 的 RPC 子命令。

    ``handler`` 现在返回原始数据（dict / str / bool / list…），由顶层 dispatcher
    根据 ``--json`` / ``--text`` 决定信封。这样新增子命令只关心"调哪个 RPC、
    取什么字段返回"，不再每个都写 print。
    """

    name: str
    handler: Callable[[GameClient, argparse.Namespace], Coroutine[Any, Any, Any]]
    description: str
    positionals: tuple[Positional, ...] = ()
    example: str = ""
    extra_epilog: str = ""
    # 文本模式下渲染结果的回调。注：每个 RpcSpec 现在都显式覆盖；保留默认是为了
    # 防止未来新加 spec 忘了写 text_formatter 时崩溃。
    text_formatter: Callable[[Any], str] = lambda r: json.dumps(r, ensure_ascii=False)
    # 把 result 转成 exit code；返回 0 视为 success，非 0 即使 ok=true 也用作退出码。
    # 例：exists / visible / wait-node 用这个把布尔结果直通到 exit code。
    exit_code_from: Callable[[Any], int] | None = None
    # 额外 argparse 参数注册器（针对带 flag 或复杂位置参数的命令）。
    extra_args: Callable[[argparse.ArgumentParser], None] | None = None
    # 连 daemon **之前**跑的用法校验。抛 ValueError 立即给 EXIT_USAGE 信封，
    # 避免在 daemon 没起的场景下让 agent 干等 30s retry 才看到 "你 combo 没传 steps"。
    preflight: Callable[[argparse.Namespace], None] | None = None


# ── handler：读路径 / 输出 ──


def _parse_json_arg(raw: str) -> Any:
    """先按 JSON 解析；失败 fallback 当字符串字面量。

    让 ``set /root/Foo position '[10, 20]'`` 自然工作，又让
    ``set /root/Label text Hello`` 不必给字符串裹一层引号。
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _preflight_combo(ns: argparse.Namespace) -> None:
    """连 daemon 前用同一份解析逻辑校验 combo 输入；抛 ValueError 即用法错。

    把解析结果缓存到 ``ns._combo_steps``：stdin 只能读一次，handler 不能再读第二次。
    """
    ns._combo_steps = _read_combo_steps(ns)


def _read_combo_steps(ns: argparse.Namespace) -> list[dict]:
    """三选一读 combo steps：``--steps-json`` / 位置 ``-`` (stdin) / 文件路径。"""
    steps_json: str | None = getattr(ns, "steps_json", None)
    json_file: str | None = getattr(ns, "json_file", None)

    if steps_json is not None:
        if json_file is not None:
            raise ValueError(
                "combo: --steps-json 与位置参数 json_file 互斥；二选一"
            )
        raw = steps_json
    elif json_file == "-":
        if sys.stdin.isatty():
            # 没人 pipe 数据进来，read() 会无限阻塞——AI agent 不会触发，
            # 但人类用户敲错时表现为"卡住"。早抛 ValueError，让 preflight 报
            # EXIT_USAGE，比挂死好。
            raise ValueError(
                "combo -: stdin 是 TTY，没有可读内容；"
                "改用 `--steps-json '...'` 或 `combo file.json`"
            )
        raw = sys.stdin.read()
    elif json_file:
        raw = Path(json_file).read_text(encoding="utf-8")
    else:
        raise ValueError(
            "combo: 必须提供 json_file 路径、传 - 走 stdin、"
            "或加 --steps-json '...'"
        )

    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = parsed.get("steps", [])
    if not isinstance(parsed, list):
        raise ValueError("combo: steps 必须是 JSON 数组或 {steps: [...]} 对象")
    return parsed


# ── 现有 RPC 命令 handler（迁移到 ns 签名 + 返回数据） ──


async def cmd_click(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.click(ns.node_path)


async def cmd_screenshot(client: GameClient, ns: argparse.Namespace) -> dict:
    """截屏并写文件。``output_path`` 现在必填 —— base64 灌 stdout 的旧路径
    会撑爆 LLM 上下文，已删。

    展开 ``~`` 让 ``screenshot ~/foo.png`` 工作；不展开时 ``Path("~")`` 会
    创建字面 ``~`` 目录，是常见 footgun。
    """
    data = await client.screenshot()
    output = Path(ns.output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return {"path": str(output), "bytes": len(data)}


async def cmd_tree(client: GameClient, ns: argparse.Namespace) -> dict:
    depth = int(ns.depth) if ns.depth else 3
    return await client.get_scene_tree(depth=depth)


async def cmd_press(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.action_press(ns.action)


async def cmd_release(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.action_release(ns.action)


async def cmd_tap(client: GameClient, ns: argparse.Namespace) -> dict:
    duration = float(ns.duration) if ns.duration else 0.1
    return await client.action_tap(ns.action, duration)


async def cmd_hold(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.hold(ns.action, float(ns.duration))


async def cmd_combo(client: GameClient, ns: argparse.Namespace) -> dict:
    # preflight 已经解析过；走 ns._combo_steps 避免 stdin 二次读。
    steps = getattr(ns, "_combo_steps", None)
    if steps is None:
        steps = _read_combo_steps(ns)
    return await client.combo(steps)


async def cmd_release_all(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.release_all()


# ── 新增的 12 个 AI 友好命令 ──


async def cmd_get(client: GameClient, ns: argparse.Namespace) -> Any:
    """读节点属性，返回原值（字符串/数字/数组/对象/null）。"""
    return await client.get_property(ns.node_path, ns.prop)


async def cmd_set(client: GameClient, ns: argparse.Namespace) -> dict:
    """写节点属性。``value`` 先按 JSON 解析；失败退回字符串字面量。"""
    value = _parse_json_arg(ns.value)
    return await client.set_property(ns.node_path, ns.prop, value)


async def cmd_call(client: GameClient, ns: argparse.Namespace) -> Any:
    """调任意节点方法。每个 arg 同样 JSON-or-string 解析。"""
    raw_args: list[str] = list(ns.args or [])
    args = [_parse_json_arg(a) for a in raw_args]
    return await client.call_method(ns.node_path, ns.method, args)


async def cmd_text(client: GameClient, ns: argparse.Namespace) -> str:
    return await client.get_text(ns.node_path)


async def cmd_exists(client: GameClient, ns: argparse.Namespace) -> bool:
    return await client.node_exists(ns.node_path)


async def cmd_visible(client: GameClient, ns: argparse.Namespace) -> bool:
    return await client.is_visible(ns.node_path)


async def cmd_children(
    client: GameClient, ns: argparse.Namespace
) -> list[dict]:
    type_filter = ns.type_filter or ""
    return await client.get_children(ns.node_path, type_filter=type_filter)


async def cmd_wait_node(
    client: GameClient, ns: argparse.Namespace
) -> dict:
    timeout = float(ns.timeout) if ns.timeout else 5.0
    found = await client.wait_for_node(ns.node_path, timeout=timeout)
    return {"found": found, "path": ns.node_path, "timeout": timeout}


async def cmd_wait_time(
    client: GameClient, ns: argparse.Namespace
) -> dict:
    seconds = float(ns.seconds)
    return await client.wait_game_time(seconds)


async def cmd_pressed(
    client: GameClient, ns: argparse.Namespace
) -> list[str]:
    return await client.get_pressed()


async def cmd_combo_cancel(
    client: GameClient, ns: argparse.Namespace
) -> dict:
    return await client.combo_cancel()


async def cmd_actions(
    client: GameClient, ns: argparse.Namespace
) -> list[str]:
    return await client.list_input_actions(include_builtin=ns.all)


# ── text-mode 格式化 helper ──


def _fmt_lines(items: list[Any]) -> str:
    return "\n".join(str(x) for x in items)


def _fmt_children_text(items: list[dict]) -> str:
    return "\n".join(str(c.get("name", "")) for c in items)


def _fmt_bool_text(b: Any) -> str:
    return "true" if b else "false"


def _fmt_get_text(value: Any) -> str:
    """读到的属性：字符串原样输出，其它类型 JSON 序列化。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _fmt_tree_text(tree: Any) -> str:
    return json.dumps(tree, indent=2, ensure_ascii=False)


def _fmt_screenshot_text(r: dict) -> str:
    return f"screenshot saved: {r['path']} ({r['bytes']} bytes)"


def _fmt_wait_node_text(r: dict) -> str:
    return "found" if r["found"] else "timeout"


def _fmt_wait_time_text(r: dict) -> str:
    return f"waited (success={r.get('success', True)})"


# ── exit_code_from helpers ──


def _exit_from_bool(b: Any) -> int:
    return EXIT_OK if b else EXIT_RPC_ERROR


def _exit_from_wait_node(r: dict) -> int:
    return EXIT_OK if r.get("found") else EXIT_RPC_ERROR


# ── extra_args registrators ──


def _register_combo_args(p: argparse.ArgumentParser) -> None:
    """combo 的 args：``json_file`` 位置可选，加 ``--steps-json``。
    互斥校验在 ``_read_combo_steps`` 里做（argparse 里不能同时加 dest 冲突的
    位置参数和可选参数到 mutually_exclusive_group）。"""
    p.add_argument(
        "json_file",
        nargs="?",
        default=None,
        help="JSON 文件路径，或 ``-`` 从 stdin 读；可为 [...steps] 或 {\"steps\": [...]}",
    )
    p.add_argument(
        "--steps-json",
        default=None,
        help="直接传 JSON 字符串，不需要文件（与位置参数互斥）",
    )


def _register_actions_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--all",
        action="store_true",
        help="包含 ui_* 内置动作（默认仅项目自定义动作）",
    )


def _register_call_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("node_path", help="绝对节点路径，如 /root/Main")
    p.add_argument("method", help="节点上的方法名")
    p.add_argument(
        "args",
        nargs="*",
        help="方法参数；每个先按 JSON 解析失败 fallback 字符串",
    )


# ── RPC_SPECS 注册表 ──

RPC_SPECS: tuple[RpcSpec, ...] = (
    RpcSpec(
        name="click",
        handler=cmd_click,
        description="对 Control/Button 节点触发点击。",
        positionals=(
            Positional(
                "node_path",
                None,
                "绝对节点路径（必须以 /root/ 开头），如 /root/Main/StartButton",
            ),
        ),
        example="click /root/Main/StartButton",
        text_formatter=lambda r: f"clicked: {r}",
    ),
    RpcSpec(
        name="screenshot",
        handler=cmd_screenshot,
        description=(
            "截屏并写 PNG 文件。**路径必填**（旧版本可省、把 base64 喷到 "
            "stdout —— 已删，避免撑爆 AI 上下文）。"
        ),
        positionals=(
            Positional("output_path", None, "PNG 输出路径（必填）"),
        ),
        example="screenshot out.png",
        text_formatter=_fmt_screenshot_text,
    ),
    RpcSpec(
        name="tree",
        handler=cmd_tree,
        description="dump 当前场景树为 JSON。",
        positionals=(
            Positional("depth", "?", "遍历深度，默认 3"),
        ),
        example="tree 3",
        text_formatter=_fmt_tree_text,
    ),
    RpcSpec(
        name="press",
        handler=cmd_press,
        description="按下输入动作（持续按住，需配 release 释放）。",
        positionals=(
            Positional("action", None, "InputMap 动作名，如 jump"),
        ),
        example="press jump",
        text_formatter=lambda r: f"pressed: {r}",
    ),
    RpcSpec(
        name="release",
        handler=cmd_release,
        description="释放之前 press 按下的输入动作。",
        positionals=(
            Positional("action", None, "InputMap 动作名"),
        ),
        example="release jump",
        text_formatter=lambda r: f"released: {r}",
    ),
    RpcSpec(
        name="tap",
        handler=cmd_tap,
        description="短按动作（press → 等待 → release）。",
        positionals=(
            Positional("action", None, "InputMap 动作名"),
            Positional("duration", "?", "按下时长（秒），默认 0.1"),
        ),
        example="tap jump 0.2",
        text_formatter=lambda r: f"tapped: {r}",
    ),
    RpcSpec(
        name="hold",
        handler=cmd_hold,
        description="按住动作指定时长（秒），到点自动释放。",
        positionals=(
            Positional("action", None, "InputMap 动作名"),
            Positional("duration", None, "按住时长（秒）"),
        ),
        example="hold jump 1.5",
        text_formatter=lambda r: f"holding: {r}",
    ),
    RpcSpec(
        name="combo",
        handler=cmd_combo,
        description=(
            "依次执行一段输入动作。三种喂法：位置 ``<file.json>`` / "
            "位置 ``-`` (stdin) / ``--steps-json '[...]'``。"
        ),
        positionals=(),  # combo 用 extra_args 自定义
        example="combo --steps-json '[{\"action\":\"jump\",\"duration\":0.2}]'",
        extra_args=_register_combo_args,
        preflight=_preflight_combo,
        text_formatter=lambda r: f"combo done: {r}",
        extra_epilog=(
            "step schema（每个 step 二选一，按数组顺序串行执行）:\n"
            "  {\"action\": \"<InputMap 动作名>\", \"duration\": <秒，默认 0.1>}\n"
            "      —— 按下 action，等 duration 秒后自动释放\n"
            "  {\"wait\": <秒>}\n"
            "      —— 不动作，纯等待\n"
            "\n"
            "最小可跑示例:\n"
            "  godot-cli-control combo --steps-json \\\n"
            "    '[{\"action\":\"jump\",\"duration\":0.2},{\"wait\":0.3},{\"action\":\"attack\"}]'\n"
            "\n"
            "或从文件读：\n"
            "  godot-cli-control combo combo.json\n"
            "\n"
            "或从 stdin 读：\n"
            "  cat combo.json | godot-cli-control combo -\n"
            "\n"
            "中途可用 release-all 终止。combo 运行期间任何 press / release /\n"
            "再开 combo 都会被服务端 1004 拒绝（不支持重叠按键）。"
        ),
    ),
    RpcSpec(
        name="release-all",
        handler=cmd_release_all,
        description="释放所有当前持有的输入动作。",
        positionals=(),
        example="release-all",
        text_formatter=lambda r: f"released all: {r}",
    ),
    # ── 新增：读 ──
    RpcSpec(
        name="get",
        handler=cmd_get,
        description="读节点属性。",
        positionals=(
            Positional("node_path", None, "绝对节点路径，如 /root/Main"),
            Positional("prop", None, "属性名，如 position / text / visible"),
        ),
        example="get /root/Main/Score text",
        text_formatter=_fmt_get_text,
    ),
    RpcSpec(
        name="set",
        handler=cmd_set,
        description="写节点属性。value 优先按 JSON 解析（数字/数组/对象），失败退回字符串。",
        positionals=(
            Positional("node_path", None, "绝对节点路径"),
            Positional("prop", None, "属性名"),
            Positional(
                "value",
                None,
                "JSON 字面量或字符串。例：'42' / '\"hello\"' / '[10, 20]' / 'hello'",
            ),
        ),
        example="set /root/Main/Score text \"42\"",
        text_formatter=lambda r: f"set: {r}",
    ),
    RpcSpec(
        name="call",
        handler=cmd_call,
        description=(
            "调节点方法。每个参数同 set：先 JSON 解析，失败退回字符串。"
            "返回值原样（同 ``get`` 渲染规则）。"
        ),
        positionals=(),  # 用 extra_args 注册（args 是 nargs='*'）
        example="call /root/Main start_game 1 \"easy\"",
        extra_args=_register_call_args,
        text_formatter=_fmt_get_text,
    ),
    RpcSpec(
        name="text",
        handler=cmd_text,
        description="读 Label / Button 的 text（get_text 的便捷形式）。",
        positionals=(
            Positional("node_path", None, "绝对节点路径"),
        ),
        example="text /root/Main/Title",
        text_formatter=lambda s: s,
    ),
    RpcSpec(
        name="exists",
        handler=cmd_exists,
        description=(
            "节点是否存在。退出码：0=true, 1=false, 2=连接/超时错误。"
            "shell ``if godot-cli-control exists /root/Foo; then …`` 可用。"
        ),
        positionals=(
            Positional("node_path", None, "绝对节点路径"),
        ),
        example="exists /root/Main/Boss",
        text_formatter=_fmt_bool_text,
        exit_code_from=_exit_from_bool,
    ),
    RpcSpec(
        name="visible",
        handler=cmd_visible,
        description="节点是否可见。退出码同 exists：0=true, 1=false, 2=infra error。",
        positionals=(
            Positional("node_path", None, "绝对节点路径"),
        ),
        example="visible /root/Main/Hud",
        text_formatter=_fmt_bool_text,
        exit_code_from=_exit_from_bool,
    ),
    RpcSpec(
        name="children",
        handler=cmd_children,
        description="列出节点的直接子节点（一层）。",
        positionals=(
            Positional("node_path", None, "绝对节点路径"),
            Positional("type_filter", "?", "可选类型过滤，如 Button / Label"),
        ),
        example="children /root/Main",
        text_formatter=_fmt_children_text,
    ),
    RpcSpec(
        name="wait-node",
        handler=cmd_wait_node,
        description=(
            "轮询直到节点出现（或 timeout）。退出码：0=found, 1=timeout, "
            "2=infra error。"
        ),
        positionals=(
            Positional("node_path", None, "绝对节点路径"),
            Positional("timeout", "?", "超时秒，默认 5"),
        ),
        example="wait-node /root/Main/StartButton 5",
        text_formatter=_fmt_wait_node_text,
        exit_code_from=_exit_from_wait_node,
    ),
    RpcSpec(
        name="wait-time",
        handler=cmd_wait_time,
        description="按 game time 等待 N 秒（在 --write-movie 模式下与录像帧对齐）。",
        positionals=(
            Positional("seconds", None, "等待秒数（>0）"),
        ),
        example="wait-time 0.5",
        text_formatter=_fmt_wait_time_text,
    ),
    RpcSpec(
        name="pressed",
        handler=cmd_pressed,
        description="列出当前模拟器持有的输入动作（press + held 去重合并）。",
        positionals=(),
        example="pressed",
        text_formatter=_fmt_lines,
    ),
    RpcSpec(
        name="combo-cancel",
        handler=cmd_combo_cancel,
        description="取消正在运行的 combo（不影响 press/hold）。",
        positionals=(),
        example="combo-cancel",
        text_formatter=lambda r: f"cancelled: {r}",
    ),
    RpcSpec(
        name="actions",
        handler=cmd_actions,
        description=(
            "列出运行项目的 InputMap 动作。默认过滤 ui_* 内置；加 ``--all`` 看全。"
        ),
        positionals=(),
        example="actions",
        extra_args=_register_actions_flag,
        text_formatter=_fmt_lines,
    ),
)


RPC_BY_NAME: dict[str, RpcSpec] = {s.name: s for s in RPC_SPECS}


# ── Daemon / run / init 子命令 ──


def cmd_daemon_start(ns: argparse.Namespace) -> int:
    from .daemon import Daemon, DaemonError

    daemon = Daemon(Path.cwd())
    try:
        daemon.start(
            record=ns.record,
            movie_path=ns.movie_path,
            headless=ns.headless,
            fps=ns.fps,
            port=ns.port,
        )
    except DaemonError as e:
        _emit_top_error(ns, code=CLIENT_CODE_USAGE, message=str(e))
        return EXIT_INFRA_ERROR
    if _output_format(ns) == OUTPUT_JSON:
        # 跟 daemon status 的信封形状对齐，方便 agent 一套 jq 处理三个命令。
        # 注：``daemon.start()`` 已阻塞到端口可用，所以 is_running 此刻为 true
        # 是稳态。理论上若 daemon 在 start 返回后被外部 kill，read_pid 与
        # is_running 之间存在亚毫秒级 race；窗口够小可以忽略。
        port = daemon.current_port() or ns.port
        pid = daemon.read_pid() if daemon.is_running() else None
        result: dict[str, Any] = {"started": True, "port": port}
        if pid is not None:
            result["pid"] = pid
        _emit_success_payload(result)
    return EXIT_OK


def cmd_daemon_stop(ns: argparse.Namespace) -> int:
    from .daemon import Daemon, DaemonError

    daemon = Daemon(Path.cwd())
    try:
        rc = daemon.stop()
    except DaemonError as e:
        _emit_top_error(ns, code=CLIENT_CODE_USAGE, message=str(e))
        return EXIT_INFRA_ERROR
    if _output_format(ns) == OUTPUT_JSON:
        # rc 0=正常停 / 2=ffmpeg 转码失败但 daemon 已停。两种都算"stopped"，
        # 把 rc 透出让 agent 决定要不要 retry transcode。
        _emit_success_payload({"stopped": True, "rc": rc})
    return rc


def cmd_daemon_status(ns: argparse.Namespace) -> int:
    """打印 daemon 状态；exit 0=运行中，1=未运行。

    JSON 模式（默认）：
      ``{"ok": true, "result": {"state": "running", "pid": <int>, "port": <int>}}``
      / ``{"ok": true, "result": {"state": "stopped"}}``
    Text 模式：
      ``running pid=<pid> port=<port>`` / ``stopped``

    退出码语义不变（shell ``if godot-cli-control daemon status; then …`` 仍能用）。
    """
    from .daemon import Daemon

    fmt = _output_format(ns)
    daemon = Daemon(Path.cwd())
    if daemon.is_running():
        pid = daemon.read_pid()
        port = daemon.current_port()
        if fmt == OUTPUT_JSON:
            payload: dict[str, Any] = {"state": "running", "pid": pid}
            if port is not None:
                payload["port"] = port
            _emit_success_payload(payload)
        else:
            print(
                f"running pid={pid} port={port if port is not None else '?'}"
            )
        return EXIT_OK
    # Stopped：若上一轮启动留下了 godot.log，把路径透出来。issue #38 要求
    # 在 daemon 已退出时让用户能直接看到日志位置而不是手摸 .cli_control/。
    stopped_payload: dict[str, Any] = {"state": "stopped"}
    if daemon.log_file.exists():
        stopped_payload["last_log"] = str(daemon.log_file)
    if fmt == OUTPUT_JSON:
        _emit_success_payload(stopped_payload)
    else:
        if "last_log" in stopped_payload:
            print(f"stopped (last log: {stopped_payload['last_log']})")
        else:
            print("stopped")
    return EXIT_RPC_ERROR


def cmd_run(ns: argparse.Namespace) -> int:
    """加载用户脚本（要求定义 ``run(bridge)``），自动启停 daemon。"""
    from .daemon import Daemon, DaemonError

    script_path = Path(ns.script)
    if not script_path.exists():
        print(f"错误：找不到脚本: {script_path}", file=sys.stderr)
        return 1

    daemon = Daemon(Path.cwd())
    auto_started = False
    if not daemon.is_running():
        try:
            daemon.start(
                record=ns.record,
                movie_path=ns.movie_path,
                headless=ns.headless,
                fps=ns.fps,
                port=ns.port,
            )
        except DaemonError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
        auto_started = True

    port = daemon.current_port() or ns.port
    # try/finally 保护：_exec_user_script 内部抛非 catch 异常（GameBridge
    # 连接失败、importlib 边界错误等）时仍要停 daemon，避免 Godot 进程泄漏
    # 让下次 daemon start 报「already running」。
    exit_code = 1
    try:
        exit_code = _exec_user_script(script_path, port)
    finally:
        if auto_started:
            try:
                stop_rc = daemon.stop()
            except DaemonError as e:
                print(f"警告：停止 daemon 失败：{e}", file=sys.stderr)
                stop_rc = 1
            if exit_code == 0 and stop_rc != 0:
                exit_code = stop_rc
    return exit_code


def _exec_user_script(script_path: Path, port: int) -> int:
    """加载脚本模块、调用 ``run(bridge)``，捕获错误返回 exit code。"""
    import importlib.util

    from .bridge import GameBridge

    spec = importlib.util.spec_from_file_location("user_script", script_path)
    if spec is None or spec.loader is None:
        print(f"错误：无法加载脚本: {script_path}", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    # 让脚本同目录的辅助模块（``from helpers import foo``）可被解析；
    # 同时把 module 注册到 ``sys.modules`` 让 dataclass / pickle 通过模块名
    # 反向查找 class 时不报 ``KeyError: 'user_script'``。pytest 在同一进程里
    # 反复调本函数，必须 finally 弹回去 —— 否则 sys.path 头部累积 N 个 tmp_path
    # 影响后续测试，sys.modules 也会留 stale 引用。
    inserted_path = str(script_path.parent.resolve())
    sys.path.insert(0, inserted_path)
    sys.modules["user_script"] = module
    bridge: GameBridge | None = None
    try:
        # 保留完整 traceback —— 用户脚本出错时调试信息比「错误：xxx」一行有用得多。
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - 用户脚本任何异常都要抓
            print(f"错误：加载脚本 {script_path} 失败：", file=sys.stderr)
            traceback.print_exc()
            return 1
        if not hasattr(module, "run"):
            print(
                f"错误：脚本 {script_path} 中缺少 run(bridge) 函数",
                file=sys.stderr,
            )
            return 1

        print(f"运行 {script_path}...", file=sys.stderr)
        # GameBridge.__init__ 在连接 daemon 失败时抛 ConnectionError；不友好地
        # 走 cmd_run 默认 traceback 路径会让用户以为是脚本出错。单独捕获给
        # 一行可读信息（daemon 没起、端口写错、防火墙拦截 等）。
        try:
            bridge = GameBridge(port=port)
        except ConnectionError as e:
            print(
                f"错误：连接 daemon 失败 (port={port}): {e}\n"
                "提示：先运行 `godot-cli-control daemon start` 或检查端口是否被占用。",
                file=sys.stderr,
            )
            return 1
        try:
            module.run(bridge)
        except Exception:  # noqa: BLE001
            print(f"错误：脚本 {script_path} 运行失败：", file=sys.stderr)
            traceback.print_exc()
            return 1
        return 0
    finally:
        if bridge is not None:
            bridge.close()
        # 严格按"自己塞自己弹"的原则：sys.path[0] 仍是我们插的那条才弹，
        # 否则不动 —— 防止脚本里 user code 改了 sys.path[0] 后被错弹。
        if sys.path and sys.path[0] == inserted_path:
            sys.path.pop(0)
        sys.modules.pop("user_script", None)


def cmd_init(ns: argparse.Namespace) -> int:
    from .init_cmd import run_init

    return run_init(
        # 保留 .resolve()：run_init 内部用 relative_to(project_root) 打印 skill
        # 路径，相对路径会让 relative_to 在 cwd 不寻常时抛 ValueError。
        project_root=(Path(ns.path).resolve() if ns.path else Path.cwd()),
        force=ns.force,
        write_skills=not ns.no_skills,
        skills_only=ns.skills_only,
        clobber_skills=not ns.skills_no_clobber,
    )


# ── 输出信封 ──


def _output_format(ns: argparse.Namespace) -> str:
    """从 ns 取 ``output_format``；缺省（直接 ``Namespace()`` 调测试）回退 json。"""
    return getattr(ns, "output_format", OUTPUT_JSON) or OUTPUT_JSON


def _emit_success_payload(result: Any) -> None:
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))


def _emit_error_payload(code: int, message: str) -> None:
    print(
        json.dumps(
            {"ok": False, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        )
    )


def _emit_top_error(ns: argparse.Namespace, code: int, message: str) -> None:
    """daemon start/stop 这类非 RPC 命令出错时统一信封。"""
    if _output_format(ns) == OUTPUT_JSON:
        _emit_error_payload(code, message)
    else:
        print(f"错误：{message}", file=sys.stderr)


def _emit_rpc_result(spec: RpcSpec, fmt: str, result: Any) -> None:
    if fmt == OUTPUT_JSON:
        _emit_success_payload(result)
    else:
        text = spec.text_formatter(result)
        if text:
            print(text)


# ── argparse 装配 ──


_TOP_EPILOG = """\
命令分组：

  Daemon 管理:
    daemon start    启动 Godot daemon（可选录制 / headless）
    daemon stop     停止当前 daemon
    daemon status   显示 daemon 状态（pid / port），exit 0=运行中，1=未运行
    run <script>    自动启停 daemon 并跑用户脚本（脚本需定义 run(bridge)）

  接入:
    init            在 Godot 项目根一键复制插件、patch project.godot

  RPC 一发命令（需先 daemon 在跑）:
    读：     get / text / exists / visible / children / tree / pressed / actions
    写：     set / call / click
    输入：   press / release / tap / hold / combo / combo-cancel / release-all
    等待：   wait-node / wait-time
    截图：   screenshot

输出契约（默认 --json，AI 友好）:
  成功： {"ok": true, "result": <data>}        单行 stdout，exit 0
  失败： {"ok": false, "error": {"code":N,"message":"..."}}
                                              单行 stdout，exit 1（RPC）/ 2（连接、用法）
  --text / --no-json 可切回旧的人类可读模式。

任意子命令后追加 -h 查看详情，例如：
  godot-cli-control click -h
  godot-cli-control combo -h        # 含 step JSON schema 与示例
  godot-cli-control daemon start -h
"""


def _add_daemon_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--record",
        action="store_true",
        help="启动后录制 demo（写到 .cli_control/movie_path）",
    )
    p.add_argument(
        "--movie-path",
        default=None,
        help="demo 输出路径，默认 .cli_control 下自动命名",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="无窗口模式，CI/无显示器环境用",
    )
    p.add_argument("--fps", type=int, default=30, help="录制帧率，默认 30")
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"GameBridge 监听端口（默认 {DEFAULT_PORT}）",
    )


def _add_output_format_flags(p: argparse.ArgumentParser) -> None:
    """全局 ``--json`` / ``--text`` / ``--no-json``。

    默认 json 是 0.2.0 起的新行为（向 AI agent 倾斜）。``--no-json`` 是
    ``--text`` 的别名，方便顺手敲。
    """
    p.add_argument(
        "--json",
        dest="output_format",
        action="store_const",
        const=OUTPUT_JSON,
        default=OUTPUT_JSON,
        help="输出 JSON 信封（默认）",
    )
    p.add_argument(
        "--text",
        dest="output_format",
        action="store_const",
        const=OUTPUT_TEXT,
        help="输出旧的人类可读字符串（不再加信封；errors 走 stderr）",
    )
    p.add_argument(
        "--no-json",
        dest="output_format",
        action="store_const",
        const=OUTPUT_TEXT,
        help="--text 别名",
    )


def build_parser() -> argparse.ArgumentParser:
    from . import _version

    parser = argparse.ArgumentParser(
        prog="godot-cli-control",
        description="Godot CLI Control —— 通过命令行远程驱动 Godot 项目",
        epilog=_TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"godot-cli-control {getattr(_version, '__version__', 'unknown')}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"RPC 子命令连接的 GameBridge 端口（默认从 .cli_control/port 读取，"
            f"否则 {DEFAULT_PORT}）。注意：仅作用于 RPC 子命令，daemon "
            "start / run 启动 daemon 时请用其各自的 --port。"
        ),
    )
    _add_output_format_flags(parser)
    subs = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # daemon 组
    daemon_p = subs.add_parser(
        "daemon",
        help="管理 Godot daemon 进程",
        description="管理 Godot daemon 进程的启停与状态查询。",
    )
    daemon_subs = daemon_p.add_subparsers(
        dest="action", required=True, metavar="<action>"
    )

    start_p = daemon_subs.add_parser(
        "start",
        help="启动 daemon",
        description="启动 Godot daemon 并写入 .cli_control/{godot.pid,port}。",
    )
    _add_daemon_flags(start_p)

    daemon_subs.add_parser(
        "stop",
        help="停止 daemon",
        description="停止 .cli_control/godot.pid 记录的 daemon。",
    )

    daemon_subs.add_parser(
        "status",
        help="查询 daemon 状态",
        description=(
            "打印 daemon 状态到 stdout 并以 exit code 表示："
            "0 = 运行中（输出 running pid=<pid> port=<port>），"
            "1 = 未运行（输出 stopped）。"
            "默认输出 JSON 信封；加 --text 切回旧的字符串格式。"
        ),
    )

    # run：自动启停 + 跑用户脚本
    run_p = subs.add_parser(
        "run",
        help="启动 daemon → 跑脚本 → 停 daemon",
        description=(
            "若 daemon 未运行则先启动，加载用户脚本调用其 run(bridge) 函数，"
            "脚本结束后停掉刚启动的 daemon（已在跑的 daemon 保持原状）。"
            "脚本里抛任何异常都会以 exit code 1 退出并打印 traceback。"
        ),
        epilog=(
            "脚本示例 (my_script.py):\n"
            "  def run(bridge):\n"
            "      bridge.wait_for_node(\"/root/Main/StartButton\", timeout=5)\n"
            "      bridge.click(\"/root/Main/StartButton\")\n"
            "      bridge.wait(0.5)\n"
            "      assert bridge.get_text(\"/root/Main/Score\") == \"0\"\n"
            "\n"
            "bridge 是 GameClient 的同步包装，方法名一致、无需 await。\n"
            "脚本同目录的兄弟模块（from helpers import foo）可正常 import。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_p.add_argument("script", help="用户脚本路径，需定义 run(bridge)")
    _add_daemon_flags(run_p)

    # init：一键接入
    init_p = subs.add_parser(
        "init",
        help="在 Godot 项目根一键接入插件",
        description=(
            "复制 addons/godot_cli_control 到目标项目、patch project.godot 启用插件、"
            "校验 GODOT_BIN。"
        ),
        epilog=(
            "GODOT_BIN 查找顺序：\n"
            "  1. 环境变量 GODOT_BIN\n"
            "  2. 项目根 .cli_control/godot_bin 文件（init 检测到时会写入）\n"
            "  3. macOS /Applications/Godot*.app/Contents/MacOS/Godot\n"
            "  4. PATH 上的 godot4 / godot / Godot\n"
            "  5. Windows Program Files\\Godot*\\Godot*.exe\n"
            "都没找到时 init 会打 warning，daemon start 会直接报错。\n"
            "可以手动 `export GODOT_BIN=/path/to/godot` 或写到 .cli_control/godot_bin。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    init_p.add_argument(
        "--path",
        default=None,
        help="目标 Godot 项目根（默认当前目录）",
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 addons/godot_cli_control",
    )
    init_p.add_argument(
        "--skills-no-clobber",
        action="store_true",
        help=(
            "写 skill 时跳过已存在的 .claude/.codex SKILL.md（默认会覆盖以"
            "保证版本与 CLI 帮助同步）。与 --no-skills / --skills-only 都兼容。"
        ),
    )
    skills_group = init_p.add_mutually_exclusive_group()
    skills_group.add_argument(
        "--no-skills",
        action="store_true",
        help="跳过 .claude/.codex skill 写入",
    )
    skills_group.add_argument(
        "--skills-only",
        action="store_true",
        help="只写 skill 文件，跳过插件复制 / project.godot patch / godot_bin 检测",
    )

    # RPC 单发命令
    for spec in RPC_SPECS:
        epilog_parts: list[str] = []
        if spec.example:
            epilog_parts.append(f"示例:\n  godot-cli-control {spec.example}")
        if spec.extra_epilog:
            epilog_parts.append(spec.extra_epilog)
        epilog = "\n\n".join(epilog_parts) if epilog_parts else None
        sp = subs.add_parser(
            spec.name,
            help=spec.description,
            description=spec.description,
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        for pos in spec.positionals:
            kwargs: dict[str, Any] = {"help": pos.help}
            if pos.nargs is not None:
                kwargs["nargs"] = pos.nargs
            sp.add_argument(pos.name, **kwargs)
        if spec.extra_args is not None:
            spec.extra_args(sp)
    return parser


def format_full_help() -> str:
    """渲染顶层 + 所有子命令（含 daemon 三动作）的 help 文本。

    给 SKILL.md 模板用：把单一信息源塞进 ``{{cli_help}}``，让 agent 不必为
    了看 ``combo -h`` / ``daemon start -h`` 再 shell 出去。

    实现注意：argparse 的 subparsers action 通过 ``_actions`` 暴露，``choices``
    是 name → ArgumentParser 的 dict。深度仅两层（顶层 → daemon → start/stop/status；
    顶层 → 各 RPC 命令），所以递归两层就够。
    """
    parser = build_parser()
    sections: list[str] = []
    sections.append("$ godot-cli-control --help")
    sections.append(parser.format_help().rstrip())

    def _iter_subparsers(p: argparse.ArgumentParser):
        for action in p._actions:  # noqa: SLF001
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for name, sub in action.choices.items():
                    yield name, sub

    for name, sub in _iter_subparsers(parser):
        # 给 daemon 这种再嵌一层的命令多打一份顶层 help，再展开内部 action
        sections.append(f"\n$ godot-cli-control {name} --help")
        sections.append(sub.format_help().rstrip())
        for action_name, sub2 in _iter_subparsers(sub):
            sections.append(
                f"\n$ godot-cli-control {name} {action_name} --help"
            )
            sections.append(sub2.format_help().rstrip())

    return "\n".join(sections)


# ── 主入口 ──


def _is_network_oserror(e: OSError) -> bool:
    """区分网络层 OSError（连接拒、地址不可达）和本地文件 IO 错误。

    ``socket.gaierror`` 是 OSError 子类。``ConnectionError`` 也是 OSError 子类，
    但有自己的捕获分支，到这里时已经被 typing 排除。剩下的 raw OSError 多数
    是文件 IO（``PermissionError`` / ``FileNotFoundError``）—— 不该被误标成
    "连接失败"，否则 agent 会去重启 daemon 浪费一轮。
    """
    import socket
    if isinstance(e, socket.gaierror):
        return True
    # errno 范围粗划：ENETUNREACH(101) / EHOSTUNREACH(113) / ECONNREFUSED(111)
    # 等明确是网络。FileNotFoundError(2) / PermissionError(13) 是本地。
    network_errnos = {101, 113, 111, 110, 104, 32}  # net unreach / host unreach / refused / timed out / reset / pipe
    return e.errno in network_errnos


def _emit_envelope_error(fmt: str, code: int, message: str) -> None:
    """统一两种格式的错误输出口。"""
    if fmt == OUTPUT_JSON:
        _emit_error_payload(code, message)
    else:
        print(f"错误：[{code}] {message}", file=sys.stderr)


async def _run_rpc(
    spec: RpcSpec, ns: argparse.Namespace, port: int, fmt: str
) -> int:
    """启 client → 调 handler → 发信封 → 算 exit code。

    所有 ``Exception`` 子类在这里收口成 JSON 信封，绝不让 traceback 漏到
    stdout —— ``--json`` 默认开后这是契约的一部分。``KeyboardInterrupt`` /
    ``SystemExit`` (BaseException 但非 Exception) 仍正常传播，CTRL-C 退出
    保持惯常体验。
    """
    try:
        async with GameClient(port=port) as client:
            result = await spec.handler(client, ns)
    except RpcError as e:
        _emit_envelope_error(fmt, e.code, e.message)
        return EXIT_RPC_ERROR
    except ConnectionError as e:
        msg = str(e) or e.__class__.__name__
        _emit_envelope_error(fmt, CLIENT_CODE_CONNECTION, msg)
        return EXIT_INFRA_ERROR
    except asyncio.TimeoutError as e:
        msg = str(e) or "timed out"
        _emit_envelope_error(fmt, CLIENT_CODE_TIMEOUT, msg)
        return EXIT_INFRA_ERROR
    except OSError as e:
        # 拆开：socket 层 OSError 还算 connection；文件 IO 类（PermissionError /
        # FileNotFoundError，screenshot 写盘失败时常见）走独立的 IO 码，
        # 不让 agent 误以为 daemon 挂了。
        code = (
            CLIENT_CODE_CONNECTION
            if _is_network_oserror(e)
            else CLIENT_CODE_IO
        )
        msg = str(e) or e.__class__.__name__
        _emit_envelope_error(fmt, code, msg)
        return EXIT_INFRA_ERROR
    except (ValueError, json.JSONDecodeError) as e:
        # 用法错误：combo 没文件没 inline、set value 解析失败……
        _emit_envelope_error(fmt, CLIENT_CODE_USAGE, str(e))
        return EXIT_INFRA_ERROR
    except Exception as e:  # noqa: BLE001
        # 兜底：客户端内部 bug（AttributeError、KeyError、协议解析意外等）
        # 也要落进信封，否则 ``--json`` 契约对 AI agent 不再可信。把异常类
        # 名带上方便 issue 复现，但不把 traceback 塞进 message —— stdout JSON
        # 不是吐 traceback 的地方。完整 traceback 仍留给 stderr 帮人 debug。
        traceback.print_exc(file=sys.stderr)
        msg = f"{type(e).__name__}: {e}"
        _emit_envelope_error(fmt, CLIENT_CODE_INTERNAL, msg)
        return EXIT_INFRA_ERROR

    _emit_rpc_result(spec, fmt, result)
    if spec.exit_code_from is not None:
        return spec.exit_code_from(result)
    return EXIT_OK


def main() -> None:
    parser = build_parser()
    ns = parser.parse_args()

    if ns.cmd == "daemon":
        if ns.action == "start":
            sys.exit(cmd_daemon_start(ns))
        if ns.action == "stop":
            sys.exit(cmd_daemon_stop(ns))
        if ns.action == "status":
            sys.exit(cmd_daemon_status(ns))
        parser.error(f"unknown daemon action: {ns.action}")
    if ns.cmd == "run":
        sys.exit(cmd_run(ns))
    if ns.cmd == "init":
        sys.exit(cmd_init(ns))

    # RPC：解析端口（顶层 --port → port file → 默认）
    if ns.cmd in RPC_BY_NAME:
        spec = RPC_BY_NAME[ns.cmd]
        fmt = _output_format(ns)

        # preflight：连 daemon 之前的用法校验（如 combo 没传 steps）。让 agent
        # 立刻看到 EXIT_USAGE 信封，不必等 30s connection retry。
        if spec.preflight is not None:
            try:
                spec.preflight(ns)
            except (ValueError, json.JSONDecodeError) as e:
                if fmt == OUTPUT_JSON:
                    _emit_error_payload(CLIENT_CODE_USAGE, str(e))
                else:
                    print(f"错误：{e}", file=sys.stderr)
                sys.exit(EXIT_USAGE)

        port = ns.port
        if port is None:
            from .daemon import Daemon

            port = Daemon(Path.cwd()).current_port() or DEFAULT_PORT

        rc = asyncio.run(_run_rpc(spec, ns, port, fmt))
        sys.exit(rc)

    parser.error(f"unknown command: {ns.cmd}")


if __name__ == "__main__":
    main()
