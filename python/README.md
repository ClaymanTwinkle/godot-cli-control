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

`init` also writes `.claude/skills/godot-cli-control/SKILL.md` and `.codex/skills/godot-cli-control/SKILL.md` so AI agents working in your Godot project can pick up this CLI surface automatically. Use `--no-skills` to skip, or `--skills-only` to refresh just those files after a CLI upgrade. See the [top-level README](../README.md#agent-integration) for details.

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

The CLI is the canonical surface — every `GameClient` method has a one-line equivalent. Default output is a JSON envelope (`--text` for legacy strings).

```bash
# Lifecycle
godot-cli-control init [--path DIR] [--force]
godot-cli-control daemon start [--headless --record --movie-path X --fps N --port N]
godot-cli-control daemon stop
godot-cli-control daemon status
godot-cli-control run <script.py> [--headless ...]

# Read
godot-cli-control tree [depth]
godot-cli-control get      <node_path> <prop>
godot-cli-control text     <node_path>
godot-cli-control exists   <node_path>      # exit 0=true, 1=false, 2=infra
godot-cli-control visible  <node_path>      # exit 0=true, 1=false, 2=infra
godot-cli-control children <node_path> [type-filter]
godot-cli-control pressed
godot-cli-control actions [--all]

# Write / call
godot-cli-control set   <node_path> <prop>   <json-value>
godot-cli-control call  <node_path> <method> [json-args...]
godot-cli-control click <node_path>

# Input
godot-cli-control press|release <action>
godot-cli-control tap   <action> [duration]
godot-cli-control hold  <action>  <duration>
godot-cli-control combo --steps-json '[...]'   # or `combo file.json` / `combo -` (stdin)
godot-cli-control combo-cancel
godot-cli-control release-all

# Wait
godot-cli-control wait-node <node_path> [timeout]   # exit 0=found, 1=timeout
godot-cli-control wait-time <seconds>

# Render (path is required as of 0.2.0)
godot-cli-control screenshot <output.png>
```

### Output contract

- success: `{"ok": true, "result": <data>}` on stdout, exit 0
- error:   `{"ok": false, "error": {"code": N, "message": "..."}}` on stdout, exit 1 (RPC) or 2 (connection / usage)
- `--text` / `--no-json` switches back to the legacy human-readable strings; errors then go to stderr.

`exists` / `visible` / `wait-node` propagate their boolean result to the exit code, so shell `if` works:

```bash
if godot-cli-control exists /root/Main/Boss; then
  godot-cli-control click /root/Main/Boss
fi
```

The port is read from `.cli_control/port` if you don't pass `--port`, so RPC calls just work after `daemon start`.

## Testing

```bash
# Python unit tests + coverage (fails if below 80%)
pip install -e ".[test]"
coverage run -m pytest python/tests/
coverage report

# GUT tests for the Godot plugin (needs GODOT_BIN env var)
GODOT_BIN=/path/to/godot ./addons/godot_cli_control/tests/run_gut.sh
```

## Documentation

See the [Godot plugin README](https://github.com/ClaymanTwinkle/godot-cli-control/blob/main/addons/godot_cli_control/README.md) for the full RPC reference, activation modes, security model, and known limitations.
