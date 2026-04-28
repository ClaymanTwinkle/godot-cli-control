"""CLI commands for one-off GameClient operations.

Usage:
    python3 -m godot_cli_control click <node_path>
    python3 -m godot_cli_control screenshot [output.png]
    python3 -m godot_cli_control tree [depth]
    python3 -m godot_cli_control press <action_name>
    python3 -m godot_cli_control hold <action_name> <duration>
    python3 -m godot_cli_control combo <sequence.json>

<action_name> 依赖目标工程的 InputMap；<node_path> 形如
/root/<Scene>/... 由 `tree` 查出。
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


COMMANDS: dict[
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="godot_cli_control")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"GameBridge 端口（默认 {DEFAULT_PORT}）",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        sp = subs.add_parser(name)
        sp.add_argument("rest", nargs="*")
    return parser


def main() -> None:
    parser = _build_parser()
    ns = parser.parse_args()

    async def run() -> None:
        async with GameClient(port=ns.port) as client:
            await COMMANDS[ns.cmd](client, list(ns.rest))

    asyncio.run(run())


if __name__ == "__main__":
    main()
