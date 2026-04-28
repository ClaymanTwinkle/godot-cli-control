# godot-cli-control

WebSocket bridge for headless / scripted control of Godot 4 scenes — Python client + CLI.

## Install

```bash
pipx install godot-cli-control

# or, for unreleased main:
pipx install "git+https://github.com/ClaymanTwinkle/godot-cli-control.git#subdirectory=python"
```

The wheel ships the Godot plugin source so the `init` command can drop it into your project.

Requires Python ≥ 3.10.

## One-shot setup of a Godot project

```bash
cd path/to/your_godot_project
godot-cli-control init        # copies plugin, patches project.godot, detects Godot binary
godot-cli-control daemon start
godot-cli-control tree 3
godot-cli-control daemon stop
```

`init` is idempotent — running it twice on the same project does nothing the second time. Pass `--force` to overwrite an existing `addons/godot_cli_control/`.

## Async API

```python
import asyncio
from godot_cli_control import GameClient

async def main():
    async with GameClient(port=9877) as client:
        tree = await client.get_scene_tree(depth=3)
        await client.click("/root/MyScene/Button")
        await client.action_press("jump")
        await client.wait_game_time(0.5)
        await client.action_release("jump")
        png_bytes = await client.screenshot()
        open("frame.png", "wb").write(png_bytes)

asyncio.run(main())
```

## Sync API (for scripts and tests)

```python
# script.py
def run(bridge):
    bridge.click("/root/MyScene/StartButton")
    bridge.wait(2)
    bridge.tap("attack")
```

```bash
godot-cli-control run script.py --headless     # auto-starts and stops the daemon
```

## pytest fixtures

```bash
pip install "godot-cli-control[pytest]"
```

The package ships a pytest plugin (auto-loaded via `pytest11` entry-point):

```python
# tests/test_jump.py — no fixture boilerplate needed
def test_jump(godot_daemon, bridge):
    bridge.click("/root/Game/Start")
    bridge.tap("jump")
    assert bridge.get_property("/root/Player", "on_floor") is False
```

- `godot_daemon` (session-scoped) starts headless Godot once and stops it after all tests; if a daemon is already running it's reused (and not stopped at teardown — keeps your IDE workflow alive).
- `bridge` (function-scoped) gives a fresh `GameBridge`; on teardown it calls `release_all()` so a `hold` left behind by one case can't bleed into the next, then closes the connection.

CLI options:

```
--godot-cli-port=N           # GameBridge port (default 9877)
--godot-cli-no-headless      # open a real Godot window
--godot-cli-project-root=DIR # default: pytest rootdir
```

## CLI

```bash
godot-cli-control init [--path DIR] [--force]
godot-cli-control daemon start [--headless --record --movie-path X --fps N --port N]
godot-cli-control daemon stop
godot-cli-control run <script.py> [--headless ...]
godot-cli-control tree [depth]
godot-cli-control click <node_path>
godot-cli-control screenshot [output.png]
godot-cli-control press|release|tap|hold|combo|release-all <args...>
```

The port is read from `.cli_control/port` if you don't pass `--port`, so RPC calls just work after `daemon start`.

## Documentation

See the [Godot plugin README](https://github.com/ClaymanTwinkle/godot-cli-control/blob/main/addons/godot_cli_control/README.md) for the full RPC reference, activation modes, security model, and known limitations.
