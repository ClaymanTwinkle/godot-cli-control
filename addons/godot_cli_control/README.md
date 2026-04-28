# Godot CLI Control

WebSocket bridge for headless / scripted control of Godot 4 scenes.
Click nodes, read/write properties, simulate input, take screenshots, record movies — all from a Python or shell client.

## Quick Start (5 minutes)

### 1. Get the plugin

Copy `addons/godot_cli_control/` to your project's `addons/` directory:

```bash
cp -r /path/to/godot-2d-skeleton/addons/godot_cli_control my_project/addons/
```

(Future: AssetLib install once published.)

### 2. Get the Python package

```bash
pip install -e /path/to/godot-2d-skeleton/python
# or, once published:
# pip install godot-cli-control
```

Requires Python ≥ 3.10.

### 3. Enable in Godot Editor

Open your project in Godot 4 → Project → Project Settings → Plugins → "Godot CLI Control" → Enabled.

The plugin auto-registers an autoload `GameBridgeNode`. No manual `project.godot` edits needed.

> **Note**: Headless `--editor --quit` may not persist plugin enable to `project.godot`. If you need to enable the plugin in CI / scripted environment, manually add `[editor_plugins] enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")` and `[autoload] GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"` to your `project.godot`.

### 4. Run

```bash
# Copy the wrapper stub to your project root (6 lines):
cat > run_cli_control.sh <<'EOF'
#!/usr/bin/env bash
exec "$(dirname "$0")/addons/godot_cli_control/bin/run_cli_control.sh" "$@"
EOF
chmod +x run_cli_control.sh

# Start the daemon
# (note: does NOT auto-import your project's assets; if your project has
# preprocessing (PSD splitting, asset gen, etc.), run those FIRST yourself)
./run_cli_control.sh start

# Try it
./run_cli_control.sh tree 3
./run_cli_control.sh click /root/MyScene/Button
./run_cli_control.sh screenshot /tmp/test.png
./run_cli_control.sh stop
```

## RPC Reference

All methods callable via `python -m godot_cli_control <method>` or `from godot_cli_control import GameClient`.

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
| **CLI flag** | Pass `--cli-control` to Godot binary | Wrapper script / pytest / CI |
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
2. **`GODOT_BIN` defaults to macOS path** `/Applications/Godot.app/Contents/MacOS/Godot`. Linux/Windows users must `export GODOT_BIN=/path/to/godot`.
3. **Python ≥ 3.10** required (websockets>=14 dependency).
4. **Windows**: requires WSL or a custom PowerShell wrapper (not provided).
5. **Single-client mode**: only one WebSocket client at a time; second connection rejected.
6. **Headless plugin auto-register**: in headless mode, `--editor --quit` may not trigger `plugin._enter_tree` to write the autoload. If integrating into a CI pipeline, manually populate `[autoload]` and `[editor_plugins]` sections in `project.godot`.

## Links

- Python package: see `python/` directory in this repo
- Source: this repo (https://github.com/kesar/godot-2d-skeleton; future independent repo TBD)
- Issues: file at https://github.com/kesar/godot-2d-skeleton/issues with `cli_control` label
