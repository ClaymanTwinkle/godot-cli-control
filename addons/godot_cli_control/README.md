# Godot CLI Control

WebSocket bridge for headless / scripted control of Godot 4 scenes.
Click nodes, read/write properties, simulate input, take screenshots, record movies — all from a Python or shell client.

## Quick Start (one command)

If you have the Python CLI installed (`pipx install godot-cli-control`), the entire setup collapses to:

```bash
cd your_godot_project
godot-cli-control init        # copy plugin, patch project.godot, detect Godot, gitignore .cli_control/
godot-cli-control daemon start
godot-cli-control tree 3
godot-cli-control daemon stop
```

`init` automates everything described in the manual setup below — copying the addon, editing `project.godot`, and detecting your Godot binary. It also appends `.cli_control/` to the project's root `.gitignore` (the daemon's machine-local state dir: detected binary path, pid, port, log, recording intermediates), so that state never gets committed. Pass `--no-gitignore` to skip that step.

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
# pip install -e /path/to/godot-cli-control
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
# Linux / macOS (bash):
GODOT_BIN=/path/to/godot ./addons/godot_cli_control/tests/run_gut.sh

# Cross-platform (Linux / macOS / Windows) — this is what CI runs:
GODOT_BIN=/path/to/godot python addons/godot_cli_control/tests/run_gut.py
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
| `hold(action, duration)` | `await client.hold("run", 1.5)` |
| `combo(steps)` | `await client.combo([{"action": "jump", "duration": 0.1}])` |
| `combo_cancel()` | `await client.combo_cancel()` |
| `release_all()` | `await client.release_all()` |
| `get_pressed()` | `await client.get_pressed()` |
| `list_input_actions(include_builtin=False)` | `await client.list_input_actions()` |

> **CLI note**: as a shell subcommand, `screenshot` **requires an output path** — `godot-cli-control screenshot /tmp/shot.png` — and writes the PNG to that file. Returning base64 over stdout is intentionally unsupported, to keep large binary payloads out of an automating agent's context window.

### Error codes

Three numeric ranges share `error.code`; they never overlap, so a single field is unambiguous.

