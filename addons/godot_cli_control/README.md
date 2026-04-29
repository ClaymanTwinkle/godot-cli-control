# Godot CLI Control

WebSocket bridge for headless / scripted control of Godot 4 scenes.
Click nodes, read/write properties, simulate input, take screenshots, record movies — all from a Python or shell client.

## Quick Start (one command)

If you have the Python CLI installed (`pipx install godot-cli-control`), the entire setup collapses to:

```bash
cd your_godot_project
godot-cli-control init        # copy plugin, patch project.godot, detect Godot
godot-cli-control daemon start
godot-cli-control tree 3
godot-cli-control daemon stop
```

`init` automates everything described in the manual setup below — copying the addon, editing `project.godot`, and detecting your Godot binary.

## Manual Setup (if you prefer)

### 1. Get the plugin

**Option A — Godot AssetLib** (once approved): in the Godot Editor, AssetLib tab → search "Godot CLI Control" → Download → Install.

**Option B — copy from a clone:**

```bash
cp -r /path/to/godot-cli-control/addons/godot_cli_control my_project/addons/
```

**Option C — direct zip download**: grab `godot-cli-control-vX.Y.Z.zip` from the [GitHub releases](https://github.com/ClaymanTwinkle/godot-cli-control/releases) and unzip into the project root (zip layout is `addons/godot_cli_control/...`).

### 2. Get the Python package

```bash
pip install godot-cli-control
# or for an unreleased main:
# pip install -e /path/to/godot-cli-control/python
```

Requires Python ≥ 3.10.

### 3. Enable in Godot Editor

Open your project in Godot 4 → Project → Project Settings → Plugins → "Godot CLI Control" → Enabled.

The plugin auto-registers an autoload `GameBridgeNode`. No manual `project.godot` edits needed.

> **Note**: Headless `--editor --quit` may not persist plugin enable to `project.godot`. If you need to enable the plugin in CI / scripted environment, either run `godot-cli-control init` (recommended) or manually add `[editor_plugins] enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")` and `[autoload] GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"` to your `project.godot`.

### 4. Run

After step 1–3, the easiest way is the Python CLI installed by `pip`:

```bash
godot-cli-control daemon start
godot-cli-control tree 3
godot-cli-control screenshot /tmp/test.png
godot-cli-control daemon stop
```

Compatibility shims are also kept at `addons/godot_cli_control/bin/run_cli_control.sh` (bash) and `addons/godot_cli_control/bin/run_cli_control.ps1` (PowerShell, for native Windows / pwsh users) — both forward every subcommand to `python -m godot_cli_control`. **They are deprecated since 0.1.6 and slated for removal in 0.3.0**; new code should call `godot-cli-control <subcommand>` directly.

## Running the GUT unit tests

```bash
GODOT_BIN=/path/to/godot ./addons/godot_cli_control/tests/run_gut.sh
```

The runner builds a throwaway Godot project, `git clone`s a pinned [GUT](https://github.com/bitwes/Gut) release into `addons/gut/`, copies this plugin in, and runs the test files under `addons/godot_cli_control/tests/gut/`. Coverage today is `LowLevelApi` handler boundaries (blacklist, missing-property, node-not-found) and `InputSimulationApi` state machine (combo / press / release / tap / release_all).

GUT itself is **not** vendored into the repo or shipped with the wheel/AssetLib zip — it's a dev-only dependency.

## RPC Reference

All methods callable via `godot-cli-control <method>` or `from godot_cli_control import GameClient`.

| Method | Example |
|---|---|
| `click(path)` | `await client.click("/root/MyScene/Button")` |
| `get_property(path, property)` | `await client.get_property("/root/Player", "position")` |
| `set_property(path, property, value)` | `await client.set_property("/root/Player", "visible", False)` |
| `call_method(path, method, args)` | `await client.call_method("/root/Player", "take_damage", [10])` |
| `get_text(path)` | `await client.get_text("/root/UI/Label")` |
| `node_exists(path)` | `await client.node_exists("/root/MyScene/Button")` |
| `is_visible(path)` | `await client.is_visible("/root/UI/Panel")` |
| `get_children(path, type_filter?)` | `await client.get_children("/root/Map", "Node2D")` |
| `get_scene_tree(depth)` | `await client.get_scene_tree(depth=3)` |
| `screenshot()` | `png_bytes = await client.screenshot()` |
| `wait_for_node(path, timeout)` | `await client.wait_for_node("/root/Boss", timeout=10.0)` |
| `wait_game_time(seconds)` | `await client.wait_game_time(2.5)` |
| `action_press(action)` | `await client.action_press("jump")` |
| `action_release(action)` | `await client.action_release("jump")` |
| `action_tap(action, duration)` | `await client.action_tap("attack", 0.1)` |
| `input_get_pressed` (raw RPC) | `await client.request("input_get_pressed")` |
| `hold(action, duration)` | `await client.hold("run", 1.5)` |
| `combo(steps)` | `await client.combo([{"action": "jump", "duration": 0.1}])` |
| `combo_cancel()` | `await client.combo_cancel()` |
| `release_all()` | `await client.release_all()` |

Error codes: `-32600` invalid request, `-32601` unknown method, `-32602` invalid params, `1001` node not found, `1002` property not found, `1003` method not found.

## Activation Modes

The plugin defaults to **OFF** even when enabled — you must trigger one of three activation paths:

| Path | Activate by | Use case |
|---|---|---|
| **CLI flag** | Pass `--cli-control` to Godot binary | Wrapper script / pytest / CI (this is what `daemon start` uses) |
| **Env var** | `export GODOT_CLI_CONTROL=1` before launching Godot | Editor F5 with launch args |
| **Project Setting** | Enable `godot_cli_control/auto_enable_in_debug` in Project Settings | Editor-only debugging; **release builds always disabled regardless** |

If none triggered, the plugin prints `[Godot CLI Control] inactive — ...` to console and self-destructs the autoload.

## Security Model

- **TCP server binds 127.0.0.1 only** (never 0.0.0.0). Use SSH tunnel for remote.
- **Default OFF + release-disabled Project Setting path** prevents accidentally shipping the WebSocket server to players.
- **PID/port files have mode 0600** (only the running user can read).
- **Method/property blacklist**: `queue_free`, `set_script`, `add_child`, `texture`, `material`, `script`, `process_mode`, etc., are blocked from RPC mutation. Custom safe hooks: define methods on your business nodes and call via `call_method`.
- **No auth token** — localhost binding is the primary boundary. Multi-user dev machines should keep daemon stopped when not in use.

## Known Limitations

1. **Headless + Movie Maker is broken** (Godot upstream): `--write-movie` produces empty MP4 in headless mode (no framebuffer). Use GUI mode for recording.
2. **Godot binary detection** falls back to `GODOT_BIN` env var. `init` writes `.cli_control/godot_bin` after autodetect; daemon reads that file before scanning. Detection covers macOS `/Applications/Godot*.app`, `PATH` (`godot`/`godot4`), and Windows `Program Files\Godot*`.
3. **Python ≥ 3.10** required (websockets>=14 dependency).
4. **Single-client mode**: only one WebSocket client at a time; second connection rejected.
5. **Headless plugin auto-register**: in headless mode, `--editor --quit` may not trigger `plugin._enter_tree` to write the autoload. `init` handles this automatically by patching `project.godot` directly.

## Links

- Python package: see `python/` directory in this repo
- Source: https://github.com/ClaymanTwinkle/godot-cli-control
- Issues: https://github.com/ClaymanTwinkle/godot-cli-control/issues
