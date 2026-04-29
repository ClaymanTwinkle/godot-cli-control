"""CLI commands for godot-cli-control.

子命令分三组：

* **Daemon 管理**：``daemon start`` / ``daemon stop`` / ``daemon status`` /
  ``run <script>`` —— 移植自原 bash wrapper，提供跨平台的 Godot 进程启停。
* **接入**：``init`` —— 在 Godot 项目根一键复制插件、patch ``project.godot``、
  检测 GODOT_BIN。
* **RPC 单发**：``click`` / ``tree`` / ``screenshot`` / ``press`` / ``release`` /
  ``tap`` / ``hold`` / ``combo`` / ``release-all`` —— 与已运行的 daemon 交互。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import DEFAULT_PORT, GameClient

# ── RPC 单发子命令（沿用既有实现） ──


async def cmd_click(client: GameClient, args: list[str]) -> None:
    result = await client.click(args[0])
    print(f"clicked: {result}")


async def cmd_screenshot(client: GameClient, args: list[str]) -> None:
    data = await client.screenshot()
    if args:
        output = Path(args[0])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        print(f"screenshot saved: {output} ({len(data)} bytes)")
    else:
        print(base64.b64encode(data).decode())


async def cmd_tree(client: GameClient, args: list[str]) -> None:
    depth = int(args[0]) if args else 3
    tree = await client.get_scene_tree(depth=depth)
    print(json.dumps(tree, indent=2, ensure_ascii=False))


async def cmd_press(client: GameClient, args: list[str]) -> None:
    result = await client.action_press(args[0])
    print(f"pressed: {result}")


async def cmd_release(client: GameClient, args: list[str]) -> None:
    result = await client.action_release(args[0])
    print(f"released: {result}")


async def cmd_tap(client: GameClient, args: list[str]) -> None:
    duration = float(args[1]) if len(args) > 1 else 0.1
    result = await client.action_tap(args[0], duration)
    print(f"tapped: {result}")


async def cmd_hold(client: GameClient, args: list[str]) -> None:
    result = await client.hold(args[0], float(args[1]))
    print(f"holding: {result}")


async def cmd_combo(client: GameClient, args: list[str]) -> None:
    steps = json.loads(Path(args[0]).read_text())
    if isinstance(steps, dict):
        steps = steps.get("steps", [])
    result = await client.combo(steps)
    print(f"combo done: {result}")


async def cmd_release_all(client: GameClient, _args: list[str]) -> None:
    result = await client.release_all()
    print(f"released all: {result}")


@dataclass(frozen=True)
class Positional:
    """RPC 子命令的位置参数描述（喂给 argparse + 帮助文本）。"""

    name: str  # ns 上的属性名
    nargs: str | None  # None=必填一个；"?"=可选；"*"=0..N
    help: str


@dataclass(frozen=True)
class RpcSpec:
    name: str
    handler: Callable[[GameClient, list[str]], Coroutine[Any, Any, None]]
    description: str
    positionals: tuple[Positional, ...]
    example: str  # 不带 prog 前缀的示例命令，如 "click /root/Main/StartButton"
    # 额外 epilog 段（schema、注意事项等），位于 "示例:" 段之后。空 = 不附加。
    extra_epilog: str = ""


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
    ),
    RpcSpec(
        name="screenshot",
        handler=cmd_screenshot,
        description=(
            "截屏。带路径则保存 PNG（推荐），不带则把 base64 写到 stdout"
            "（量大，几 MB 起步，常会撑爆 LLM 上下文）。"
        ),
        positionals=(
            Positional(
                "output_path",
                "?",
                "PNG 输出路径；省略则输出 base64（建议总是给路径）",
            ),
        ),
        example="screenshot out.png",
    ),
    RpcSpec(
        name="tree",
        handler=cmd_tree,
        description="dump 当前场景树为 JSON。",
        positionals=(
            Positional("depth", "?", "遍历深度，默认 3"),
        ),
        example="tree 3",
    ),
    RpcSpec(
        name="press",
        handler=cmd_press,
        description="按下输入动作（持续按住，需配 release 释放）。",
        positionals=(
            Positional("action", None, "InputMap 动作名，如 jump"),
        ),
        example="press jump",
    ),
    RpcSpec(
        name="release",
        handler=cmd_release,
        description="释放之前 press 按下的输入动作。",
        positionals=(
            Positional("action", None, "InputMap 动作名"),
        ),
        example="release jump",
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
    ),
    RpcSpec(
        name="combo",
        handler=cmd_combo,
        description="从 JSON 文件读步骤数组并依次执行（按 action / 等 wait 秒）。",
        positionals=(
            Positional(
                "json_file",
                None,
                "JSON 文件路径；可为 [...steps] 或 {\"steps\": [...]}",
            ),
        ),
        example="combo combo.json",
        extra_epilog=(
            "step schema（每个 step 二选一，按数组顺序串行执行）:\n"
            "  {\"action\": \"<InputMap 动作名>\", \"duration\": <秒，默认 0.1>}\n"
            "      —— 按下 action，等 duration 秒后自动释放\n"
            "  {\"wait\": <秒>}\n"
            "      —— 不动作，纯等待\n"
            "\n"
            "最小可跑 combo.json:\n"
            "  [\n"
            "    {\"action\": \"jump\", \"duration\": 0.2},\n"
            "    {\"wait\": 0.5},\n"
            "    {\"action\": \"attack\"}\n"
            "  ]\n"
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
    ),
)


RPC_BY_NAME: dict[str, RpcSpec] = {s.name: s for s in RPC_SPECS}


# ── Daemon / run 子命令 ──


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
        print(f"错误：{e}", file=sys.stderr)
        return 1
    return 0


def cmd_daemon_stop(_ns: argparse.Namespace) -> int:
    from .daemon import Daemon, DaemonError

    daemon = Daemon(Path.cwd())
    try:
        return daemon.stop()
    except DaemonError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


def cmd_daemon_status(_ns: argparse.Namespace) -> int:
    """打印 daemon 状态；exit 0=运行中，1=未运行。

    用 stdout 输出 ``running pid=<pid> port=<port>`` 或 ``stopped``，方便脚本
    grep；exit code 让 shell `if godot-cli-control daemon status; then …` 直
    接可用，避免 RPC 调用前先靠 ``tree`` 失败试探。
    """
    from .daemon import Daemon

    daemon = Daemon(Path.cwd())
    if daemon.is_running():
        pid = daemon.read_pid()
        port = daemon.current_port()
        print(f"running pid={pid} port={port if port is not None else '?'}")
        return 0
    print("stopped")
    return 1


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
        bridge = GameBridge(port=port)
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


# ── argparse 装配 ──


_TOP_EPILOG = """\
命令分组：

  Daemon 管理:
    daemon start    启动 Godot daemon（可选录制 / headless）
    daemon stop     停止当前 daemon
    daemon status   显示当前 daemon 状态（pid / port），exit 0=运行中，1=未运行
    run <script>    自动启停 daemon 并跑用户脚本（脚本需定义 run(bridge)）

  接入:
    init            在 Godot 项目根一键复制插件、patch project.godot

  RPC 单发（需先有 daemon 在跑）:
    click           对节点触发点击
    screenshot      截屏（PNG 或 base64）
    tree            dump 场景树 JSON
    press / release 按下 / 释放输入动作
    tap / hold      短按 / 按住一段时长
    combo           从 JSON 文件批量执行输入动作
    release-all     释放所有当前持有的动作