| Code | Source | Meaning |
|---|---|---|
| `1001` | server | Node not found at the given path |
| `1002` | server | Property not found / shape mismatch |
| `1003` | server | Method not found on the node, **or** unknown InputMap action passed to `press`/`release`/`tap`/`hold`/`combo` (`"Unknown action: <name>"`). Schema error — don't retry; run `actions` (or `actions --all`) to list valid actions |
| `1004` | server | Combo already in progress (call `combo-cancel` to retry) |
| `1005` | server | Scene tree too large (lower `depth` or pass `--max-nodes`) |
| `1006` | server | Resource transiently unavailable (e.g. screenshot during scene transition). Rare under normal use — GameBridge waits for viewport first-frame before listening, and `screenshot` retries internally. Safe to retry if you do hit it. |
| `-32600` | server | Malformed JSON-RPC request |
| `-32601` | server | Unknown method name |
| `-32602` | server | Invalid params (incl. blocked methods/properties from the security blacklist, `set` value-type mismatch — e.g. `Vector2` property given an array of wrong length / non-numeric elements, or `hold` given `duration ≤ 0` — use `press` for an indefinite hold) |
| `-1001` | client | Connection failure (daemon not running, port wrong, proxy hijacking localhost) |
| `-1002` | client | Timeout waiting for response |
| `-1003` | client | CLI usage error (combo missing steps, malformed `--steps-json`, …) |
| `-1004` | client | Local file IO error (e.g. screenshot can't write the destination) |
| `-1005` | client | `run <script>` user script raised an uncaught exception — fix the script |
| `-1099` | client | Internal CLI bug — please file an issue |

For full retry guidance see the SKILL.md shipped by `godot-cli-control init` (`.claude/skills/godot-cli-control/SKILL.md` in the target project).

## Daemon commands

The daemon is the long-lived Godot process the CLI talks to (one per project root). The CLI auto-starts it where needed; manage it explicitly with:

| Command | What it does |
|---|---|
| `daemon start [--headless\|--gui] [--record]` | Launch Godot with the bridge listening. Headless vs GUI is auto-detected from the TTY; `--record` forces GUI (Movie Maker needs a real renderer) and is **rejected with `--headless`** (exit 2). |
| `daemon stop` | Stop this project's daemon. Exit 2 if it stopped cleanly but the `.avi`→`.mp4` ffmpeg transcode failed (raw `.avi` kept; see `.cli_control/ffmpeg.log`). |
| `daemon stop --all` | Stop every registered daemon across all projects. **Exit 3** if at least one failed; per-record `rc` (and an `error` field on failures) is in `result.stopped[]`. |
| `daemon stop --project <path>` | Stop the daemon for one specific project root. |
| `daemon status` | Exit 0 = running, 1 = stopped. When stopped, the payload may carry `last_log` / `last_exit_code` to diagnose a crash. |
| `daemon ls` | List every registered daemon (project root, pid, port). |

## Exit codes

Semantic exit codes so `if godot-cli-control exists /root/Foo; then …` works in shell:

| Code | Meaning |
|---|---|
| 0 | Success (or boolean true for `exists` / `visible`, node found for `wait-node`) |
| 1 | RPC error; also `exists`/`visible`=false, `wait-node` timeout, `daemon status`=stopped |
| 2 | Connection / IO error, or `daemon stop` ffmpeg-transcode failure |
| 3 | `daemon stop --all` partial failure (≥1 daemon failed to stop) |
| 64 | Usage error — bad argparse args, or a pre-flight reject like `combo` with no steps / malformed `--steps-json` (envelope carries client code `-1003`) |

## Activation Modes

Activation paths (any one triggers the bridge):

| Path | Activate by | Use case |
|---|---|---|
| **CLI flag** | Pass `--cli-control` to Godot binary | Wrapper script / pytest / CI (this is what `daemon start` uses) |
| **Env var** | `export GODOT_CLI_CONTROL=1` before launching Godot | Editor F5 with launch args |
| **Project Setting** | `godot_cli_control/auto_enable_in_debug` (defaults to **ON**) | Editor / debug builds; **release builds always disabled regardless**. Set to `false` to opt out. |

If none triggered, the plugin prints `[Godot CLI Control] inactive — ...` to console and self-destructs the autoload.

## Security Model

- **TCP server binds 127.0.0.1 only** (never 0.0.0.0). Use SSH tunnel for remote.
- **Release builds always disabled** via `OS.is_debug_build()` gate — the auto-enable Project Setting only takes effect in debug builds, so exported release games never expose the WebSocket.
- **PID/port files have mode 0600** (only the running user can read).
- **Method/property blacklist**: `queue_free`, `set_script`, `add_child`, `texture`, `material`, `script`, `process_mode`, etc., are blocked from RPC mutation. Custom safe hooks: define methods on your business nodes and call via `call_method`.
- **No auth token** — localhost binding is the primary boundary. Multi-user dev machines should keep daemon stopped when not in use.

## Known Limitations

1. **Headless + Movie Maker is broken** (Godot upstream): `--write-movie` produces empty MP4 in headless mode (no framebuffer). Use GUI mode for recording.
2. **Godot binary detection** order: `GODOT_BIN` env var → macOS `/Applications/Godot*.app` → `PATH` (`godot4`/`godot`) → Windows `Program Files\Godot*`. `init` writes the detected path to `.cli_control/godot_bin`; the daemon reads that file before re-scanning. The pytest e2e suite uses this same detection, so a `godot` on `PATH` is enough to run it.
3. **Python ≥ 3.10** required (websockets>=14 dependency).
4. **Single-client mode**: only one WebSocket client at a time; second connection rejected.
5. **Headless plugin auto-register**: in headless mode, `--editor --quit` may not trigger `plugin._enter_tree` to write the autoload. `init` handles this automatically by patching `project.godot` directly.

## Links

- Python package: see `python/` directory in this repo
- Source: https://github.com/ClaymanTwinkle/godot-cli-control
- Issues: https://github.com/ClaymanTwinkle/godot-cli-control/issues
