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

import asyncio
import base64
import json
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from .client import GameClient

DEFAULT_PORT: int = 9877


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


async def cmd_move(client: GameClient, args: list[str]) -> None:
    if len(args) < 3:
        print("Usage: move <x> <y> <duration>", file=sys.stderr)
        sys.exit(1)
    result = await client.move(float(args[0]), float(args[1]), float(args[2]))
    print(f"moving: {result}")


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
    "move": cmd_move,
    "combo": cmd_combo,
    "release-all": cmd_release_all,
}


def main() -> None:
    args: list[str] = sys.argv[1:]
    port: int = DEFAULT_PORT

    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 >= len(args):
            print("Error: --port requires a value", file=sys.stderr)
            sys.exit(1)
        port = int(args[idx + 1])
        args = args[:idx] + args[idx + 2 :]

    if not args or args[0] not in COMMANDS:
        cmds = ", ".join(COMMANDS.keys())
        print(
            f"Usage: python3 -m godot_cli_control [--port N] <{cmds}> [args...]",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = args[0]

    async def run() -> None:
        async with GameClient(port=port) as client:
            await COMMANDS[cmd](client, args[1:])

    asyncio.run(run())


if __name__ == "__main__":
    main()
