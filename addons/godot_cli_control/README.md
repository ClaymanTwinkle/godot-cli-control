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

The runner builds a throwaway Godot project, `git clone`s a pinned [GUT](https://github.com/bitwes/Gut) release into `addons/gut/`, copies this plugin in, and runs the test files under `addons/godot_cli_control/tests/gut/`. Coverage today is `LowLevelApi` handler boundaries (blacklist, missing-property, node-not-found), `InputSimulationApi` state machine (combo / press / release / tap / release_all), and `WaitApi` wait primitives (frames, property polling, signal capture, setup-guard).

GUT itself is **not** vendored into the repo or shipped with the wheel/AssetLib zip — it's a dev-only dependency.

## RPC Reference

All methods callable via `godot-cli-control <method>` or `from godot_cli_control import GameClient`.

| Method | Example |
|---|---|
| `click(path)` | `await client.click("/root/MyScene/Button")` |
| `click_at(x, y, node=None, button="left", double=False)` | `await client.click_at(320, 240)` or `client.click_at(node="/root/Slot", button="right")` — coordinate-level mouse click via the real event pipeline (viewport physical px); CLI: `click-at <x> <y> \| --node <path> [--button] [--double]` |
| `mouse_move(x, y, node=None)` | `await client.mouse_move(400, 300)` — inject one mouse-motion event (with `relative`); CLI: `mouse-move <x> <y> \| --node <path>` |
| `drag(x1, y1, x2, y2, from_node=None, to_node=None, button="left", duration=0.3, steps=10)` | `await client.drag(100, 100, 300, 200)` — press→interpolated move→release (slider/drag-and-drop/swipe); each end may use `from_node`/`to_node` for a node center; game-time `duration`, one drag at a time (`1014` otherwise); CLI: `drag <x1> <y1> <x2> <y2> \| --from-node/--to-node <path> [--button] [--duration] [--steps]` |
| `get_property(path, property)` | `await client.get_property("/root/Player", "position")` — returns bare value; CLI `get` returns `{"value": ..., "type"?: ...}` shape |
| `get_properties(path, properties)` | `await client.get_properties("/root/Player", ["position", "health"])` — multi-property atomic read; returns `{prop: bare_value, ...}` dict |
| `set_property(path, property, value)` | `await client.set_property("/root/Player", "visible", False)` |
| `call_method(path, method, args)` | `await client.call_method("/root/Player", "take_damage", [10])` |
| `emit_signal(path, signal, args)` | `await client.emit_signal("/root/Game", "level_complete", [])` — fire a node signal (test seam). **Disabled by default** (server returns `1015`); daemon must be started with `--allow-emit-signal` (debug-build + localhost + explicit opt-in). `call <node> emit_signal` is always blocked by the method blacklist — use this method. CLI: `emit-signal <path> <signal> [args...]` |
| `get_text(path)` | `await client.get_text("/root/UI/Label")` |
| `node_exists(path)` | `await client.node_exists("/root/MyScene/Button")` |
| `is_visible(path)` | `await client.is_visible("/root/UI/Panel")` |
| `get_children(path, type_filter?)` | `await client.get_children("/root/Map", "Node2D")` |
| `get_scene_tree(depth, max_nodes=None, path=None)` | `await client.get_scene_tree(depth=3, path="/root/GameUI")` — omit `path` → current scene; CLI: `tree [path] [depth]` |
| `find_nodes(node_type?, text?, text_contains?, name_pattern?, from_path?, limit=20)` | `await client.find_nodes(node_type="Button", text_contains="开始")` — server-side single-traversal node search (#153): the way to locate programmatic anonymous UI (`@Button@12`) by type (subclass + `class_name` aware) / `text` property (exact or substring, mutually exclusive) / node-name glob; filters AND together, at least one required (else `-32602`); BFS order (shallowest first); returns `{matches: [{name,type,path,text?,visible?}], truncated?: true}` over `limit` (server cap 500); CLI: `find [--from ...] [--type ...] [--exact ...\|--contains ...] [--name-pattern ...] [--limit N]`, exit 0 = ≥1 match, 1 = none |
| `screenshot(node=None)` | `png_bytes = await client.screenshot()`; pass `node="/root/Game/Sprite"` to crop to that node's screen-space AABB; CLI: `screenshot <path> [--node <node-path>]` (errors: `1010` bounds undeterminable, `1011` off-screen, `1013` daemon can't write the path). `screenshot_raw(node=None, path=None)`: pass an **absolute** `path` to have the daemon write the PNG to disk itself — response is `{path, bytes}` metadata only, no base64 crosses the socket (#149); parent dir must already exist |
| `sprite_info(path)` | `await client.sprite_info("/root/Game/Sprite")` — aggregate render-state query for `Sprite2D` / `AnimatedSprite2D` / `TextureRect` (texture, `effective_region`, `frame_texture`, flips, frame, modulate); headless-safe; CLI: `sprite-info <node-path>`; error `1010` for other node types |
| `errors(since=0, limit=100)` | `await client.errors(since=marker)` — structured `push_error`/`push_warning` capture (ring of last 1000, cursor pagination via `marker`); needs Godot 4.5+ `Logger` (else `1012`); CLI: `errors [--since MARKER] [--limit N]` |
| `wait_for_node(path, timeout)` | `await client.wait_for_node("/root/Boss", timeout=10.0)` |
| `wait_game_time(seconds)` | `await client.wait_game_time(2.5)` |
| `wait_property(path, prop, value, op, timeout, tolerance)` | `await client.wait_property("/root/Player", "position:x", 500, op="gt", timeout=3.0)` |
| `wait_signal(path, signal, timeout)` | `await client.wait_signal("/root/Game", "level_complete", timeout=10.0)` — miss result includes `reason: "timeout"\|"node_freed"` |
| `wait_frames(frames, physics)` | `await client.wait_frames(4)` |
| `action_press(action)` | `await client.action_press("jump")` |
| `action_release(action)` | `await client.action_release("jump")` |
| `action_tap(action, duration)` | `await client.action_tap("attack", 0.1)` |
| `hold(action, duration)` | `await client.hold("run", 1.5)` |
| `combo(steps)` | `await client.combo([{"action": "jump", "duration": 0.1}])` |
| `combo_cancel()` | `await client.combo_cancel()` |
| `release_all()` | `await client.release_all()` |
| `get_pressed()` | `await client.get_pressed()` |
| `list_input_actions(include_builtin=False)` | `await client.list_input_actions()` |
| `scene_reload(timeout)` | `await client.scene_reload(timeout=10.0)` — reload current scene, block until ready; CLI: `scene-reload [--timeout N]` |
| `scene_change(path, timeout)` | `await client.scene_change("res://levels/l2.tscn", timeout=10.0)` — switch scene, block until ready; CLI: `scene-change <res://path.tscn> [--timeout N]` |
| `time_scale(value=None)` | `await client.time_scale()` — read `Engine.time_scale`; `await client.time_scale(5.0)` — set to 5×; CLI: `time-scale [value]`; valid range `(0, 100]` |
| `pause()` | `await client.pause()` — freeze scene tree (`get_tree().paused = true`); idempotent; CLI: `pause` |
| `unpause()` | `await client.unpause()` — resume scene tree; idempotent; CLI: `unpause` |
| `step_frames(frames, physics=False)` | `await client.step_frames(10)` — while paused, advance exactly N frames (1..3600) then stop; CLI: `step-frames <n> [--physics]`; error `1009` if tree not paused |

> **CLI note**: as a shell subcommand, `screenshot` **requires an output path** — `godot-cli-control screenshot /tmp/shot.png`. The PNG is written by the **daemon process directly to disk** (#149), so image size is unlimited — no payload crosses the WebSocket, and hiDPI / 4K fullscreen captures work without shrinking the window. Returning base64 over stdout is intentionally unsupported, to keep large binary payloads out of an automating agent's context window.

> **`--wait`**: `tap` / `hold` / `combo` return as soon as the input is armed, *before* the motion finishes. Add `--wait` (e.g. `hold run 1.5 --wait`) to block until the action's duration elapses (game-time) so the next read sees the settled state — equivalent to following the command with `wait-time <duration>` on the same connection.

> **Event pipeline**: `press` / `tap` / `hold` / `combo` inject an `InputEventAction` through the engine's event pipeline — both polling (`is_action_pressed`, `get_vector`) **and** event callbacks (`_input`, `_unhandled_input`) see the input. `InputEventAction` carries no mouse coordinates; for position-dependent `_gui_input` widgets use `click_at` / `mouse_move` / `drag` (coordinate-level, viewport physical pixels, via `Viewport.push_input`) or `click` (node-level). `drag` presses, interpolates motion over game-time `duration` with the button held, then releases — one at a time (`1014` otherwise), and `release_all` cancels an in-flight drag with a pending mouse-up. Note these don't update the global `Input` mouse-polling state — read `position` / `relative` from the event, not by polling.

> **No-arg `GameClient()` / `GameBridge()`**: with no `port` argument they auto-discover the daemon's port from `.cli_control/instances/<name>/port` (falling back to `9877`). Pass `instance="server"` to target a named instance when multiple are running (explicit `port` always wins over `instance`). A no-arg call in a single-instance environment still works as before.

### Variant encoding depth limit

`get` encodes property values recursively. Containers nested deeper than **64 levels**, or self-referencing containers, are replaced with the sentinel string `"<max-depth-exceeded>"`. If you see it, narrow your read using a sub-path (`get /root/Node mydict:somekey`).

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
| `1007` | server | Signal not found on the node (`wait-signal` schema error — signal name typo or the node doesn't define it). Permanent — don't retry; inspect with `tree` or `children` to find valid signals. |
| `1008` | server | Scene unavailable (`scene-reload` / `scene-change`): no current scene, scene file missing / failed to load, or timed out waiting for the new scene to become ready. Fix the path (permanent) or inspect the daemon log (timeout). |
| `1009` | server | NOT_PAUSED: `step-frames` called while the scene tree is not paused. Call `pause` first. Precondition error — not a param error (distinct from `-32602`). |
| `1010` | server | UNSUPPORTED_NODE_TYPE: `sprite-info` on a non-sprite node, or `screenshot --node` on a node whose bounds can't be determined. Schema-class — pick another node/command, don't retry. |
| `1011` | server | NODE_NOT_ON_SCREEN: `screenshot --node` crop rect doesn't intersect the viewport (off-screen / zero size). State-class — bring the node into view, then retry. |
| `1012` | server | FEATURE_UNAVAILABLE: the hosting engine lacks an API this RPC needs (currently: `errors` requires Godot 4.5+ `Logger`). Permanent for that engine — upgrade Godot, don't retry. |
| `1013` | server | WRITE_FAILED: the daemon process couldn't write the `screenshot` PNG to the requested path (parent dir missing / no permission). Distinct from `-1004` (CLI-side IO). Permanent — fix the path, don't retry. |
| `1014` | server | DRAG_IN_PROGRESS: a `drag` was issued while another is still interpolating. One mouse drag in flight at a time — wait for it (or `release-all` to cancel) before the next. State-class (like `1004`). |
| `1015` | server | EMIT_SIGNAL_DISABLED: `emit-signal` called but daemon was not started with `--allow-emit-signal`. Restart the daemon with that flag (debug-build + localhost + explicit opt-in). `emit_signal` remains in the method blacklist — `call <node> emit_signal` is always rejected regardless of this flag. |
| `-32600` | server | Malformed JSON-RPC request |
| `-32601` | server | Unknown method name |
| `-32602` | server | Invalid params (incl. blocked methods/properties from the security blacklist, `set` value-type mismatch — e.g. `Vector2` property given an array of wrong length / non-numeric elements, or `hold` given `duration ≤ 0` — use `press` for an indefinite hold; also out-of-range values like a scene `timeout` outside `(0, 3600]` sent directly via `GameClient`, and `find_nodes` called with no filter at all or with both `text` and `text_contains`) |
| `-1001` | client | Connection failure (daemon not running, port wrong, proxy hijacking localhost) |
| `-1002` | client | Timeout waiting for response |
| `-1003` | client | CLI usage error (combo missing steps, malformed `--steps-json`, a non-numeric `tap`/`wait-time` arg, a `scene-change` path not starting with `res://`/`uid://`, a `scene-reload`/`scene-change` `--timeout` outside `(0, 3600]`, a `set`/`call` value that fails JSON parsing, script path not found, script missing `run(bridge)`, **multi-instance ambiguity** (≥2 instances running and no `--instance`/`--name` given), **named instance not running** (explicit `--instance nope` but that instance isn't up), **instance port not readable yet** (alive but mid-startup — transient, retry), or `--instance` / `--name` conflict). Always exits **64** (#82 / #111). |
| `-1004` | client | Local file IO error in the CLI process (e.g. screenshot can't create the destination's parent dir); daemon-side write failures are `1013` |
| `-1005` | client | `run <script>` user script raised an uncaught exception — fix the script |
| `-1006` | client | Infra pre-condition failure (`daemon start`/`stop`/`run` auto-start failed at OS level — port conflict, Godot binary not found, etc.). Always exits **2** (#92). |
| `-1099` | client | Internal CLI bug — please file an issue |

For full retry guidance see the SKILL.md shipped by `godot-cli-control init` (`.claude/skills/godot-cli-control/SKILL.md` in the target project).

## Daemon commands

The daemon is the long-lived Godot process the CLI talks to (one per project root). The CLI auto-starts it where needed; manage it explicitly with:

| Command | What it does |
|---|---|
| `daemon start [--headless\|--gui] [--record] [--time-scale N] [--name <inst>] [--allow-emit-signal]` | Launch Godot with the bridge listening. `--name` sets the instance name (default `default`); use different names to run multiple Godot daemons for the same project in parallel. Headless vs GUI is auto-detected from the TTY; `--record` forces GUI (Movie Maker needs a real renderer) and is **rejected with `--headless`** (exit 2). `--time-scale N` sets `Engine.time_scale` from frame 0 (range `(0, 100]`). `--allow-emit-signal` unlocks the `emit-signal` subcommand (debug-build + localhost + explicit opt-in three gates); without it any `emit-signal` call returns `1015`. |
| `daemon stop [--name <inst>]` | Stop this project's daemon. With `--name`, stop the named instance. Exit 2 if it stopped cleanly but the `.avi`→`.mp4` ffmpeg transcode failed (raw `.avi` kept; see `.cli_control/ffmpeg.log`). |
| `daemon stop --all` | Stop every registered daemon across all projects. **Exit 3** if at least one failed; per-record `rc` (and an `error` field on failures) is in `result.stopped[]`. |
| `daemon stop --all --project <path>` | Stop all instances of one specific project. |
| `daemon stop --project <path>` | Stop the daemon for one specific project root (single-instance shorthand). |
| `daemon status [--name <inst>]` | Exit 0 = running, 1 = stopped. Response includes `"instance"` field. When stopped, the payload may carry `last_log` / `last_exit_code` to diagnose a crash. |
| `daemon ls` | List every registered daemon (project root, pid, port, **instance**). Text columns: `pid\tport\tinstance\tproject_root\tstarted_at`. |
| `daemon logs [--name <inst>] [--tail N]` | Print last N lines of godot.log for the given instance. |

**Project-level defaults**: drop a `.cli_control/config.json` with `{"idle_timeout": "30m"}` to give `daemon start` / `run` a default idle-timeout without passing `--idle-timeout` each time. It's consulted only when the flag is omitted (default `0`); an explicit `--idle-timeout` always wins. A long-running `wait-time` / `combo` / recording is bounded client-side by a 600s wall-time fail-safe — override it for legitimately long operations with `GODOT_CLI_LONG_OP_TIMEOUT=<seconds>`.

## Exit codes

Semantic exit codes so `if godot-cli-control exists /root/Foo; then …` works in shell:

| Code | Meaning |
|---|---|
| 0 | Success (or boolean true for `exists` / `visible`, node found for `wait-node`, condition matched for `wait-prop` / `wait-signal`) |
| 1 | RPC error; also `exists`/`visible`=false, `wait-node`/`wait-prop`/`wait-signal` timeout, `daemon status`=stopped |
| 2 | Connection / IO error, infra pre-condition failure (`daemon start`/`stop` system error, carry `-1006`), or `daemon stop` ffmpeg-transcode failure |
| 3 | `daemon stop --all` partial failure (≥1 daemon failed to stop) |
| 64 | Usage error — argparse parse failure, a pre-flight reject (`combo` with no steps / malformed `--steps-json`), a non-numeric `tap`/`wait-time` arg, a `set`/`call` value that fails JSON parsing, `run <script>` with a non-existent path or missing `run(bridge)`. All carry client code `-1003` and exit 64 (#82 / #111). |

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
