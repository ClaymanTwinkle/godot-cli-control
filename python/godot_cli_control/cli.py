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
import base64
import contextlib
import json
import sys
import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from .client import DEFAULT_PORT, GameClient, RpcError

# ── 输出格式与退出码 ──

OUTPUT_JSON = "json"
OUTPUT_TEXT = "text"

EXIT_OK = 0
EXIT_RPC_ERROR = 1
EXIT_INFRA_ERROR = 2  # 连接 / 超时 / 用户输入解析失败
# `daemon stop --all` 专用：至少一条 stop 失败。不复用 EXIT_INFRA_ERROR(=2) —— 单个项目
# stop rc=2 是「daemon 已停但 ffmpeg 转码失败」的合法成功旁路；--all 聚合若也用 2，
# 调用方分不清「全停成功只是某个 transcode 失败」与「真有 daemon 没停掉」。
EXIT_PARTIAL = 3  # 聚合操作（daemon stop --all / --instance all 广播）部分或全部目标失败
EXIT_USAGE = 64  # 命令组合无效（如 combo 既无文件又无 --steps-json）

# RPC 错误统一信封（无论 --json 还是 --text）的连接/超时占位 code。GD 端
# 自身的 code 都是正整数（-32601 等 JSON-RPC 标准码也用上了），所以用负数
# 区分客户端侧错误：避免 agent 拿到 code 时与服务端 code 撞概念。
CLIENT_CODE_CONNECTION = -1001
CLIENT_CODE_TIMEOUT = -1002
CLIENT_CODE_USAGE = -1003
CLIENT_CODE_IO = -1004  # 本地文件 IO 错误（screenshot 写盘等），与连接错误分开
CLIENT_CODE_SCRIPT_ERROR = -1005  # `run <script>` 用户脚本抛出未捕获异常（agent 错的是脚本，不是 CLI 框架）
CLIENT_CODE_PRECONDITION = -1006  # infra 前置失败（daemon 起不来、脚本不可访问等），恒 exit 2；与「-1003 恒 64」双射互补
CLIENT_CODE_INTERNAL = -1099  # 兜底：客户端内部异常（理论上不该到这里，但兜住契约）


def _resolve_headless(
    ns: argparse.Namespace, *, force_gui_hint: bool = False
) -> bool:
    """决定本次 daemon start 是否走 --headless。

    优先级：显式 --headless > 显式 --gui > force_gui_hint > stdout.isatty() 自动判。
    isatty=False（pipe / redirect / 非 TTY agent shell）默认 headless；
    isatty=True（开发者交互终端）默认开窗。

    ``force_gui_hint``：调用方（``cmd_run``）静态检测脚本含 ``screenshot`` 时传
    True，让没显式指定 ``--headless`` / ``--gui`` 的非 TTY 场景（subagent / pipe）
    自动改走 GUI —— headless 下 dummy renderer 拿不到 viewport texture，screenshot
    永远 1006 fail（issue #65）。

    ``--record`` 同理且更狠：Movie Maker 的 ``add_frame()`` 读的也是 viewport
    texture，headless 下拿到 null 直接 SIGSEGV（CultivationWorld #180）。所以没显式
    ``--headless`` 时一律翻成 GUI；显式 ``--headless`` 仍返回 True，由
    ``daemon.start`` 的 preflight 拒绝（明确用法错，不静默改写用户意图）。
    """
    if getattr(ns, "headless", False):
        return True
    if getattr(ns, "gui", False):
        return False
    if getattr(ns, "record", False):
        return False
    if force_gui_hint:
        return False
    try:
        return not sys.stdout.isatty()
    except (OSError, ValueError):
        # ValueError: I/O operation on closed file
        # OSError: 底层 fd 失效（罕见，譬如 fd 被 close 但对象未刷新）
        return True  # 安全默认


def _script_likely_uses_screenshot(script_path: Path) -> bool:
    """启发式：脚本源码是否含 ``screenshot`` 子串。

    保守策略 —— 误报（多开一次窗）成本远低于漏报（截图静默 1006 fail）。
    简单子串足以覆盖 ``bridge.screenshot(...)`` / ``client.screenshot()`` /
    间接 ``getattr(bridge, "screenshot")``；comment / docstring 命中也无所谓，
    最坏只是 subagent 多看到一个窗口。

    读不到（OSError / decode 失败）时返回 False —— 让 cmd_run 走原来的 isatty
    默认，至少不会把"脚本不存在"的报错被本检测吞掉。
    """
    try:
        text = script_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "screenshot" in text


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


def _resolve_value_for_set(ns: argparse.Namespace) -> Any:
    """根据 --text-value 决定是 JSON-or-string 解析还是直接 string。"""
    if getattr(ns, "text_value", False):
        return ns.value
    return _parse_json_arg(ns.value)


def _resolve_args_for_call(ns: argparse.Namespace) -> list:
    raw_args: list[str] = list(ns.args or [])
    if getattr(ns, "text_value", False):
        return raw_args
    return [_parse_json_arg(a) for a in raw_args]


def _preflight_hold(ns: argparse.Namespace) -> None:
    """连 daemon 前校验 hold 的 duration：必须是 > 0 的数字。

    duration <= 0 会让动作下一帧就释放（只生效一帧），是无意义用法；
    要无限按住应该用 ``press``。preflight 拦住，避免 agent 干等连接重试。
    """
    try:
        duration = float(ns.duration)
    except (TypeError, ValueError):
        raise ValueError(f"hold: duration 必须是数字，收到 {ns.duration!r}")
    if duration <= 0:
        raise ValueError(
            f"hold: duration 必须 > 0（秒），收到 {duration}；要无限按住请用 `press`"
        )


def _preflight_combo(ns: argparse.Namespace) -> None:
    """连 daemon 前用同一份解析逻辑校验 combo 输入；抛 ValueError 即用法错。

    把解析结果缓存到 ``ns._combo_steps``：stdin 只能读一次，handler 不能再读第二次。
    """
    ns._combo_steps = _read_combo_steps(ns)


def _preflight_screenshot(ns: argparse.Namespace) -> None:
    """广播（--instance all）下 output_path 必须带 ``{instance}`` 占位符（#145）。

    多实例写同一路径会互相覆盖且不报错——典型静默坑，preflight 拦在连接前
    （契约 #5）。非广播模式零约束。
    """
    if getattr(ns, "instance", None) == "all" and "{instance}" not in ns.output_path:
        raise ValueError(
            "broadcast screenshot：output_path 必须包含 {instance} 占位符"
            "（如 shot-{instance}.png），否则各实例写同一文件互相覆盖"
        )


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


_WAIT_PROP_OPS = ("eq", "ne", "gt", "lt", "ge", "le")


