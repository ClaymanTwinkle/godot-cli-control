# godot-cli-control

WebSocket bridge for headless / scripted control of Godot 4 scenes — Python client + CLI.

## Install

```bash
pipx install godot-cli-control

# or, for unreleased main:
pipx install "git+https://github.com/ClaymanTwinkle/godot-cli-control.git"
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

Re-running `init` refreshes both `addons/godot_cli_control/` and the SKILL.md files to match the installed CLI version (the plugin directory is wiped and re-copied; `project.godot` patching stays idempotent). Pass `--keep-addon` to keep an existing `addons/godot_cli_control/` untouched.

`init` also writes `.claude/skills/godot-cli-control/SKILL.md` and `.codex/skills/godot-cli-control/SKILL.md` so AI agents working in your Godot project can pick up this CLI surface automatically. Use `--no-skills` to skip, or `--skills-only` to refresh just those files after a CLI upgrade. See the [top-level README](../README.md#agent-integration) for details.

## Async API

```python
import asyncio
from godot_cli_control import GameClient

async def main():
    # Omitting port lets GameClient auto-discover from .cli_control/port (written by daemon start)
    async with GameClient() as client:
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
- `bridge` (function-scoped) gives a fresh `GameBridge`; on teardown it best-effort restores global engine state — `unpause()`, `time_scale` back to its setup-time snapshot, `release_all()` — so a `hold`/`pause`/speed-up left behind by one case can't bleed into the next, then closes the connection.
- `fresh_scene` (function-scoped, opt-in) calls `scene_reload()` at setup so the case starts on a pristine scene. Node references cached before the reload are invalid afterwards.
- `no_push_errors` (function-scoped, opt-in) records an `errors` marker at setup and fails the case if any new `push_error` was emitted during it (warnings don't fail). Requires Godot 4.5+ (Logger API) — older engines raise `RpcError` 1012 at setup, loudly.
- `godot_instances` (scope configurable, default function) is a multi-instance factory for multiplayer e2e — start a named server **and** clients inside one test and get connected `GameBridge` objects back; teardown stops everything the fixture started (and only that):

  ```python
  def test_join(godot_instances):
      server = godot_instances.start("server")
      client = godot_instances.start("client1")
  ```

  `start(name)` is idempotent get-or-start (`headless`/`time_scale` follow the global options, overridable per call; `port` always defaults to 0 = OS-assigned); `stop(name)` stops one instance mid-test (restartable); `daemon(name)` exposes the underlying `Daemon`. `--godot-cli-instances-scope session` shares one set of instances across the whole suite (faster, no state isolation between tests).
- On a test failure (non-headless daemon only) a best-effort screenshot is saved to `.cli_control/failures/<nodeid>.png` and the path is attached to the pytest report.

CLI options:

```
--godot-cli-port=N           # GameBridge port (default: read from .cli_control/port)
--godot-cli-no-headless      # open a real Godot window
--godot-cli-project-root=DIR # default: pytest rootdir
--godot-cli-time-scale=X     # Engine.time_scale applied at daemon startup (e.g. 5 to speed up the suite)
--godot-cli-instances-scope=function|session  # godot_instances fixture scope (default: function)
```

## CLI

The CLI is the canonical surface — every `GameClient` method has a one-line equivalent. Default output is a JSON envelope (`--text` for legacy strings).

```bash
# Lifecycle
godot-cli-control init [--path DIR] [--keep-addon]
godot-cli-control daemon start [--headless | --gui] [--port N --idle-timeout 30m]
godot-cli-control daemon start --record --movie-path X [--fps N]   # 录制需真实渲染器，不能与 --headless 同用
godot-cli-control daemon stop [--all | --project PATH]
godot-cli-control daemon status
godot-cli-control daemon ls                  # list running daemons across all projects
godot-cli-control daemon logs [--tail N]     # last N lines of .cli_control/godot.log (works post-mortem)
godot-cli-control run <script.py> [--headless ...]

# Read
godot-cli-control tree [depth]
godot-cli-control get      <node_path> <prop> [prop2 ...]   # multi-prop = atomic same-frame read
godot-cli-control text     <node_path>
godot-cli-control exists   <node_path>      # exit 0=true, 1=false, 2=infra
godot-cli-control visible  <node_path>      # exit 0=true, 1=false, 2=infra
godot-cli-control children <node_path> [type-filter]
godot-cli-control pressed
godot-cli-control actions [--all]
godot-cli-control sprite-info <node_path>   # Sprite2D/AnimatedSprite2D/TextureRect render state in one call
godot-cli-control errors [--since MARKER] [--limit N]   # structured push_error/push_warning log (Godot 4.5+)

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

# Wait (exit 0=hit, 1=timeout)
godot-cli-control wait-node   <node_path> [timeout]
godot-cli-control wait-prop   <node_path> <prop> <value> [--op gt|lt|ge|le|ne] [--timeout S] [--tolerance T]
godot-cli-control wait-signal <node_path> <signal> [timeout]   # arm BEFORE triggering the action
godot-cli-control wait-frames <n> [--physics]
godot-cli-control wait-time <seconds>        # game time — scales with time-scale

# Scene isolation
godot-cli-control scene-reload [--timeout S]            # reload current scene, block until ready
godot-cli-control scene-change <res://path.tscn>        # switch scene, block until ready

# Time control
godot-cli-control time-scale [value]         # read (no arg) or set Engine.time_scale, (0, 100]
godot-cli-control pause | unpause
godot-cli-control step-frames <n> [--physics]   # deterministic stepping while paused

# Render (path is required as of 0.2.0)
godot-cli-control screenshot <output.png> [--node <node_path>]   # --node crops to that node's screen rect
```

### Output contract

- success: `{"ok": true, "result": <data>}` on stdout, exit 0
- error:   `{"ok": false, "error": {"code": N, "message": "..."}}` on stdout, exit 1 (RPC), 2 (connection / infra), or 64 (usage)
- `--text` / `--no-json` switches back to the legacy human-readable strings; errors then go to stderr.

`exists` / `visible` and the `wait-*` commands propagate their boolean / hit-or-timeout result to the exit code, so shell `if` works:

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
# bash (Linux/macOS):
GODOT_BIN=/path/to/godot ./addons/godot_cli_control/tests/run_gut.sh
# cross-platform (Linux/macOS/Windows) — what CI runs:
GODOT_BIN=/path/to/godot python addons/godot_cli_control/tests/run_gut.py
```

## Documentation

See the [Godot plugin README](https://github.com/ClaymanTwinkle/godot-cli-control/blob/main/addons/godot_cli_control/README.md) for the full RPC reference, activation modes, security model, and known limitations.