注意：除 RPC 子命令外，仅这里这条 CLI 链上有 click/tree/screenshot 等
能力；GameClient（Python）还提供 get_text / get_property /
wait_for_node 等额外方法（不是 CLI 子命令）。需要在 shell 里多步操作时
请写 `def run(bridge):` 脚本走 `godot-cli-control run`。

任意子命令后追加 -h 查看详情，例如：
  godot-cli-control click -h
  godot-cli-control daemon start -h
  godot-cli-control combo -h        # 含 step JSON schema 与示例
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
    subs = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # daemon 组
    daemon_p = subs.add_parser(
        "daemon",
        help="管理 Godot daemon 进程",
        description="管理 Godot daemon 进程的启停与状态查询。",
    )
    daemon_subs = daemon_p.add_subparsers(dest="action", required=True, metavar="<action>")

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
        epilog = f"示例:\n  godot-cli-control {spec.example}"
        if spec.extra_epilog:
            epilog += "\n\n" + spec.extra_epilog
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
        port = ns.port
        if port is None:
            from .daemon import Daemon

            port = Daemon(Path.cwd()).current_port() or DEFAULT_PORT

        # 按 spec.positionals 从 ns 收集参数为 list[str]，handler 签名不变。
        # 各 nargs 在 ns 上的形态：None → str；"?" → str | None；"*"/"+" → list[str]。
        rpc_args: list[str] = []
        for pos in spec.positionals:
            val = getattr(ns, pos.name)
            if val is None:
                continue
            if isinstance(val, list):
                rpc_args.extend(val)
            else:
                rpc_args.append(val)

        async def run() -> None:
            async with GameClient(port=port) as client:
                await spec.handler(client, rpc_args)

        asyncio.run(run())
        return

    parser.error(f"unknown command: {ns.cmd}")


if __name__ == "__main__":
    main()