def _require_float(raw: Any, cmd: str, field: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{cmd}: {field} 必须是数字，收到 {raw!r}")


def _require_frames(raw: Any, cmd: str) -> int:
    """frames 参数校验：整数 + 1..3600（wait-frames / step-frames 共用）。"""
    try:
        frames = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{cmd}: frames 必须是整数，收到 {raw!r}")
    if not 1 <= frames <= 3600:
        raise ValueError(f"{cmd}: frames 必须在 1..3600，收到 {frames}")
    return frames


def _preflight_wait_prop(ns: argparse.Namespace) -> None:
    timeout = _require_float(ns.timeout, "wait-prop", "timeout")
    if not 0 <= timeout <= 3600:
        raise ValueError(f"wait-prop: timeout 必须在 0..3600 秒，收到 {timeout}")
    tolerance = _require_float(ns.tolerance, "wait-prop", "tolerance")
    if tolerance < 0:
        raise ValueError(f"wait-prop: tolerance 必须 >= 0，收到 {tolerance}")
    expected = _parse_json_arg(ns.value)
    is_numeric = isinstance(expected, (int, float)) and not isinstance(expected, bool)
    if ns.op not in ("eq", "ne") and not is_numeric:
        raise ValueError(
            f"wait-prop: --op {ns.op} 只支持数值比较；复合/字符串/bool 值只能用 eq/ne"
        )


def _preflight_wait_signal(ns: argparse.Namespace) -> None:
    if ns.timeout is None:
        return
    timeout = _require_float(ns.timeout, "wait-signal", "timeout")
    if not 0 <= timeout <= 3600:
        raise ValueError(f"wait-signal: timeout 必须在 0..3600 秒，收到 {timeout}")


def _preflight_wait_frames(ns: argparse.Namespace) -> None:
    _require_frames(ns.frames, "wait-frames")


def _preflight_scene_reload(ns: argparse.Namespace) -> None:
    timeout = _require_float(ns.timeout, "scene-reload", "timeout")
    if not 0 < timeout <= 3600:
        raise ValueError(f"scene-reload: timeout 必须 > 0 且 <= 3600 秒，收到 {timeout}")


def _preflight_scene_change(ns: argparse.Namespace) -> None:
    if not ns.scene_path.startswith(("res://", "uid://")):
        raise ValueError(
            f"scene-change: 场景路径必须以 res:// 或 uid:// 开头，收到 {ns.scene_path!r}"
        )
    timeout = _require_float(ns.timeout, "scene-change", "timeout")
    if not 0 < timeout <= 3600:
        raise ValueError(f"scene-change: timeout 必须 > 0 且 <= 3600 秒，收到 {timeout}")


def _preflight_time_scale(ns: argparse.Namespace) -> None:
    if ns.value is None:
        return  # 无参 = 读当前值，无需校验
    v = _require_float(ns.value, "time-scale", "value")
    if not 0 < v <= 100:
        raise ValueError(
            f"time-scale: value 必须 > 0 且 <= 100，收到 {v}（要冻结游戏用 pause，别用 0）"
        )


def _preflight_step_frames(ns: argparse.Namespace) -> None:
    _require_frames(ns.frames, "step-frames")


def _preflight_tree(ns: argparse.Namespace) -> None:
    """tree 位置参数消歧 + depth 校验，结果 stash 到 ns（连 daemon 前跑，issue #150）。

    第一个位置参数以 / 开头 → 当 node path（子树根，从 /root 解析）；否则当 depth。
    depth-only 形式不接受第二个位置参数；漏斜杠的路径（如 ``tree GameUI``）会被当
    depth 解析失败而 fail-loud，不静默吞掉。
    """
    arg1 = ns.tree_arg1
    arg2 = ns.tree_arg2
    if arg1 is not None and arg1.startswith("/"):
        path: str | None = arg1
        depth_token = arg2
    else:
        path = None
        depth_token = arg1
        if arg2 is not None:
            raise ValueError(
                f"tree: 多余的参数 {arg2!r}；节点路径须以 / 开头"
                f"（如 tree /root/GameUI 2），否则只接受单个 depth"
            )
    depth = 3
    if depth_token is not None:
        try:
            depth = int(depth_token)
        except (TypeError, ValueError):
            raise ValueError(
                f"tree: depth 必须是整数，收到 {depth_token!r}"
                f"（要查子树请用绝对路径，如 tree /root/GameUI）"
            )
        if depth < 0:
            raise ValueError(f"tree: depth 必须 >= 0，收到 {depth}")
    ns._tree_path = path
    ns._tree_depth = depth


def _combo_total_duration(steps: list[dict]) -> float:
    """combo 全部 step 的累计 game-time（给 ``--wait`` 算阻塞时长，issue #90）。

    step schema 与服务端一致：``{"action": ..., "duration": <默认 0.1>}`` 串行
    按下/释放，``{"wait": <秒>}`` 纯等待。非数字 / 缺字段按默认 0.1 兜底，绝不
    抛错——``--wait`` 是体验增强，估时偏差远好过让命令崩掉。
    """
    total = 0.0
    for step in steps:
        if not isinstance(step, dict):
            continue
        raw = step.get("wait", step.get("duration", 0.1))
        try:
            total += float(raw)
        except (TypeError, ValueError):
            total += 0.1
    return total


async def _maybe_block_for_duration(
    client: GameClient, ns: argparse.Namespace, duration: float
) -> None:
    """``--wait``：输入命令发出后，阻塞 ``duration`` 的 game-time 再返回（issue #90）。

    把 SKILL.md 推荐的「读动作完成后状态前先 wait-time <时长>」折叠进同一条命令、
    复用同一连接：需要同步的 agent 一步到位拿到结算后状态。默认（不传 ``--wait``）
    保持异步立即返回，sticky + timer 模型不变。``wait_game_time`` 在 ≤0 时本就短路，
    这里也提前挡掉，省一次 RPC 往返。
    """
    if getattr(ns, "wait", False) and duration > 0:
        await client.wait_game_time(duration)


# ── 现有 RPC 命令 handler（迁移到 ns 签名 + 返回数据） ──


async def cmd_click(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.click(ns.node_path)


async def cmd_screenshot(client: GameClient, ns: argparse.Namespace) -> dict:
    """截屏并写文件。``output_path`` 现在必填 —— base64 灌 stdout 的旧路径
    会撑爆 LLM 上下文，已删。

    展开 ``~`` 让 ``screenshot ~/foo.png`` 工作；不展开时 ``Path("~")`` 会
    创建字面 ``~`` 目录，是常见 footgun。

    ``--node``（issue #101）：按节点屏幕 AABB 裁剪小图；信封带回实际裁剪
    region（视口像素坐标）便于排查「裁到的不是我想的区域」。

    落盘走 daemon 直写（issue #149）：daemon 与 CLI 必然同机（localhost-only
    是安全前提），PNG 由 daemon 进程直接写到绝对路径（两进程 CWD 不同，
    必须 resolve），base64 不再过 WS——hiDPI 全屏大图曾撞 1MB 消息上限被
    close 1009，误报 -1001 连接错。父目录仍由 CLI 创建。旧 addon 不认 path
    参数、照旧回 base64 → 本地解码落盘兜底（版本错位窗口的优雅降级，跑
    一次 ``init`` 即同步 addon）。
    """
    node: str | None = getattr(ns, "node", None)
    output = Path(ns.output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = await client.screenshot_raw(node, path=str(output.resolve()))
    if "image" in raw:  # 旧 addon 回退：base64 过 WS，本地解码落盘
        data = base64.b64decode(raw["image"])
        output.write_bytes(data)
        nbytes = len(data)
    else:
        nbytes = int(raw["bytes"])
    result: dict = {"path": str(output), "bytes": nbytes}
    if node:
        result["node"] = node
        result["region"] = raw.get("region")
    return result


async def cmd_sprite_info(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.sprite_info(ns.node_path)


async def cmd_errors(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.errors(since=ns.since, limit=ns.limit)


async def cmd_tree(client: GameClient, ns: argparse.Namespace) -> dict:
    # ns._tree_path / ns._tree_depth 由 _preflight_tree 解析填入（连 daemon 前）。
    return await client.get_scene_tree(
        depth=ns._tree_depth, max_nodes=ns.max_nodes, path=ns._tree_path
    )


async def cmd_press(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.action_press(ns.action)


async def cmd_release(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.action_release(ns.action)


async def cmd_tap(client: GameClient, ns: argparse.Namespace) -> dict:
    duration = float(ns.duration) if ns.duration else 0.1
    result = await client.action_tap(ns.action, duration)
    await _maybe_block_for_duration(client, ns, duration)
    return result


async def cmd_hold(client: GameClient, ns: argparse.Namespace) -> dict:
    duration = float(ns.duration)
    result = await client.hold(ns.action, duration)
    await _maybe_block_for_duration(client, ns, duration)
    return result


async def cmd_combo(client: GameClient, ns: argparse.Namespace) -> dict:
    # preflight 已经解析过；走 ns._combo_steps 避免 stdin 二次读。
    steps = getattr(ns, "_combo_steps", None)
    if steps is None:
        steps = _read_combo_steps(ns)
    result = await client.combo(steps)
    await _maybe_block_for_duration(client, ns, _combo_total_duration(steps))
    return result


async def cmd_release_all(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.release_all()


# ── 新增的 12 个 AI 友好命令 ──


async def cmd_get(client: GameClient, ns: argparse.Namespace) -> dict:
    """读节点属性（1 个或多个，支持 sub-path 如 position:x）。

    透传 RPC result（带 type 字段）——client.get_property 的裸 value 是给
    Python 脚本的便捷层；CLI 信封必须带 type 给 agent 消歧（issue #99/#100）。
    单属性 result={"value", "type"?}；多属性 result={"values": {...}}。
    """
    props: list[str] = ns.props
    if len(props) == 1:
        return await client.request(
            "get_property", {"path": ns.node_path, "property": props[0]}
        )
    return await client.request(
        "get_properties", {"path": ns.node_path, "properties": props}
    )


async def cmd_set(client: GameClient, ns: argparse.Namespace) -> dict:
    """写节点属性。``value`` 先按 JSON 解析；失败退回字符串字面量。
    加 ``--text-value`` 跳过 JSON 解析，强制当字符串处理。"""
    value = _resolve_value_for_set(ns)
    return await client.set_property(ns.node_path, ns.prop, value)


async def cmd_call(client: GameClient, ns: argparse.Namespace) -> Any:
    """调任意节点方法。每个 arg 同样 JSON-or-string 解析。
    加 ``--text-value`` 跳过 JSON 解析，所有 args 强制当字符串处理。"""
    args = _resolve_args_for_call(ns)
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


async def cmd_wait_prop(client: GameClient, ns: argparse.Namespace) -> dict:
    expected = _parse_json_arg(ns.value)
    return await client.wait_property(
        ns.node_path, ns.prop, expected,
        op=ns.op, timeout=float(ns.timeout), tolerance=float(ns.tolerance),
    )


async def cmd_wait_signal(client: GameClient, ns: argparse.Namespace) -> dict:
    timeout = float(ns.timeout) if ns.timeout else 5.0
    return await client.wait_signal(ns.node_path, ns.signal_name, timeout=timeout)


async def cmd_wait_frames(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.wait_frames(int(ns.frames), physics=ns.physics)


async def cmd_scene_reload(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.scene_reload(timeout=float(ns.timeout))


async def cmd_scene_change(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.scene_change(ns.scene_path, timeout=float(ns.timeout))


async def cmd_time_scale(client: GameClient, ns: argparse.Namespace) -> dict:
    value = float(ns.value) if ns.value is not None else None
    return await client.time_scale(value)


async def cmd_pause(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.pause()


async def cmd_unpause(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.unpause()


async def cmd_step_frames(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.step_frames(int(ns.frames), physics=ns.physics)


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


def _fmt_get_result_text(r: dict) -> str:
    """get 的文本渲染：单属性打印 value；多属性每行 ``prop = value``。"""
    if "values" in r:
        return "\n".join(
            f"{prop} = {_fmt_get_text(entry.get('value') if isinstance(entry, dict) else entry)}"
            for prop, entry in r["values"].items()
        )
    return _fmt_get_text(r.get("value"))


def _fmt_tree_text(tree: Any) -> str:
    return json.dumps(tree, indent=2, ensure_ascii=False)


def _fmt_screenshot_text(r: dict) -> str:
    base = f"screenshot saved: {r['path']} ({r['bytes']} bytes)"
    if "region" in r:
        base += f" [node={r.get('node')} region={r.get('region')}]"
    return base


def _fmt_sprite_info_text(r: dict) -> str:
    # 聚合 payload 字段多且按类型变化，text 模式直接缩进 JSON（人读）
    return json.dumps(r, ensure_ascii=False, indent=2)


def _fmt_errors_text(r: dict) -> str:
    entries = r.get("errors", [])
    if not entries:
        return f"no new errors (marker={r.get('marker')})"
    lines = [
        f"[{e.get('type')}] {e.get('message')}  ({e.get('source') or e.get('file')})"
        for e in entries
    ]
    suffix = f"marker={r.get('marker')}"
    if r.get("truncated"):
        suffix += " truncated=true"
    if r.get("dropped"):
        suffix += f" dropped={r.get('dropped')}"
    return "\n".join(lines) + f"\n({suffix})"


def _register_errors_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--since",
        type=int,
        default=0,
        metavar="MARKER",
        help="只看 seq > MARKER 的新增（传上一次响应的 marker，实现「本用例期间」语义）",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        metavar="N",
        help="最多返回 N 条（0..1000；0 = 纯基线查询，只拿 marker 不取数据）",
    )


def _register_screenshot_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--node",
        default=None,
        metavar="NODE_PATH",
        help=(
            "按该节点的屏幕 AABB 裁剪截图（issue #101），产出小图供像素级断言。"
            "节点在屏幕外/零尺寸 → 1011；非 CanvasItem/算不出边界 → 1010。"
        ),
    )


def _fmt_wait_node_text(r: dict) -> str:
    return "found" if r["found"] else "timeout"


def _fmt_wait_time_text(r: dict) -> str:
    return f"waited (success={r.get('success', True)})"


# ── exit_code_from helpers ──


def _exit_from_bool(b: Any) -> int:
    return EXIT_OK if b else EXIT_RPC_ERROR


def _exit_from_wait_node(r: dict) -> int:
    return EXIT_OK if r.get("found") else EXIT_RPC_ERROR


def _fmt_wait_prop_text(r: dict) -> str:
    if r.get("matched"):
        return f"matched (waited {r.get('waited', 0.0):.3f}s)"
    return (
        f"timeout (reason={r.get('reason', 'timeout')}, "
        f"last={json.dumps(r.get('value'), ensure_ascii=False)})"
    )


def _fmt_wait_signal_text(r: dict) -> str:
    return "emitted" if r.get("emitted") else "timeout"


def _exit_from_wait_prop(r: dict) -> int:
    return EXIT_OK if r.get("matched") else EXIT_RPC_ERROR


def _exit_from_wait_signal(r: dict) -> int:
    return EXIT_OK if r.get("emitted") else EXIT_RPC_ERROR


# ── extra_args registrators ──


def _register_wait_flag(p: argparse.ArgumentParser) -> None:
    """``--wait``：输入命令阻塞到动作时长（game-time）结束再返回（issue #90）。"""
    p.add_argument(
        "--wait",
        action="store_true",
        help="阻塞到动作时长（game-time）结束再返回，再读状态即结算后值；"
        "默认异步立即返回。等价于命令后再跑一次 wait-time <时长>，但复用同一连接。",
    )


def _register_combo_args(p: argparse.ArgumentParser) -> None:
    """combo 的 args：``json_file`` 位置可选，加 ``--steps-json`` / ``--wait``。
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
    _register_wait_flag(p)


def _register_tree_args(p: argparse.ArgumentParser) -> None:
    # issue #150：第一个位置参数以 / 开头当 node path（子树根），否则当 depth。
    # 消歧 + 校验在 _preflight_tree 里做（argparse 无法靠内容区分两个可选位置参数）。
    p.add_argument(
        "tree_arg1",
        nargs="?",
        default=None,
        metavar="path-or-depth",
        help=(
            "可选：节点绝对路径（以 / 开头，如 /root/GameUI）查该子树根；"
            "或遍历深度（整数，默认 3）。省略则 dump 当前场景。"
        ),
    )
    p.add_argument(
        "tree_arg2",
        nargs="?",
        default=None,
        metavar="depth",
        help="可选：遍历深度（仅当第一个参数是路径时有意义，默认 3）",
    )
    p.add_argument(
        "--max-nodes",
        type=int,
        default=200,
        help=(
            "节点数软上限（默认 200）。超出时服务端截断子节点并返回 "
            "{truncated: true, total_nodes: N}，agent 据此决定是否拆分子树。"
        ),
    )


def _register_actions_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--all",
        action="store_true",
        help="包含 ui_* 内置动作（默认仅项目自定义动作）",
    )


def _register_wait_prop_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("node_path", help="绝对节点路径")
    p.add_argument("prop", help="属性名（支持 sub-path 如 position:x）")
    p.add_argument("value", help="期望值（JSON-or-string，同 set 的 value 规则）")
    p.add_argument("--op", choices=_WAIT_PROP_OPS, default="eq",
                   help="比较运算符，默认 eq；gt/lt/ge/le 仅数值")
    p.add_argument("--timeout", default="5", help="超时秒（0..3600，默认 5）")
    p.add_argument("--tolerance", default="0", help="float eq/ne 容差（默认 0=精确比较）")


def _register_wait_frames_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("frames", help="等待帧数（1..3600）")
    p.add_argument("--physics", action="store_true", help="等 physics_frame（默认 process_frame）")


def _register_scene_reload_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--timeout", default="10",
                   help="等新场景 ready 的超时秒（>0 且 <=3600，默认 10）")


def _register_scene_change_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("scene_path", help="目标场景资源路径（res:// 或 uid://）")
    p.add_argument("--timeout", default="10",
                   help="等新场景 ready 的超时秒（>0 且 <=3600，默认 10）")


def _register_time_scale_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("value", nargs="?", default=None,
                   help="新倍速（>0 且 <=100）；省略则读当前值")


def _register_step_frames_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("frames", help="推进帧数（1..3600）")
    p.add_argument("--physics", action="store_true",
                   help="推进 physics_frame（默认 process_frame）")


def _register_set_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("node_path", help="绝对节点路径")
    p.add_argument("prop", help="属性名")
    p.add_argument(
        "value",
        help="JSON 字面量或字符串。例：'42' / '\"hello\"' / '[10, 20]' / 'hello'",
    )
    p.add_argument(
        "--text-value",
        action="store_true",
        help="把 value 当字面字符串，不走 JSON 解析（避开 'null'/'true'/数字 footgun）",
    )


def _register_call_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("node_path", help="绝对节点路径，如 /root/Main")
    p.add_argument("method", help="节点上的方法名")
    p.add_argument(
        "args",
        nargs="*",
        help="方法参数；每个先按 JSON 解析失败 fallback 字符串",
    )
    p.add_argument(
        "--text-value",
        action="store_true",
        help="把所有 args 当字面字符串，不走 JSON 解析",
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
        example="screenshot out.png --node /root/Game/Player/Sprite",
        extra_args=_register_screenshot_args,
        text_formatter=_fmt_screenshot_text,
        preflight=_preflight_screenshot,
    ),
    RpcSpec(
        name="sprite-info",
        handler=cmd_sprite_info,
        description=(
            "渲染态聚合查询（issue #101）：Sprite2D / AnimatedSprite2D / "
            "TextureRect 的 texture、实际图集区域（effective_region / "
            "frame_texture）、翻转、帧号、modulate、visible 一次拿齐。"
            "纯属性读，headless 可用。非 sprite 类节点 → 1010。"
        ),
        positionals=(
            Positional(
                "node_path", None, "绝对节点路径，如 /root/Game/Player/Sprite"
            ),
        ),
        example="sprite-info /root/Game/Player/Sprite",
        text_formatter=_fmt_sprite_info_text,
    ),
    RpcSpec(
        name="errors",
        handler=cmd_errors,
        description=(
            "结构化查询运行期捕获的 push_error / push_warning（issue #103）。"
            "返回 {errors, marker, dropped, truncated}；--since 传上次 marker "
            "只看新增（「本用例期间应零 push_error」断言的原语），--limit 0 "
            "纯拿基线。需 Godot 4.5+（Logger API），老引擎报 1012。"
        ),
        positionals=(),
        example="errors --since 42",
        extra_args=_register_errors_args,
        text_formatter=_fmt_errors_text,
    ),
    RpcSpec(
        name="tree",
        handler=cmd_tree,
        description="dump 场景树为 JSON（省略 path 取当前场景，传 /root 起的路径取子树）。",
        positionals=(),  # 由 extra_args 注册（path-or-depth + depth + --max-nodes）
        example="tree /root/GameUI 2",
        extra_args=_register_tree_args,
        preflight=_preflight_tree,
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
        description="短按动作（press → 等待 → release）。默认异步立即返回；加 --wait 阻塞到时长结束。",
        positionals=(
            Positional("action", None, "InputMap 动作名"),
            Positional("duration", "?", "按下时长（秒），默认 0.1"),
        ),
        example="tap jump 0.2",
        extra_args=_register_wait_flag,
        text_formatter=lambda r: f"tapped: {r}",
    ),
    RpcSpec(
        name="hold",
        handler=cmd_hold,
        description="按住动作指定时长（秒），到点自动释放。默认命令立即返回（动作在游戏里持续该时长）；要读动作完成后的状态请加 --wait（或命令后先 wait-time <时长>）。",
        positionals=(
            Positional("action", None, "InputMap 动作名"),
            Positional("duration", None, "按住时长（秒，必须 > 0）"),
        ),
        example="hold jump 1.5",
        extra_args=_register_wait_flag,
        preflight=_preflight_hold,
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
        description=(
            "读节点属性（1 个或多个；多个时服务端同帧原子读，issue #100）。"
            "复合类型（Vector2 等）返回与 set 同 schema 的数组 + type 字段，"
            "可直接回灌 set（issue #99）。支持 sub-path：position:x。"
        ),
        positionals=(
            Positional("node_path", None, "绝对节点路径，如 /root/Main"),
            Positional("props", "+", "属性名，1 个或多个；支持 sub-path 如 position:x"),
        ),
        example="get /root/Player position visible",
        text_formatter=_fmt_get_result_text,
    ),
    RpcSpec(
        name="set",
        handler=cmd_set,
        description=(
            "写节点属性。value 优先按 JSON 解析（数字/数组/对象），失败退回字符串。"
            "加 --text-value 强制把 value 当字符串，避开 null/true/false/数字 footgun。"
        ),
        positionals=(),  # 由 extra_args 注册（node_path + prop + value + --text-value）
        example="set /root/Main/Score text \"42\"",
        extra_args=_register_set_args,
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
            Positional("seconds", None, "等待秒数（服务端范围 0 ≤ seconds ≤ 3600；client 在 ≤0 时短路返回成功）"),
        ),
        example="wait-time 0.5",
        text_formatter=_fmt_wait_time_text,
    ),
    RpcSpec(
        name="wait-prop",
        handler=cmd_wait_prop,
        description=(
            "逐帧轮询直到属性满足条件（或 timeout）。退出码：0=命中, 1=超时, "
            "2=infra error。超时返回 reason（timeout/node_not_found/property_not_found）"
            "+ 最后读到的值，便于诊断 typo。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="wait-prop /root/Player position:x 500 --op gt --timeout 3",
        extra_args=_register_wait_prop_args,
        preflight=_preflight_wait_prop,
        text_formatter=_fmt_wait_prop_text,
        exit_code_from=_exit_from_wait_prop,
    ),
    RpcSpec(
        name="wait-signal",
        handler=cmd_wait_signal,
        description=(
            "等信号发射（或 timeout），命中带回编码后的信号参数。退出码：0=命中, "
            "1=超时, 2=infra error。注意：必须先挂等待再触发动作（见 SKILL.md pitfall）。"
        ),
        positionals=(
            Positional("node_path", None, "绝对节点路径"),
            Positional("signal_name", None, "信号名，如 body_entered"),
            Positional("timeout", "?", "超时秒（0..3600，默认 5）"),
        ),
        example="wait-signal /root/Area door_opened 3",
        preflight=_preflight_wait_signal,
        text_formatter=_fmt_wait_signal_text,
        exit_code_from=_exit_from_wait_signal,
    ),
    RpcSpec(
        name="wait-frames",
        handler=cmd_wait_frames,
        description="等 N 个 process 帧（--physics 等物理帧）。确定性帧推进，替代短 sleep。",
        positionals=(),  # 由 extra_args 注册
        example="wait-frames 3 --physics",
        extra_args=_register_wait_frames_args,
        preflight=_preflight_wait_frames,
        text_formatter=lambda r: f"waited {r.get('frames')} frames",
    ),
    RpcSpec(
        name="scene-reload",
        handler=cmd_scene_reload,
        description=(
            "重载当前场景并阻塞到新场景 ready（per-test 隔离原语）。"
            "失败（无 current scene / 超时）报 1008，exit 1。"
            "注意：返回后此前缓存的所有节点路径/引用全部失效。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="scene-reload",
        extra_args=_register_scene_reload_args,
        preflight=_preflight_scene_reload,
        text_formatter=lambda r: f"scene reloaded: {r.get('scene_path')} (root: {r.get('name')})",
    ),
    RpcSpec(
        name="scene-change",
        handler=cmd_scene_change,
        description=(
            "切换到指定场景并阻塞到新场景 ready。路径不存在/加载失败/超时 "
            "报 1008，exit 1。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="scene-change res://levels/level2.tscn",
        extra_args=_register_scene_change_args,
        preflight=_preflight_scene_change,
        text_formatter=lambda r: f"scene changed: {r.get('scene_path')} (root: {r.get('name')})",
    ),
    RpcSpec(
        name="time-scale",
        handler=cmd_time_scale,
        description=(
            "读 / 写 Engine.time_scale（无参 = 读）。wait-time 按 game time 计，"
            "倍速后语义不变、墙钟变快。合法域 (0, 100]。注意：--record 下仍生效，"
            "录出的是加速画面。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="time-scale 5",
        extra_args=_register_time_scale_args,
        preflight=_preflight_time_scale,
        text_formatter=lambda r: f"time_scale = {r.get('time_scale')}",
    ),
    RpcSpec(
        name="pause",
        handler=cmd_pause,
        description='暂停 SceneTree（get_tree().paused = true）。幂等；返回 {"paused": true}。',
        positionals=(),
        example="pause",
        text_formatter=lambda r: f"paused: {r.get('paused')}",
    ),
    RpcSpec(
        name="unpause",
        handler=cmd_unpause,
        description='恢复 SceneTree（paused = false）。幂等；返回 {"paused": false}。',
        positionals=(),
        example="unpause",
        text_formatter=lambda r: f"paused: {r.get('paused')}",
    ),
    RpcSpec(
        name="step-frames",
        handler=cmd_step_frames,
        description=(
            "paused 状态下确定性推进 N 帧再停（物理断言银弹：推 N 个物理帧后状态"
            "必然确定）。必须先 pause，否则报 1009，exit 1。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="step-frames 3 --physics",
        extra_args=_register_step_frames_args,
        preflight=_preflight_step_frames,
        text_formatter=lambda r: f"stepped {r.get('stepped')} frames (still paused)",
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


def _resolve_idle_timeout(ns: argparse.Namespace) -> int:
    """解析 idle-timeout 秒数：显式 ``--idle-timeout`` > 项目级 config > 0（关闭）。

    issue #44：默认值 ``"0"`` 时回退读 ``.cli_control/config.json`` 的 ``idle_timeout``，
    省得喜欢自动收尾的用户每次手敲 ``--idle-timeout``。config 也没设则仍是 0（关闭）。
    注意：显式 ``--idle-timeout 0`` 与默认 ``"0"`` 不可区分，两者都会回退 config——
    config 设了又想单次强制关闭，临时 `unset` 配置即可（YAGNI，未做 opt-out flag）。
    非法 duration（CLI 或 config 任一）抛 ``ValueError``，由调用方转 EXIT_USAGE。
    """
    from ._duration import parse_duration

    raw = getattr(ns, "idle_timeout", "0")
    if raw == "0":
        from .daemon import read_project_config

        cfg_val = read_project_config().get("idle_timeout")
        if cfg_val is not None:
            try:
                return parse_duration(str(cfg_val))
            except ValueError as e:
                raise ValueError(
                    f".cli_control/config.json 的 idle_timeout 非法：{e}"
                ) from e
    return parse_duration(raw)


def cmd_daemon_start(ns: argparse.Namespace) -> int:
    from .daemon import Daemon, DaemonError

    try:
        idle_seconds = _resolve_idle_timeout(ns)
    except ValueError as e:
        _emit_top_error(ns, code=CLIENT_CODE_USAGE, message=str(e))
        return EXIT_USAGE

    # --name / 顶层 --instance 选靶（通过 _merge_instance_flags 统一收口）
    merged, conflict = _merge_instance_flags(ns)
    if conflict:
        sub = getattr(ns, "name", None)
        top = getattr(ns, "instance", None)
        _emit_top_error(
            ns,
            code=CLIENT_CODE_USAGE,
            message=f"--name {sub!r} 与顶层 --instance {top!r} 冲突，二选一",
        )
        return EXIT_USAGE
    inst = merged or "default"
    if inst == "all":
        # #145：start 不经 _resolve_daemon_instance，需独立守卫顶层 --instance all。
        _emit_top_error(ns, code=CLIENT_CODE_USAGE, message=_BROADCAST_NOT_FOR_DAEMON_MSG)
        return EXIT_USAGE
    daemon = Daemon(Path.cwd(), instance=inst)
    try:
        daemon.start(
            record=ns.record,
            movie_path=ns.movie_path,
            headless=_resolve_headless(ns),
            fps=ns.fps,
            port=ns.port,
            idle_timeout=idle_seconds,
            time_scale=getattr(ns, "time_scale", None),
        )
    except DaemonError as e:
        _emit_top_error(ns, code=CLIENT_CODE_PRECONDITION, message=str(e))  # infra 前置失败 → -1006（#92）
        return EXIT_INFRA_ERROR
    if _output_format(ns) == OUTPUT_JSON:
        # 跟 daemon status 的信封形状对齐，方便 agent 一套 jq 处理三个命令。
        # 注：``daemon.start()`` 已阻塞到端口可用，所以 is_running 此刻为 true
        # 是稳态。理论上若 daemon 在 start 返回后被外部 kill，read_pid 与
        # is_running 之间存在亚毫秒级 race；窗口够小可以忽略。
        port = daemon.current_port() or ns.port
        pid = daemon.read_pid() if daemon.is_running() else None
        result: dict[str, Any] = {"started": True, "instance": daemon.instance, "port": port}
        if pid is not None:
            result["pid"] = pid
        _emit_success_payload(result)
    return EXIT_OK


def _emit_stop_summary(
    results: list[dict[str, Any]], had_failure: bool, fmt: str
) -> int:
    """``stop --all`` 两分支共用的收尾：JSON/text 汇总输出 + 聚合退出码（#144）。

    rc 含义同既有：0=全成功；EXIT_PARTIAL=至少一条 DaemonError。
    """
    rc_total = EXIT_PARTIAL if had_failure else EXIT_OK
    if fmt == OUTPUT_JSON:
        _emit_success_payload({"stopped": results, "rc": rc_total})
    else:
        failed = sum(1 for x in results if "error" in x)
        print(
            f"summary: {len(results) - failed}/{len(results)} stopped"
            + (f", {failed} failed" if failed else "")
        )
    return rc_total


def cmd_daemon_stop(ns: argparse.Namespace) -> int:
    """停止 daemon。

    选靶矩阵：
    * ``--all``（无 --project）：全局注册表所有实例，以每条记录的 instance 字段构造 Daemon。
    * ``--all --project <path>``：指定项目下所有活实例（扫 instances/ 目录）。
    * ``--name <inst>``：cwd 项目（或 --project 指定项目）的指定实例。
    * 无 --all 无 --name：自动选靶（0 个 → default；1 个 → 它；≥2 → preflight 报错）。

    JSON 模式每条 entry 含 instance 字段；Text 行含 instance 字样。
    """
    from .daemon import Daemon, DaemonError
    from . import registry

    fmt = _output_format(ns)

    # --all 与实例选靶（--name / 顶层 --instance）互斥
    # 注意：只查 ns.name 不够——顶层 --instance 落在 ns.instance，同样是"选靶"语义
    inst_flag = getattr(ns, "name", None) or getattr(ns, "instance", None)
    if getattr(ns, "all", False) and inst_flag:
        _emit_top_error(ns, code=CLIENT_CODE_USAGE,
                        message="--all 与实例选靶（--name / 顶层 --instance）互斥")
        return EXIT_USAGE

    if getattr(ns, "all", False):
        if getattr(ns, "project", None):
            # --all --project：停指定项目下所有活实例（扫 instances/ 不查注册表）
            from .daemon import list_live_instances
            target = ns.project.resolve()
            names = list_live_instances(target)
            if not names:
                # legacy daemon（平铺布局在跑）不出现在 instances/ 扫描里，但
                # default 实例的 is_running 带 legacy fallback，仍能停到它；
                # 真没东西在跑时不伪造 default 条目（#144），空汇总输出与
                # --all 全局空注册表的先例形状对齐。
                if Daemon(target).is_running():
                    names = ["default"]
                else:
                    if fmt == OUTPUT_JSON:
                        _emit_success_payload({"stopped": [], "rc": 0})
                    else:
                        print("(no running daemons)")
                    return EXIT_OK
            results: list[dict[str, Any]] = []
            had_failure = False
            for inst_name in names:
                entry: dict[str, Any] = {
                    "project_root": str(target),
                    "instance": inst_name,
                }
                try:
                    d = Daemon(target, instance=inst_name)
                    entry["pid"] = d.read_pid()
                    entry["port"] = d.current_port()
                    rc = d.stop()
                    entry["rc"] = rc
                    if fmt != OUTPUT_JSON:
                        suffix = f" (rc={rc})" if rc != 0 else ""
                        print(f"stopped instance={inst_name} {target}{suffix}")
                except DaemonError as e:
                    entry["rc"] = EXIT_INFRA_ERROR
                    entry["error"] = str(e)
                    had_failure = True
                    print(f"[{target}:{inst_name}] {e}", file=sys.stderr)
                results.append(entry)
            return _emit_stop_summary(results, had_failure, fmt)

        # --all（无 --project）：全局注册表，以记录的 instance 字段构造 Daemon
        records = registry.list_all()
        if not records:
            if fmt == OUTPUT_JSON:
                _emit_success_payload({"stopped": [], "rc": 0})
            else:
                print("(no running daemons)")
            return EXIT_OK
        all_results: list[dict[str, Any]] = []
        had_failure_global = False
        for r in records:
            r_entry: dict[str, Any] = {
                "project_root": r.project_root,
                "instance": r.instance,
                "pid": r.pid,
                "port": r.port,
            }
            try:
                # 以记录的 instance 构造，防止永远打 "default" 的回归
                rc = Daemon(Path(r.project_root), instance=r.instance).stop()
                r_entry["rc"] = rc
                if fmt != OUTPUT_JSON:
                    suffix = f" (rc={rc})" if rc != 0 else ""
                    print(f"stopped pid={r.pid} port={r.port} instance={r.instance} {r.project_root}{suffix}")
            except DaemonError as e:
                r_entry["rc"] = EXIT_INFRA_ERROR
                r_entry["error"] = str(e)
                had_failure_global = True
                # 单条失败不阻止其余收尾
                print(f"[{r.project_root}:{r.instance}] {e}", file=sys.stderr)
            all_results.append(r_entry)
        return _emit_stop_summary(all_results, had_failure_global, fmt)

    # 单停
    target = (ns.project.resolve() if getattr(ns, "project", None) else Path.cwd())
    inst = _resolve_daemon_instance(ns, target)
    if inst is None:
        # 歧义，_resolve_daemon_instance 已 emit 信封
        return EXIT_USAGE
    daemon = Daemon(target, instance=inst)
    try:
        rc = daemon.stop()
    except DaemonError as e:
        _emit_top_error(ns, code=CLIENT_CODE_PRECONDITION, message=str(e))  # infra 前置失败 → -1006（#92）
        return EXIT_INFRA_ERROR
    if fmt == OUTPUT_JSON:
        # rc 0=正常停 / 2=ffmpeg 转码失败但 daemon 已停。两种都算"stopped"，
        # 把 rc 透出让 agent 决定要不要 retry transcode。
        _emit_success_payload({"stopped": True, "rc": rc, "instance": inst, "project_root": str(target)})
    return rc


def cmd_daemon_status(ns: argparse.Namespace) -> int:
    """打印 daemon 状态；exit 0=运行中，1=未运行。

    JSON 模式（默认）：
      ``{"ok": true, "result": {"state": "running", "pid": <int>, "port": <int>, "instance": <str>}}``
      / ``{"ok": true, "result": {"state": "stopped"}}``
    Text 模式：
      ``running pid=<pid> port=<port> instance=<name>`` / ``stopped``

    多实例时用 --name 选靶；若只有一个实例在跑则自动选中。
    退出码语义不变（shell ``if godot-cli-control daemon status; then …`` 仍能用）。
    """
    from .daemon import Daemon

    fmt = _output_format(ns)
    # 选靶：--name 显式 → 直接用；否则自动选（0→default，1→它，≥2→歧义报错）
    inst = _resolve_daemon_instance(ns, Path.cwd())
    if inst is None:
        return EXIT_USAGE
    daemon = Daemon(Path.cwd(), instance=inst)
    if daemon.is_running():
        pid = daemon.read_pid()
        port = daemon.current_port()
        if fmt == OUTPUT_JSON:
            payload: dict[str, Any] = {"state": "running", "instance": inst, "pid": pid}
            if port is not None:
                payload["port"] = port
            _emit_success_payload(payload)
        else:
            print(
                f"running pid={pid} port={port if port is not None else '?'} instance={inst}"
            )
        return EXIT_OK
    # Stopped：若上一轮启动留下了 godot.log / last_exit_code，把诊断信息透出来。
    # issue #38 要求 daemon 已退出时直接告诉用户「last exit: <code>, see ...log」，
    # 不让用户再手摸 .cli_control/ 翻文件。
    stopped_payload: dict[str, Any] = {"state": "stopped"}
    if daemon.log_file.exists():
        stopped_payload["last_log"] = str(daemon.log_file)
    last_rc = daemon.read_last_exit_code()
    if last_rc is not None:
        stopped_payload["last_exit_code"] = last_rc
    if fmt == OUTPUT_JSON:
        _emit_success_payload(stopped_payload)
    else:
        hints: list[str] = []
        if "last_exit_code" in stopped_payload:
            hints.append(f"last exit: {stopped_payload['last_exit_code']}")
        if "last_log" in stopped_payload:
            hints.append(f"see {stopped_payload['last_log']}")
        if hints:
            print(f"stopped ({', '.join(hints)})")
        else:
            print("stopped")
    return EXIT_RPC_ERROR


def cmd_daemon_logs(ns: argparse.Namespace) -> int:
    """输出 .cli_control/instances/<name>/godot.log 尾部若干行（issue #103）。

    纯客户端读文件，不走 RPC —— daemon 已退出也能 post-mortem（与
    ``daemon status`` 透出 last_log 的语义衔接：status 告诉你日志在哪，
    logs 直接把尾部喂给你，免去 agent 再 shell 出去 tail + 找路径）。

    多实例时用 --name 选靶；若只有一个实例在跑则自动选中。
    JSON 模式：``{"ok": true, "result": {"path": ..., "lines": [...],
    "returned": N, "instance": <str>}}``；无日志文件 → ``-1006`` infra 前置失败，exit 2。
    """
    from .daemon import Daemon

    fmt = _output_format(ns)
    # 选靶：--name 显式 → 直接用；否则自动选
    inst = _resolve_daemon_instance(ns, Path.cwd())
    if inst is None:
        return EXIT_USAGE
    daemon = Daemon(Path.cwd(), instance=inst)
    if not daemon.log_file.exists():
        _emit_top_error(
            ns,
            code=CLIENT_CODE_PRECONDITION,
            message=(
                f"no daemon log at {daemon.log_file} — daemon 从未在该项目启动过"
                "（或 .cli_control/ 被清理）。先 daemon start。"
            ),
        )
        return EXIT_INFRA_ERROR
    tail: int = ns.tail
    text = daemon.log_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()[-tail:]
    payload: dict[str, Any] = {
        "instance": inst,
        "path": str(daemon.log_file),
        "lines": lines,
        "returned": len(lines),
    }
    if fmt == OUTPUT_JSON:
        _emit_success_payload(payload)
    else:
        for line in lines:
            print(line)
        print(f"({payload['path']}, last {len(lines)} lines)")
    return EXIT_OK


def cmd_daemon_ls(ns: argparse.Namespace) -> int:
    """跨项目列出运行中的 daemon。

    扫全局注册表（POSIX `~/.local/state/godot-cli-control/daemons/`；Windows
    `%LOCALAPPDATA%\\godot-cli-control\\daemons\\`），对每条记录探活。
    死记录会被 list_all 自动清理（连同对应项目的 .cli_control/godot.pid 与 port）。
    JSON 模式：{"ok": true, "result": {"daemons": [{"pid","port","instance","project_root",...}]}}。
    Text 模式：每条一行 ``<pid>\\t<port>\\t<instance>\\t<project_root>\\t<started_at>``；
               空时打 (no running daemons)。
    """
    from . import registry

    fmt = _output_format(ns)
    records = registry.list_all()
    payload = {
        "daemons": [
            {
                "project_root": r.project_root,
                "pid": r.pid,
                "port": r.port,
                "instance": r.instance,
                "started_at": r.started_at,
                "godot_bin": r.godot_bin,
                "log_path": r.log_path,
            }
            for r in records
        ]
    }
    if fmt == OUTPUT_JSON:
        _emit_success_payload(payload)
    else:
        if not records:
            print("(no running daemons)")
        else:
            for r in records:
                # 格式：pid\tport\tinstance\tproject_root\tstarted_at
                print(f"{r.pid}\t{r.port}\t{r.instance}\t{r.project_root}\t{r.started_at}")
    return EXIT_OK


def cmd_run(ns: argparse.Namespace) -> int:
    """加载用户脚本（要求定义 ``run(bridge)``），自动启停 daemon。

    json 模式下：成功 ``{"ok": true, "result": {"exit_code": 0, "script": "..."}}``；
    各类失败（脚本不存在、daemon 起不来、用户脚本 raise）封 ``{"ok": false, "error": ...}``。
    text 模式行为不变。

    退出码：
      0  脚本成功（且 daemon stop 成功）
      1  脚本运行失败（RPC 错语义；envelope `ok=false` 时也走这里）
      2  基础设施前置失败（daemon 起不来等，code -1006 PRECONDITION）
      64 用法错（脚本路径不存在、缺 run(bridge)、idle_timeout 解析失败等，code -1003；
         多实例并行且未传 --name 也属用法错，同归 64/-1003）
    """
    from .daemon import Daemon, DaemonError

    fmt = _output_format(ns)

    # 顶层 try 兜底：cmd_run 内部任何未捕获异常（daemon.is_running 抛 OSError、
    # _exec_user_script importlib 边界 raise 等）都要落 envelope。和 _run_rpc 的
    # 兜底语义对齐，保 CLAUDE.md 契约 1（任何异常都必须落进信封）。
    try:
        script_path = Path(ns.script)
        if not script_path.exists():
            # 用户传错路径属于"用法错" → -1003 + EXIT_USAGE(64)（#92）
            msg = f"找不到脚本: {script_path}"
            if fmt == OUTPUT_JSON:
                _emit_error_payload(CLIENT_CODE_USAGE, msg)
            else:
                print(f"错误：{msg}", file=sys.stderr)
            return EXIT_USAGE

        try:
            idle_seconds = _resolve_idle_timeout(ns)
        except ValueError as e:
            if fmt == OUTPUT_JSON:
                _emit_error_payload(CLIENT_CODE_USAGE, str(e))
            else:
                print(f"错误：{e}", file=sys.stderr)
            return EXIT_USAGE

        # 多实例选靶：0 个在跑 → "default"（走既有 auto-start 分支）；
        # 1 个命名实例在跑 → 自动选中（不会再起新实例，auto_started=False 自然成立）；
        # N≥2 且无 --name → preflight 报歧义（CLAUDE.md 契约 #5）。
        inst = _resolve_daemon_instance(ns, Path.cwd())
        if inst is None:
            # _resolve_daemon_instance 已 emit 信封
            return EXIT_USAGE
        daemon = Daemon(Path.cwd(), instance=inst)
        auto_started = False
        if not daemon.is_running():
            # 静态检测脚本含 screenshot 时，非 TTY 默认从 headless 翻转到 GUI
            # （issue #65）。显式 --headless / --gui / --no-gui-auto 都能 opt-out。
            force_gui_hint = (
                not getattr(ns, "no_gui_auto", False)
                and _script_likely_uses_screenshot(script_path)
            )
            try:
                daemon.start(
                    record=ns.record,
                    movie_path=ns.movie_path,
                    headless=_resolve_headless(ns, force_gui_hint=force_gui_hint),
                    fps=ns.fps,
                    port=ns.port,
                    idle_timeout=idle_seconds,
                )
            except DaemonError as e:
                # daemon 起不来是 infra 前置失败（端口冲突、godot bin 不可执行等），
                # → -1006 (PRECONDITION) + EXIT_INFRA_ERROR(2)（#92）
                if fmt == OUTPUT_JSON:
                    _emit_error_payload(CLIENT_CODE_PRECONDITION, str(e))
                else:
                    print(f"错误：{e}", file=sys.stderr)
                return EXIT_INFRA_ERROR
            auto_started = True

        port = daemon.current_port() or ns.port
        # try/finally 保护：_exec_user_script 内部抛非 catch 异常（GameBridge
        # 连接失败、importlib 边界错误等）时仍要停 daemon，避免 Godot 进程泄漏
        # 让下次 daemon start 报「already running」。
        exit_code = 1
        # json + 脚本成功时由本函数 emit envelope（这样可以塞
        # daemon_stop_warning，让 envelope 与 exit code 在「脚本 OK + daemon
        # stop 失败」组合下一致）。脚本失败时仍由 _exec_user_script 直接 emit
        # error envelope（脚本错是主因，daemon stop 的次要状态不应覆盖它）。
        success_payload: dict[str, Any] | None = (
            {} if fmt == OUTPUT_JSON else None
        )
        try:
            exit_code = _exec_user_script(
                script_path,
                port,
                output_format=fmt,
                success_payload_out=success_payload,
            )
        finally:
            stop_warning: str | None = None
            if auto_started:
                try:
                    stop_rc = daemon.stop()
                except DaemonError as e:
                    # 停 daemon 出错走人类可读 stderr —— envelope 仍由脚本结果主导。
                    # 这一行在 json 模式下也保留 stderr 提示，避免 silent leak。
                    print(f"警告：停止 daemon 失败：{e}", file=sys.stderr)
                    stop_rc = 1
                    stop_warning = f"daemon stop raised: {e}"
                else:
                    if stop_rc != 0:
                        # rc 0 / 2 由 Daemon.stop 定义：2 = ffmpeg transcode 失败
                        # 但进程已停。让 envelope 携带这一信号，给 agent retry 提示。
                        stop_warning = f"daemon stop rc={stop_rc}"
                if exit_code == 0 and stop_rc != 0:
                    exit_code = stop_rc
            if (
                fmt == OUTPUT_JSON
                and success_payload is not None
                and success_payload  # 仅在 _exec_user_script 已成功回填字段时 emit
            ):
                if stop_warning is not None:
                    success_payload["daemon_stop_warning"] = stop_warning
                _emit_success_payload(success_payload)
        return exit_code
    except Exception as e:  # noqa: BLE001
        # 与 _run_rpc 的 except Exception 兜底对齐：traceback 走 stderr 帮人
        # debug，envelope 给 agent 一个 CLIENT_CODE_INTERNAL 解释失败原因。
        traceback.print_exc(file=sys.stderr)
        if fmt == OUTPUT_JSON:
            _emit_error_payload(
                CLIENT_CODE_INTERNAL, f"{type(e).__name__}: {e}"
            )
        else:
            print(f"错误：内部异常 {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_INFRA_ERROR


def _exec_user_script(
    script_path: Path,
    port: int,
    *,
    output_format: str = "text",
    success_payload_out: dict[str, Any] | None = None,
) -> int:
    """加载脚本模块、调用 ``run(bridge)``，捕获错误返回 exit code。

    json 模式：每个分支负责输出 envelope。
      - 成功 → ``{exit_code:0, script:...}``
      - ``spec_from_file_location`` 返回 None / 缺 ``run(bridge)`` → ``CLIENT_CODE_USAGE``
      - 用户脚本"加载阶段"（``exec_module`` 时顶层 raise，含 ImportError /
        SyntaxError / 顶层赋值出错）→ ``CLIENT_CODE_SCRIPT_ERROR``
      - 用户脚本"运行阶段"（``run(bridge)`` 内 raise）→ ``CLIENT_CODE_SCRIPT_ERROR``
      - GameBridge 连接失败 → ``CLIENT_CODE_CONNECTION``

    stderr 仍写完整 traceback / 友好提示，保留 human debug 信息。json 模式
    下用户脚本期间的 stdout（含 ``exec_module`` 跑顶层语句、``run(bridge)``
    内部）被整体 redirect 到 stderr，envelope 永远是单行 stdout——单纯包
    ``module.run`` 不够，因为顶层 ``print()`` 在 import 时就会输出，于是
    覆盖范围拉到 ``exec_module`` 起。

    ``success_payload_out``：调用方（cmd_run）若需要在 success envelope 里
    附加 daemon stop 警告等额外字段、由调用方自己 emit，可传入一个空 dict；
    本函数仅回填 ``exit_code`` / ``script`` 不 emit success envelope。
    传 None（默认）保留旧行为：success 时 _exec_user_script 直接 emit
    envelope（error envelope 永远由本函数 emit，外层不接管，避免脚本失败
    走顶层兜底误判为框架内部 bug）。

    text 模式行为完全不变。
    """
    import importlib.util

    from .bridge import GameBridge

    is_json = output_format == OUTPUT_JSON

    def _emit_usage(message: str) -> None:
        if is_json:
            _emit_error_payload(CLIENT_CODE_USAGE, message)
        else:
            print(f"错误：{message}", file=sys.stderr)

    def _emit_script_failure(stage: str, exc: BaseException) -> None:
        # stderr 永远拿到完整 traceback —— human 调试需要堆栈；json envelope
        # 只放 exception type + last line，避免一行单 message 太长。
        # 用 print_exception(exc) 而非 print_exc()：本函数总是在 except 块
        # 外被调（exception 先存进本地变量、退出 redirect 上下文后才处理），
        # active exc_info 此时已被清空。
        print(f"错误：脚本 {script_path} {stage}失败：", file=sys.stderr)
        traceback.print_exception(exc)
        if is_json:
            msg = (
                f"{type(exc).__name__}: {exc}"
                if str(exc)
                else type(exc).__name__
            )
            _emit_error_payload(
                CLIENT_CODE_SCRIPT_ERROR, f"脚本 {stage}失败：{msg}"
            )

    spec = importlib.util.spec_from_file_location("user_script", script_path)
    if spec is None or spec.loader is None:
        _emit_usage(f"无法加载脚本: {script_path}")
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
    # json 模式下整段"用户代码执行"包在 redirect_stdout 内：从 exec_module
    # 跑脚本顶层（用户可能 ``print("Loading…")``）到 ``module.run(bridge)``
    # 内部 print 一律走 stderr，envelope 唯一占用 stdout 单行。异常先存进
    # 本地变量、出 redirect 上下文后再 emit envelope —— 在 redirect 内 emit
    # 会把 envelope 也写到 stderr。
    # text 模式用 nullcontext 不绕路、零开销。
    stdout_redirect: contextlib.AbstractContextManager[Any] = (
        contextlib.redirect_stdout(sys.stderr)
        if is_json
        else contextlib.nullcontext()
    )
    try:
        load_error: BaseException | None = None
        run_error: BaseException | None = None
        missing_run = False
        connection_error: ConnectionError | None = None
        ran = False

        with stdout_redirect:
            try:
                spec.loader.exec_module(module)
            except Exception as e:  # noqa: BLE001 - 用户脚本任何异常都要抓
                load_error = e
            else:
                if not hasattr(module, "run"):
                    missing_run = True
                else:
                    # GameBridge.__init__ 在连接 daemon 失败时抛 ConnectionError；
                    # 不友好地走默认 traceback 路径会让用户以为是脚本出错。
                    # 单独捕获给一行可读信息（daemon 没起、端口写错、防火墙
                    # 拦截 等）。"运行 ..." 这条状态行写 stderr，redirect 影响
                    # 不到（contextlib.redirect_stdout 只覆盖 sys.stdout）。
                    print(f"运行 {script_path}...", file=sys.stderr)
                    try:
                        bridge = GameBridge(port=port)
                    except ConnectionError as e:
                        connection_error = e
                    else:
                        try:
                            module.run(bridge)
                            ran = True
                        except Exception as e:  # noqa: BLE001
                            run_error = e

        if load_error is not None:
            _emit_script_failure("加载", load_error)
            return 1
        if missing_run:
            _emit_usage(f"脚本 {script_path} 中缺少 run(bridge) 函数")
            return EXIT_USAGE  # 用法错，#92
        if connection_error is not None:
            if is_json:
                _emit_error_payload(
                    CLIENT_CODE_CONNECTION,
                    f"连接 daemon 失败 (port={port}): {connection_error}",
                )
            print(
                f"错误：连接 daemon 失败 (port={port}): {connection_error}\n"
                "提示：先运行 `godot-cli-control daemon start` 或检查端口是否被占用。",
                file=sys.stderr,
            )
            return 1
        if run_error is not None:
            _emit_script_failure("运行", run_error)
            return 1
        if not ran:
            # 理论上不可达：上面四种 error 状态都已 return。兜底 envelope 保契约。
            _emit_usage("脚本未执行（内部状态异常）")
            return 1
        if is_json:
            success_payload = {"exit_code": 0, "script": str(script_path)}
            if success_payload_out is not None:
                # 让 cmd_run 在 finally 拿到 daemon stop 结果后统一 emit，
                # 这样 envelope 才能携带 daemon_stop_warning，与 exit code 对齐。
                success_payload_out.update(success_payload)
            else:
                _emit_success_payload(success_payload)
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
    from .init_cmd import INIT_RESULT_ERROR_KEY, run_init

    fmt = _output_format(ns)
    # json 模式下让 run_init 抑制人类可读 print，把结构化字段回填到本地 dict。
    # text 模式仍走原路径（result=None 等价于不收集）。
    result: dict[str, Any] | None = {} if fmt == OUTPUT_JSON else None

    # 顶层 try 兜底：run_init 内部任何未捕获异常（shutil.copytree、
    # reimport_project 抛 OSError / subprocess 边界异常等）都要落 envelope。
    # 和 _run_rpc 的兜底语义对齐，保 CLAUDE.md 契约 1。
    try:
        rc = run_init(
            # 保留 .resolve()：run_init 内部用 relative_to(project_root) 打印
            # skill 路径，相对路径会让 relative_to 在 cwd 不寻常时抛 ValueError。
            project_root=(Path(ns.path).resolve() if ns.path else Path.cwd()),
            clobber_addon=not ns.keep_addon,
            write_skills=not ns.no_skills,
            skills_only=ns.skills_only,
            clobber_skills=not ns.skills_no_clobber,
            write_gitignore=not ns.no_gitignore,
            output_format=fmt,
            result=result,
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        if fmt == OUTPUT_JSON:
            _emit_error_payload(
                CLIENT_CODE_INTERNAL, f"{type(e).__name__}: {e}"
            )
        else:
            print(f"错误：内部异常 {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_INFRA_ERROR

    if fmt == OUTPUT_JSON and result is not None:
        if rc == 0:
            _emit_success_payload(result)
        else:
            message = result.pop(INIT_RESULT_ERROR_KEY, None) or "init failed"
            _emit_error_payload(CLIENT_CODE_USAGE, message)
    return rc


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


def _instance_substituted(value: Any, instance: str) -> Any:
    """递归把字符串里的 ``{instance}`` 换成实例名（str/list/dict；其余原样）。

    通用机制：screenshot 路径、set/call 的 JSON 字面量、combo 缓存 steps 都吃
    同一规则——agent 只须记一条。无转义口子（YAGNI），SKILL.md pitfall 注明。
    """
    if isinstance(value, str):
        return value.replace("{instance}", instance)
    if isinstance(value, list):
        return [_instance_substituted(v, instance) for v in value]
    if isinstance(value, dict):
        return {k: _instance_substituted(v, instance) for k, v in value.items()}
    return value


def _namespace_for_instance(ns: argparse.Namespace, instance: str) -> argparse.Namespace:
    """广播：拷贝 namespace 并做 {instance} 替换，原 ns 不动（#145）。"""
    return argparse.Namespace(
        **{k: _instance_substituted(v, instance) for k, v in vars(ns).items()}
    )


# ── argparse 装配 ──


_TOP_EPILOG = """\
命令分组：

  Daemon 管理:
    daemon start    启动 Godot daemon（可选录制 / headless）
    daemon stop     停止当前 daemon
    daemon status   显示 daemon 状态（pid / port），exit 0=运行中，1=未运行
    daemon logs     输出 godot.log 尾部（--tail N；daemon 停了也能 post-mortem）
    run <script>    自动启停 daemon 并跑用户脚本（脚本需定义 run(bridge)）

  接入:
    init            在 Godot 项目根一键复制插件、patch project.godot

  RPC 一发命令（需先 daemon 在跑）:
    读：     get / text / exists / visible / children / tree / pressed / actions / sprite-info
    写：     set / call / click
    输入：   press / release / tap / hold / combo / combo-cancel / release-all
    等待：   wait-node / wait-time
    截图：   screenshot（--node 按节点裁剪）
    诊断：   errors（push_error 结构化增量查询）

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


def _tail_arg(raw: str) -> int:
    """argparse type：daemon logs --tail 的域校验（1..1000，错误走 -1003 + 64）。"""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--tail 必须是整数，收到 {raw!r}")
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError(f"--tail 必须在 1..1000，收到 {value}")
    return value


def _time_scale_arg(raw: str) -> float:
    """argparse type：daemon start --time-scale 的域校验（错误走 -1003 + 64）。

    # 域 (0, 100] 与 _preflight_time_scale / daemon.start 校验对齐，改动需三处同步
    """
    try:
        v = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是数字，收到 {raw!r}")
    if not 0 < v <= 100:
        raise argparse.ArgumentTypeError(f"必须 > 0 且 <= 100，收到 {v}")
    return v


def _movie_path_arg(raw: str) -> str:
    """argparse type：``--movie-path`` 的扩展名校验（错误走 -1003 + 64）。

    Godot Movie Maker 只认 .avi/.png；传 .mp4 时 Godot 打 "Can't find movie
    writer" 后**继续正常跑**——脚本照常执行、exit 0，但什么都没录（#152 假成功）。
    「stop 时自动转码出 .mp4」的设计又让人直觉传 .mp4，所以启动前 fail-loud。
    # 合法后缀集与 daemon.start 的 belt 校验对齐，改动需两处同步
    """
    if Path(raw).suffix.lower() not in {".avi", ".png"}:
        raise argparse.ArgumentTypeError(
            f"Godot Movie Maker 只支持 .avi/.png，收到 {raw!r}；"
            "传 .avi 即可，CLI 会在 daemon stop 时自动转码出 .mp4"
        )
    return raw


def _instance_name_arg(value: str) -> str:
    """argparse type 校验：非法实例名 → ArgumentTypeError，触发 -1003/64 信封。"""
    from .daemon import DaemonError, validate_instance_name

    try:
        return validate_instance_name(value)
    except DaemonError as e:
        raise argparse.ArgumentTypeError(str(e))


def _instance_arg_allow_all(value: str) -> str:
    """顶层 ``--instance`` 的 argparse type：放行广播哨兵 ``all``（#145），
    其余走常规实例名校验。``--name`` 不放行——'all' 不可用作实例名。"""
    if value == "all":
        return value
    return _instance_name_arg(value)


def _add_instance_name_flag(p: argparse.ArgumentParser) -> None:
    """daemon 子命令的实例选择 flag。独立于 _add_daemon_flags：后者全是
    start 专属 flag（--record/--headless/...），挂到 status/logs/stop 会污染。"""
    p.add_argument(
        "--name",
        type=_instance_name_arg,
        default=None,
        help="实例名（默认 default；多实例并行时用于选靶，等价顶层 --instance；两者同时传值须一致）",
    )


# #145：--instance all 仅对 RPC 子命令广播；daemon / run 是单靶或已有 --all 语义。
_BROADCAST_NOT_FOR_DAEMON_MSG = (
    "--instance all 仅对 RPC 子命令广播；daemon/run 子命令请用 --name <inst> "
    "指定单实例（停全部实例用 daemon stop --all）"
)


def _merge_instance_flags(ns: argparse.Namespace) -> tuple[str | None, bool]:
    """合并顶层 --instance 与子命令 --name，返回 (合并后的实例名 or None, 是否冲突)。

    两者相同或只传一个 → (值, False)；两者都传且值不同 → (None, True)。
    冲突时调用方负责 emit -1003 信封并返回 EXIT_USAGE。
    """
    sub = getattr(ns, "name", None)       # 子命令 --name（daemon start/stop/status/logs/run）
    top = getattr(ns, "instance", None)   # 顶层 --instance
    if sub is not None and top is not None and sub != top:
        return None, True
    return sub or top, False


def _resolve_daemon_instance(ns: argparse.Namespace, project_root: Path) -> str | None:
    """返回实例名；歧义时 emit -1003 信封并返回 None（调用方 return EXIT_USAGE）。

    顶层 --instance 与子命令 --name 统一收口（通过 _merge_instance_flags）：
    两者等价；同时传且值相同合法；值不同报冲突（用法错 -1003）。

    未显式指定时：0 个在跑 → "default"（保 legacy fallback 与 stopped 语义）；
    1 个 → 它；≥2 → 报错列名（preflight，本地 FS 判定，先于任何网络/进程操作）。
    """
    inst, conflict = _merge_instance_flags(ns)
    if conflict:
        sub = getattr(ns, "name", None)
        top = getattr(ns, "instance", None)
        _emit_top_error(
            ns,
            code=CLIENT_CODE_USAGE,
            message=f"--name {sub!r} 与顶层 --instance {top!r} 冲突，二选一",
        )
        return None
    if inst == "all":
        # #145：广播哨兵只在 RPC 路径有意义；daemon/run 单靶路径明确拒绝并指路。
        _emit_top_error(ns, code=CLIENT_CODE_USAGE, message=_BROADCAST_NOT_FOR_DAEMON_MSG)
        return None
    if inst is not None:
        return inst
    from .daemon import list_live_instances

    live = list_live_instances(project_root)
    if len(live) > 1:
        _emit_top_error(
            ns,
            code=CLIENT_CODE_USAGE,
            message=f"multiple instances running: {', '.join(live)} — pass --name <instance>",
        )
        return None
    # 0 个在跑 → "default"（legacy fallback 语义）；1 个 → 它
    return live[0] if live else "default"


def _add_daemon_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--record",
        action="store_true",
        help="启动后录制 demo（写到 .cli_control/movie_path）。需真实渲染器，"
        "不能与 --headless 同用；没指定时会自动开窗（即使非 TTY）。",
    )
    p.add_argument(
        "--movie-path",
        default=None,
        type=_movie_path_arg,
        help="demo 输出路径，只接受 .avi/.png（Godot Movie Maker 所限；"
        "stop 时自动转码出 .mp4）",
    )
    headless_grp = p.add_mutually_exclusive_group()
    headless_grp.add_argument(
        "--headless",
        action="store_true",
        help="无窗口模式。默认值：stdout 非 TTY 时自动 headless（CI / pipe / agent）。"
        "与 --record 互斥（录制需真实渲染器）。",
    )
    headless_grp.add_argument(
        "--gui",
        action="store_true",
        help="强制开窗。覆盖 isatty 自动判（例如 stdout 是 pipe 仍想看到窗口）。",
    )
    p.add_argument("--fps", type=int, default=30, help="录制帧率，默认 30")
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="GameBridge 监听端口（默认 0 = OS 自动分配；写入 .cli_control/instances/<name>/port）",
    )
    p.add_argument(
        "--idle-timeout",
        type=str,
        default="0",
        help="空闲超时（如 30m / 2h / 90s / 0=关闭，默认关）。开启后 Godot 端 Timer 自动 quit。"
        "不传时回退读 .cli_control/config.json 的 idle_timeout（issue #44），省得每次手敲。",
    )


def _add_output_format_flags(p: argparse.ArgumentParser) -> None:
    """全局 ``--json`` / ``--text`` / ``--no-json``。

    默认 json 是 0.2.0 起的新行为（向 AI agent 倾斜）。``--no-json`` 是
    ``--text`` 的别名，方便顺手敲。

    所有 ``add_argument`` 均使用 ``default=argparse.SUPPRESS``，使 argparse
    仅在 flag 被显式传入时才向 namespace 写值，而非用 default 覆盖已有值。
    真正的全局默认值由顶层 ``parser.set_defaults(output_format=OUTPUT_JSON)``
    负责注入（见 ``build_parser``），子命令 subparser 直接调用本函数即可，
    不会产生"子 default 盖父 const"的经典 argparse 陷阱。
    """
    p.add_argument(
        "--json",
        dest="output_format",
        action="store_const",
        const=OUTPUT_JSON,
        default=argparse.SUPPRESS,
        help="输出 JSON 信封（默认）",
    )
    p.add_argument(
        "--text",
        dest="output_format",
        action="store_const",
        const=OUTPUT_TEXT,
        default=argparse.SUPPRESS,
        help="输出旧的人类可读字符串（不再加信封；errors 走 stderr）",
    )
    p.add_argument(
        "--no-json",
        dest="output_format",
        action="store_const",
        const=OUTPUT_TEXT,
        default=argparse.SUPPRESS,
        help="--text 别名",
    )


class _QuietPeekParser(argparse.ArgumentParser):
    """peek-parse 专用：error() 不往 stderr 打 usage/error，直接抛 SystemExit。

    peek parser 自身解析失败（如 `--text=x`）属预期内分支（调用方 catch 后
    回落 JSON 信封），不应在 stderr 留下第二段 usage 噪声（#118）。
    """

    def error(self, message: str) -> NoReturn:
        raise SystemExit(2)


class _EnvelopeArgumentParser(argparse.ArgumentParser):
    """覆写 argparse 的 error() —— 把用法错统一进 JSON 信封 + exit 64。

    不覆写 exit()，保住 --help 的 exit(0)。

    在 parse_args 调用时把 argv 存入类变量 _last_argv，子 parser 调用 error()
    时读同一份（类变量跨实例共享，整个 parse_args 栈只有一次顶层调用）。
    """

    _last_argv: list[str] = []

    def parse_args(  # type: ignore[override]
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        # 顶层 parse_args 调用时更新类变量；子 parser 不会再调用 parse_args，
        # 所以这里只会被根 parser 调到，子 parser error() 读的是同一份。
        _EnvelopeArgumentParser._last_argv = (
            list(args) if args is not None else sys.argv[1:]
        )
        return super().parse_args(args, namespace)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        # --text / --no-json 旁路判定：用 peek-parse 而非 token 扫描，
        # 避免 `--` 之后的字面量 --text 被误判为 text 模式旗标。
        # peek parser 用 _QuietPeekParser（非 _EnvelopeArgumentParser），
        # 既避免递归、也避免解析失败时往 stderr 重复打 usage（#118）。
        _peek = _QuietPeekParser(add_help=False, allow_abbrev=False)
        _add_output_format_flags(_peek)
        _is_text_mode = False
        try:
            _opts, _ = _peek.parse_known_args(_EnvelopeArgumentParser._last_argv)
            _is_text_mode = getattr(_opts, "output_format", OUTPUT_JSON) == OUTPUT_TEXT
        except SystemExit:
            # peek 解析异常（如 --text=x 等非法形式），保守回落到 JSON 信封（契约默认值）
            _is_text_mode = False
        if not _is_text_mode:
            _emit_error_payload(CLIENT_CODE_USAGE, f"{self.prog}: {message}")
        else:
            print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    from . import _version

    parser = _EnvelopeArgumentParser(
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
    # --port 与 --instance 互斥：agent 要么明确端口、要么指定实例名，不能两者并用。
    # 两者均未传时，CLI 通过 discover_port() 自动发现（0 实例→default fallback，
    # 1 实例→自动选中，N≥2→preflight 报歧义）。
    conn_grp = parser.add_mutually_exclusive_group()
    conn_grp.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"RPC 子命令连接的 GameBridge 端口（默认从 .cli_control/instances/<name>/port 读取，"
            f"legacy .cli_control/port 作为 fallback，否则 {DEFAULT_PORT}）。"
            "注意：仅作用于 RPC 子命令，daemon "
            "start / run 启动 daemon 时请用其各自的 --port。"
        ),
    )
    conn_grp.add_argument(
        "--instance",
        type=_instance_arg_allow_all,
        default=None,
        help=(
            "目标实例名；RPC 与 run/daemon 子命令通用（daemon 子命令的 --name 是等价写法）。"
            "多实例并行时必传；与 --port 互斥。传 all 对全部活实例广播（仅 RPC 子命令）。"
        ),
    )
    _add_output_format_flags(parser)
    # 全局默认：不传任何 output-format flag 时保持 JSON 输出。
    # 注意：各 subparser 中也调用 _add_output_format_flags，但均用 SUPPRESS 不写
    # default，故此处 set_defaults 是唯一的全局 fallback，不会被子命令覆盖。
    parser.set_defaults(output_format=OUTPUT_JSON)
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
    _add_instance_name_flag(start_p)
    start_p.add_argument(
        "--time-scale",
        type=_time_scale_arg,
        default=None,
        help="启动即设 Engine.time_scale（>0 且 <=100），整套 e2e 提速用",
    )
    _add_output_format_flags(start_p)

    stop_p = daemon_subs.add_parser(
        "stop",
        help="停止 daemon",
        description=(
            "停止 daemon。无 flag 时停 cwd 项目（自动选靶：若 cwd 有多实例须加 --name）；"
            "--all 停所有注册的 daemon；--project <path> 停指定项目；"
            "--name <inst> 选靶指定实例（与 --all 互斥）。"
        ),
    )
    # 注意：--all 与 --project 不再互斥（--all --project 允许"全停某项目"），
    # --all 与 --name 的互斥改为 cmd_daemon_stop 内显式校验（更灵活的错误消息）。
    stop_p.add_argument(
        "--all", action="store_true",
        help="停止注册的所有 daemon（配合 --project 可限定项目）"
    )
    stop_p.add_argument(
        "--project", type=Path, default=None,
        help="停止指定项目根的 daemon（绝对/相对路径均可）"
    )
    _add_instance_name_flag(stop_p)
    _add_output_format_flags(stop_p)

    status_p = daemon_subs.add_parser(
        "status",
        help="查询 daemon 状态",
        description=(
            "打印 daemon 状态到 stdout 并以 exit code 表示："
            "0 = 运行中（输出 running pid=<pid> port=<port> instance=<name>），"
            "1 = 未运行（输出 stopped）。"
            "多实例时用 --name 选靶；若只有一个实例在跑则自动选中。"
            "默认输出 JSON 信封；加 --text 切回旧的字符串格式。"
        ),
    )
    _add_instance_name_flag(status_p)
    _add_output_format_flags(status_p)

    logs_p = daemon_subs.add_parser(
        "logs",
        help="输出 godot.log 尾部（daemon 停了也能查）",
        description=(
            "直接输出 .cli_control/godot.log 的最后 N 行（JSON 信封包裹），"
            "免去先 daemon status 拿路径再 tail。纯客户端读文件：daemon "
            "已退出时同样可用（post-mortem 排查启动失败/崩溃）。"
            "多实例时用 --name 选靶。无日志文件 → -1006，exit 2。"
        ),
    )
    logs_p.add_argument(
        "--tail",
        type=_tail_arg,
        default=50,
        metavar="N",
        help="返回最后 N 行（1..1000，默认 50）",
    )
    _add_instance_name_flag(logs_p)
    _add_output_format_flags(logs_p)

    ls_p = daemon_subs.add_parser(
        "ls",
        help="列出所有正在运行的 daemon（跨项目）",
        description=(
            "扫描全局注册表（POSIX ~/.local/state/godot-cli-control/daemons/；"
            "Windows %LOCALAPPDATA%\\godot-cli-control\\daemons\\），"
            "列出所有探活通过的 daemon。死记录会被自动清理。"
            "JSON：{\"daemons\": [{\"pid\", \"port\", \"instance\", \"project_root\", ...}]}；"
            "Text：每行 pid\\tport\\tinstance\\tproject_root\\tstarted_at。"
        ),
    )
    _add_output_format_flags(ls_p)

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
    _add_instance_name_flag(run_p)
    run_p.add_argument(
        "--no-gui-auto",
        action="store_true",
        help=(
            "禁用脚本静态检测自动 GUI。默认含 screenshot 调用的脚本在非 TTY "
            "（subagent / pipe / CI）下也强制开窗 —— headless dummy renderer "
            "拿不到 viewport texture，截图会 1006 fail。"
        ),
    )
    _add_output_format_flags(run_p)

    # init：一键接入
    init_p = subs.add_parser(
        "init",
        help="在 Godot 项目根一键接入插件",
        description=(
            "复制 addons/godot_cli_control 到目标项目、patch project.godot 启用插件、"
            "校验 GODOT_BIN、在 .gitignore 忽略 .cli_control/。"
        ),
        epilog=(
            "GODOT_BIN 查找顺序：\n"
            "  1. 环境变量 GODOT_BIN\n"
            "  2. 项目根 .cli_control/godot_bin 文件（init 检测到时会写入）\n"
            "  3. macOS /Applications 与 ~/Applications 下的 "
            "Godot*.app/Contents/MacOS/Godot（系统级优先）\n"
            "  4. PATH 上的 godot4 / godot / Godot，"
            "及 mono 别名 godot4-mono / godot-mono / godot_mono / Godot_mono\n"
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
    addon_group = init_p.add_mutually_exclusive_group()
    addon_group.add_argument(
        "--force",
        action="store_true",
        help=(
            "覆盖已存在的 addons/godot_cli_control"
            "（现已是默认行为，本 flag 仅为兼容保留）"
        ),
    )
    addon_group.add_argument(
        "--keep-addon",
        action="store_true",
        help=(
            "已存在 addons/godot_cli_control 时跳过插件复制"
            "（保留本地版本，不随 CLI 升级刷新；默认会覆盖以同步版本）"
        ),
    )
    init_p.add_argument(
        "--skills-no-clobber",
        action="store_true",
        help=(
            "写 skill 时跳过已存在的 .claude/.codex SKILL.md（默认会覆盖以"
            "保证版本与 CLI 帮助同步）。与 --no-skills / --skills-only 都兼容。"
        ),
    )
    init_p.add_argument(
        "--no-gitignore",
        action="store_true",
        help=(
            "跳过往项目根 .gitignore 追加 .cli_control/（默认会追加，"
            "忽略 daemon 的机器本地状态目录）。--skills-only 模式下本就跳过。"
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
    _add_output_format_flags(init_p)

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
        _add_output_format_flags(sp)
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


def _rpc_failure_envelope(e: Exception) -> tuple[int, str, int]:
    """异常 → ``(client code, message, exit code)``，_run_rpc 与广播路径共用。

    isinstance 判定顺序必须与原 except 链一致：ConnectionError 与（3.11+ 的）
    TimeoutError 都是 OSError 子类，先窄后宽，否则错码漂移。

    各分支语义（原样搬运自 _run_rpc，历史注释见 git blame）：
    * RpcError → 服务端业务/协议错，exit 1。
    * ConnectionError → -1001，exit 2。
    * asyncio.TimeoutError → -1002，exit 2。
    * OSError → socket 类算 connection（-1001）；文件 IO 类（screenshot 写盘
      失败常见）走 -1004，不让 agent 误以为 daemon 挂了。exit 2。
    * ValueError / JSONDecodeError → 用法错 -1003，恒 exit 64（#82）。
    * 其余 Exception → 客户端内部 bug：traceback 留给 stderr 帮人 debug，
      stdout 信封只带异常类名（-1099），exit 2。
    """
    if isinstance(e, RpcError):
        return e.code, e.message, EXIT_RPC_ERROR
    if isinstance(e, ConnectionError):
        return CLIENT_CODE_CONNECTION, str(e) or e.__class__.__name__, EXIT_INFRA_ERROR
    if isinstance(e, asyncio.TimeoutError):
        return CLIENT_CODE_TIMEOUT, str(e) or "timed out", EXIT_INFRA_ERROR
    if isinstance(e, OSError):
        code = CLIENT_CODE_CONNECTION if _is_network_oserror(e) else CLIENT_CODE_IO
        return code, str(e) or e.__class__.__name__, EXIT_INFRA_ERROR
    if isinstance(e, (ValueError, json.JSONDecodeError)):
        return CLIENT_CODE_USAGE, str(e), EXIT_USAGE
    traceback.print_exc(file=sys.stderr)
    return CLIENT_CODE_INTERNAL, f"{type(e).__name__}: {e}", EXIT_INFRA_ERROR


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
    except Exception as e:  # noqa: BLE001 — 全部收口成信封（契约 1）；KeyboardInterrupt/SystemExit 照常传播
        code, msg, rc = _rpc_failure_envelope(e)
        _emit_envelope_error(fmt, code, msg)
        return rc

    _emit_rpc_result(spec, fmt, result)
    if spec.exit_code_from is not None:
        return spec.exit_code_from(result)
    return EXIT_OK


async def _run_rpc_broadcast(
    spec: RpcSpec, ns: argparse.Namespace, fmt: str
) -> int:
    """``--instance all``（#145）：对 cwd 项目全部活实例并发执行同一 RPC，聚合信封。

    * 目标 = ``list_live_instances(cwd)``（与 ``daemon stop --all --project`` 同一
      枚举路径）；0 个活实例 → -1006 + exit 2。legacy 平铺 daemon 不在目标内
      （``instances/`` 不存在时枚举为空），与 default 实例的 legacy 探活语义一致。
    * 逐实例 entry 复刻单命令信封（``ok`` + ``result``|``error``）+ ``instance`` +
      ``rc``——agent 复用同一套解析。``rc`` 按原命令语义算（exit_code_from /
      RPC 错=1 / 连接错=2）。数组按实例名排序。
    * 聚合退出码：全 0 → 0；任一非 0 → EXIT_PARTIAL(3)。顶层 ``ok`` 恒 true
      （广播本身执行了就算 ok，沿 stop --all 先例），失败细节在 entry。
    * asyncio.gather 并发：「同时给 4 个 client 截图」≈ 同一时刻；wait-* 总耗时
      = 最慢实例而非求和。单实例异常逐个捕获，不拖死其他。
    """
    from .daemon import Daemon, list_live_instances

    root = Path.cwd()
    names = list_live_instances(root)
    if not names:
        _emit_envelope_error(
            fmt,
            CLIENT_CODE_PRECONDITION,
            "no live instances to broadcast —— --instance all 只对 "
            ".cli_control/instances/ 下的活实例生效（legacy 平铺 daemon 不算）；"
            "先 daemon start --name <inst>",
        )
        return EXIT_INFRA_ERROR

    async def _one(name: str) -> dict[str, Any]:
        inst_ns = _namespace_for_instance(ns, name)
        try:
            port = Daemon(root, instance=name).current_port()
            if port is None:
                # 探活与读端口之间实例死了 / 启动中：瞬态，按连接错处理。
                raise ConnectionError(f"instance {name!r}: port file not readable")
            async with GameClient(port=port) as client:
                result = await spec.handler(client, inst_ns)
        except Exception as e:  # noqa: BLE001 — 单实例失败落 entry，不拖死其他
            code, msg, rc = _rpc_failure_envelope(e)
            return {
                "instance": name,
                "ok": False,
                "error": {"code": code, "message": msg},
                "rc": rc,
            }
        rc = spec.exit_code_from(result) if spec.exit_code_from is not None else EXIT_OK
        return {"instance": name, "ok": True, "result": result, "rc": rc}

    entries = list(await asyncio.gather(*(_one(n) for n in names)))
    # list_live_instances 已排序、gather 保序；显式再排一次把输出契约钉死。
    entries.sort(key=lambda e: e["instance"])
    agg_rc = EXIT_OK if all(e["rc"] == EXIT_OK for e in entries) else EXIT_PARTIAL
    if fmt == OUTPUT_JSON:
        _emit_success_payload({"instances": entries, "rc": agg_rc})
    else:
        for e in entries:
            if e["ok"]:
                # 空 formatter 输出（如 pressed 空列表）退化为裸 [name] 行，
                # 不留尾空格——广播下省略整行会让 agent 误以为实例丢了。
                text = spec.text_formatter(e["result"])
                print(f"[{e['instance']}] {text}" if text else f"[{e['instance']}]")
            else:
                print(
                    f"[{e['instance']}] error: [{e['error']['code']}] "
                    f"{e['error']['message']}",
                    file=sys.stderr,
                )
    return agg_rc


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
        if ns.action == "logs":
            sys.exit(cmd_daemon_logs(ns))
        if ns.action == "ls":
            sys.exit(cmd_daemon_ls(ns))
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

        # --instance all：广播路径（#145）——逐实例并发 + 聚合信封，
        # 不走单实例端口发现（preflight 已在上面跑过，screenshot 守卫等生效）。
        if ns.instance == "all":
            sys.exit(asyncio.run(_run_rpc_broadcast(spec, ns, fmt)))

        port = ns.port
        if port is None:
            # 与 GameClient()/GameBridge() 共用同一发现入口（issue #91）。
            # --instance 指定时传入，单实例自动选中，N≥2 且无 --instance 时
            # 抛 InstanceAmbiguityError → preflight exit 64（CLAUDE.md 契约 #5：
            # 用法错必须在连 daemon 之前报出，不让 agent 等 30s connection retry）。
            from .daemon import InstanceAmbiguityError, discover_port

            try:
                port = discover_port(instance=ns.instance) or DEFAULT_PORT
            except InstanceAmbiguityError as e:
                if fmt == OUTPUT_JSON:
                    _emit_error_payload(CLIENT_CODE_USAGE, str(e))
                else:
                    print(f"错误：{e}", file=sys.stderr)
                sys.exit(EXIT_USAGE)

        rc = asyncio.run(_run_rpc(spec, ns, port, fmt))
        sys.exit(rc)

    parser.error(f"unknown command: {ns.cmd}")


if __name__ == "__main__":
    main()
