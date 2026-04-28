"""CLI commands for godot-cli-control.

子命令分三组：

* **Daemon 管理**：``daemon start`` / ``daemon stop`` / ``run <script>`` —— 移植自
  原 bash wrapper，提供跨平台的 Godot 进程启停。
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
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from .client import DEFAULT_PORT, GameClient

# ── RPC 单发子命令（沿用既有实现） ──


async def cmd_click(client: GameClient, args: list[str]) -> None:
    if not args:
        print("Usage: click <node_path>", file=sys.stderr)
        sys.exit(1)
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
    if not args:
        print("Usage: press <action>", file=sys.stderr)
        sys.exit(1)
    result = await client.action_press(args[0])
    print(f"pressed: {result}")


async def cmd_release(client: GameClient, args: list[str]) -> None:
    if not args:
        print("Usage: release <action>", file=sys.stderr)
        sys.exit(1)
    result = await client.action_release(args[0])
    print(f"released: {result}")


async def cmd_tap(client: GameClient, args: list[str]) -> None:
    if not args:
        print("Usage: tap <action> [duration]", file=sys.stderr)
        sys.exit(1)
    duration = float(args[1]) if len(args) > 1 else 0.1
    result = await client.action_tap(args[0], duration)
    print(f"tapped: {result}")


async def cmd_hold(client: GameClient, args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: hold <action> <duration>", file=sys.stderr)
        sys.exit(1)
    result = await client.hold(args[0], float(args[1]))
    print(f"holding: {result}")


async def cmd_combo(client: GameClient, args: list[str]) -> None:
    if not args:
        print("Usage: combo <json_file>", file=sys.stderr)
        sys.exit(1)
    steps = json.loads(Path(args[0]).read_text())
    if isinstance(steps, dict):
        steps = steps.get("steps", [])
    result = await client.combo(steps)
    print(f"combo done: {result}")


async def cmd_release_all(client: GameClient, _args: list[str]) -> None:
    result = await client.release_all()
    print(f"released all: {result}")


RPC_COMMANDS: dict[
    str, Callable[[GameClient, list[str]], Coroutine[Any, Any, None]]
] = {
    "click": cmd_click,
    "screenshot": cmd_screenshot,
    "tree": cmd_tree,
    "press": cmd_press,
    "release": cmd_release,
    "tap": cmd_tap,
    "hold": cmd_hold,
    "combo": cmd_combo,
    "release-all": cmd_release_all,
}


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
    exit_code = _exec_user_script(script_path, port)

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
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 - 用户脚本任何异常都要抓
        print(f"错误：加载脚本失败: {e}", file=sys.stderr)
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
    except Exception as e:  # noqa: BLE001
        print(f"错误：脚本运行失败: {e}", file=sys.stderr)
        return 1
    finally:
        bridge.close()
    return 0


def cmd_init(ns: argparse.Namespace) -> int:
    from .init_cmd import run_init

    return run_init(
        project_root=Path(ns.path).resolve() if ns.path else Path.cwd(),
        force=ns.force,
    )


# ── argparse 装配 ──


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="godot-cli-control")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"GameBridge 端口（默认从 .cli_control/port 读取，否则 {DEFAULT_PORT}）",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    # daemon 组
    daemon_p = subs.add_parser("daemon", help="管理 Godot daemon 进程")
    daemon_subs = daemon_p.add_subparsers(dest="action", required=True)

    start_p = daemon_subs.add_parser("start", help="启动 daemon")
    start_p.add_argument("--record", action="store_true")
    start_p.add_argument("--movie-path", default=None)
    start_p.add_argument("--headless", action="store_true")
    start_p.add_argument("--fps", type=int, default=30)
    start_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    daemon_subs.add_parser("stop", help="停止 daemon")

    # run：自动启停 + 跑用户脚本
    run_p = subs.add_parser("run", help="启动 daemon → 跑脚本 → 停 daemon")
    run_p.add_argument("script", help="用户脚本路径，需定义 run(bridge)")
    run_p.add_argument("--record", action="store_true")
    run_p.add_argument("--movie-path", default=None)
    run_p.add_argument("--headless", action="store_true")
    run_p.add_argument("--fps", type=int, default=30)
    run_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    # init：一键接入
    init_p = subs.add_parser("init", help="在 Godot 项目根一键接入插件")
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

    # RPC 单发命令
    for name in RPC_COMMANDS:
        sp = subs.add_parser(name, help="RPC 单发")
        sp.add_argument("rest", nargs="*")
    return parser


def main() -> None:
    parser = _build_parser()
    ns = parser.parse_args()

    if ns.cmd == "daemon":
        sys.exit(
            cmd_daemon_start(ns) if ns.action == "start" else cmd_daemon_stop(ns)
        )
    if ns.cmd == "run":
        sys.exit(cmd_run(ns))
    if ns.cmd == "init":
        sys.exit(cmd_init(ns))

    # RPC：解析端口（顶层 --port → port file → 默认）
    if ns.cmd in RPC_COMMANDS:
        port = ns.port
        if port is None:
            from .daemon import Daemon

            port = Daemon(Path.cwd()).current_port() or DEFAULT_PORT

        async def run() -> None:
            async with GameClient(port=port) as client:
                await RPC_COMMANDS[ns.cmd](client, list(ns.rest))

        asyncio.run(run())
        return

    parser.error(f"unknown command: {ns.cmd}")


if __name__ == "__main__":
    main()
