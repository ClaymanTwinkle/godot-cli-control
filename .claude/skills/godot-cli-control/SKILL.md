---
name: godot-cli-control
description: Use when driving a Godot 4 game from a script or terminal — clicking buttons, simulating input actions, taking screenshots, dumping the scene tree, reading or writing node properties, calling node methods, listing InputMap actions, writing pytest end-to-end tests against a live Godot scene (via the bundled pytest plugin / `godot_daemon` + `bridge` fixtures), or recording video / screen capture / demo replays (Godot Movie Maker, `--write-movie`, auto-transcoded to mp4 via ffmpeg). Trigger when the user mentions godot-cli-control, the godot-cli-control CLI/daemon, the `bridge` / `godot_daemon` pytest fixtures, or asks to automate / scrape / black-box-test / record / capture / film / e2e-test a Godot scene.
---

# godot-cli-control

WebSocket bridge for headless / scripted control of Godot 4 scenes. A daemon process owns a running Godot instance; clients (the CLI or the Python `GameClient`) send JSON-RPC over `ws://127.0.0.1:<port>` to click nodes, read & write properties, call methods, simulate input, dump the scene tree, take screenshots, and record movies.

## AI Quickstart (read this first)

**The shell CLI is now canonical.** Everything you can do from Python you can do from `godot-cli-control <subcommand>`. Default to the shell — only drop to a `def run(bridge):` script when you genuinely need to keep one client connection across many steps inside a single test scenario.

**Output is JSON by default.** Every RPC subcommand prints a single-line envelope on stdout:

- success: `{"ok": true, "result": <data>}` — exit 0 (or per-command exit code, see *Exit codes* below)
- error:   `{"ok": false, "error": {"code": <int>, "message": "..."}}` — exit 1 (RPC error) / 2 (connection, timeout) / 64 (usage)

Pipe straight into `jq` or `json.loads`. Add `--text` (or `--no-json`) to switch back to the legacy human-readable strings if you really want them.

**Node paths must be absolute** — start with `/root/`. Relative paths return "node not found".

**Quickstart commands:**

```bash
# One-time per Godot project (already done if you're reading this file):
godot-cli-control init

# Per session:
godot-cli-control daemon start --headless         # boots Godot in the background
godot-cli-control daemon status                   # exit 0 = running, 1 = stopped
godot-cli-control tree 2 | jq .result             # confirm RPC works
# ... your work ...
godot-cli-control daemon stop
```

> As of this version, `daemon start` autodetects headless mode by checking `stdout.isatty()`. Pipes, CI, and agent shell-outs run headless by default; an interactive terminal still gets a window. The explicit flags below are only needed to override: `--headless` forces headless even in a TTY; `--gui` forces a window even when stdout is piped.
>
> **`run <script>` adds one more layer**: it grep's the script source for `screenshot`. If found, headless is force-flipped to GUI even on non-TTY shells — headless dummy renderer can't read viewport texture, so `bridge.screenshot(...)` would otherwise hard-fail with code `1006`. Pass `--no-gui-auto` to disable this detection; explicit `--headless` / `--gui` still win.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (or, for `exists` / `visible` / `wait-node` / `wait-prop` / `wait-signal`, the boolean was true / found / matched / emitted) |
| 1 | RPC error (server returned `{"error":...}`); also `exists`/`visible`=false, `wait-node`/`wait-prop`/`wait-signal`=timeout, `daemon status`=stopped |
| 2 | Connection / IO error (daemon not running) or infra pre-condition failure (daemon failed to start, `daemon stop` encountered a system error — these carry client code `-1006`). Also: **`daemon stop` returns 2** when the daemon stopped cleanly but `ffmpeg` transcode of the recorded `.avi`→`.mp4` failed — the raw `.avi` is kept and `.cli_control/ffmpeg.log` has the details. `run <script>` propagates this: a successful script + failed transcode still exits 2. |
| 3 | `daemon stop --all` partial failure: at least one daemon in the registry failed to stop. Per-record `rc` is in the JSON `result.stopped[]`. |
| 64 | Usage error — argparse parse failure (missing / invalid args, unknown subcommand), a pre-flight reject caught before connecting (`combo` with no steps / malformed `--steps-json` / `combo -` from a TTY, `hold` with a non-positive duration), a bad runtime argument (`tap` / `wait-time` given a non-number, a `set`/`call` value that fails JSON parsing), **or** `run <script>` given a non-existent path / a script with no `run(bridge)` function. All carry client code `-1003` and consistently exit 64 (#82 / #111). |

Shell-`if` works:

```bash
if godot-cli-control exists /root/Main/Boss; then
  godot-cli-control click /root/Main/Boss
fi
```

## Daemon management

```bash
godot-cli-control daemon start             # boot daemon for cwd project
godot-cli-control daemon start --time-scale 5  # start at 5× game speed (applies Engine.time_scale from frame 0)
godot-cli-control daemon status            # exit 0 = running, 1 = stopped
godot-cli-control daemon stop              # stop cwd-project daemon (rc 0; rc 2 = ffmpeg transcode failed)
godot-cli-control daemon stop --project /path/to/other/godot/project
godot-cli-control daemon stop --all        # stop every registered daemon; exit 3 if any failed
godot-cli-control daemon ls                # list all running daemons (cross-project, walks the registry)
godot-cli-control daemon logs --tail 50    # last N lines of godot.log (works after the daemon died, too)
```

- **`daemon status` payload when running**: `{"state": "running", "pid": N, "port": M}`.
- **`daemon status` payload when stopped**: `{"state": "stopped"}`. If the previous launch wrote `.cli_control/godot.log` or recorded an exit code, the envelope also includes `"last_log": "<path>"` and/or `"last_exit_code": <int>` — use these to diagnose why the daemon died without manually grepping under `.cli_control/`.
- **`daemon ls` payload**: `{"daemons": [{"project_root", "pid", "port", "started_at", "godot_bin", "log_path"}, ...]}`. Dead records (PID gone) are auto-pruned on each call, so this is the canonical list of *actually-alive* daemons across all projects on the machine.
- **`daemon stop --all` payload**: `{"stopped": [{"project_root","pid","port","rc"[, "error"]}, ...], "rc": 0|3}`. Each entry's `rc` is the per-project stop result; the top-level `rc` is the aggregate exit code.
- **`daemon logs [--tail N]` payload**: `{"path": "<godot.log>", "lines": [...], "returned": N}` (default 50, max 1000). Reads the file client-side — **no RPC**, so it works post-mortem after the daemon crashed or stopped (the companion to `daemon status`'s `last_log` hint: status tells you where the log is, `logs` hands you the tail directly). No log file yet → `-1006`, exit 2.
- **`daemon start --time-scale N`**: sets `Engine.time_scale = N` (range `(0, 100]`) from the very first frame of the Godot process. Useful to run an entire test suite at e.g. 5× speed. **Asymmetry**: `run <script>` mode does not support `--time-scale` as a startup flag — inside the script call `bridge.time_scale(5)` on the first line instead; or use `daemon start --time-scale 5` beforehand and connect the script to the already-running daemon.

## JSON envelope examples

```bash
$ godot-cli-control exists /root/Main
{"ok": true, "result": true}

$ godot-cli-control click /root/DoesNotExist
{"ok": false, "error": {"code": 1001, "message": "node not found: /root/DoesNotExist"}}

$ godot-cli-control --text exists /root/Main
true

$ godot-cli-control get /root/Player position
{"ok": true, "result": {"value": [-2480.0, 1400.0], "type": "Vector2"}}

$ godot-cli-control get /root/Player visible
{"ok": true, "result": {"value": true}}

$ godot-cli-control get /root/Player position:x
{"ok": true, "result": {"value": -2480.0}}

$ godot-cli-control get /root/Player position health
{"ok": true, "result": {"values": {"position": {"value": [-2480.0, 1400.0], "type": "Vector2"}, "health": {"value": 80}}}}

$ godot-cli-control init
{"ok": true, "result": {"project_root": "/path/to/proj", "plugin_copied": true, "plugin_overwritten": false, "project_godot_changes": ["autoload/GameBridgeNode", "editor_plugins/enabled"], "godot_bin": "/usr/bin/godot", "skills_written": [".../SKILL.md", ".../SKILL.md"], "gitignore_added": [".cli_control/"], "skills_only": false, "write_skills": true}}

$ godot-cli-control run my_script.py
{"ok": true, "result": {"exit_code": 0, "script": "my_script.py"}}

$ godot-cli-control run broken.py
{"ok": false, "error": {"code": -1005, "message": "运行失败：RuntimeError: assertion failed"}}
```

Both `init` and `run` honour the same envelope. In `run --json` mode the
user script's `print()` output is redirected to stderr so the envelope stays
on a single stdout line — anything your script writes is still visible in
the terminal, just not in the parseable payload.

## Error code reference

Three numeric ranges cohabit in `error.code`. Knowing which is which lets you decide retry vs fail-hard.

**Server-side (Godot plugin) — positive integers:**

| Code | Meaning |
|---|---|
| `1001` | Node not found at the given path. Most common — usually the agent passed a wrong / not-yet-loaded path. Retry after `wait-node`. |
| `1002` | Property not found on the node, or shape mismatch (e.g. `text` on a node that doesn't have it). Don't retry; inspect with `tree`. |
| `1003` | Method not found on the node, **or** unknown InputMap action passed to `press`/`release`/`tap`/`hold`/`combo` (`"Unknown action: <name>"`). Schema error — don't retry. For node methods inspect with `tree`; for missing actions run `actions` (or `actions --all`). |
| `1004` | Combo already in progress. Call `combo-cancel` (or `release-all`) and re-issue. Safe to retry after that. |
| `1005` | Scene tree too large to serialize (default safety limit). Pass `--max-nodes` or query a subtree with `children` / `tree <subpath>`. Don't retry as-is. |
| `1006` | Resource transiently unavailable (e.g. screenshot during scene transition / window resize). Rare under normal use: GameBridge waits for viewport first-frame before accepting connections, and `screenshot` retries internally up to ~30 frames (~500ms at 60 fps, ~1s at 30 fps, longer when `--write-movie` lowers the fixed fps). If you still see this, retry after `wait-time 0.05` or similar. |
| `1007` | Signal not found on the node (`wait-signal` schema error — signal name typo or the node doesn't define it). Permanent — don't retry; inspect with `tree` to list available signals. |
| `1008` | Scene unavailable (`scene-reload` / `scene-change`): no current scene, scene file missing / failed to load, or timed out waiting for the new scene to become ready. Missing file is permanent — fix the path; timeout usually means the scene itself fails to load — inspect the daemon log. |
| `1009` | NOT_PAUSED: `step-frames` was called while the scene tree is not paused. This is a state precondition error, not a parameter error — the frames value is valid, but the world state doesn't satisfy the prerequisite. Call `pause` first, then `step-frames`. Don't confuse with `-32602` (bad param value) or `-1003` (CLI usage error). |
| `1010` | UNSUPPORTED_NODE_TYPE: `sprite-info` on a node that isn't `Sprite2D` / `AnimatedSprite2D` / `TextureRect`, or `screenshot --node` on a node whose bounds can't be determined (not a CanvasItem, or no size/rect/texture to measure). Schema-class permanent error — pick a different node (often a child sprite of the one you tried) or a different command; retrying is pointless. |
| `1011` | NODE_NOT_ON_SCREEN: `screenshot --node` resolved the node and computed its rect, but the rect doesn't intersect the viewport (off-screen, or zero visible size). State-class error (like `1009`): the arguments are fine, the world isn't — move the camera / the node, or wait for it to enter view, then retry. |
| `1012` | FEATURE_UNAVAILABLE: the engine hosting the daemon lacks an API this RPC needs. Currently only `errors` (push_error capture requires Godot 4.5+'s `Logger`). Permanent for that engine — don't retry; upgrade Godot or drop the `errors` / `no_push_errors` usage. |

**JSON-RPC standard — negative integers `-32xxx`:**

| Code | Meaning |
|---|---|
| `-32600` | Malformed request (missing / non-string `method`). Bug in client; should never reach an agent. |
| `-32601` | Unknown method name. Bug; means client + plugin versions drifted. |
| `-32602` | Invalid params: missing required field, blocked property/method (security blacklist), out-of-range value, value-type mismatch on `set` (e.g. `Vector2` property given an array of wrong length / non-numeric elements), node-isn't-clickable (e.g. you `click`'d a `Node2D`), or `hold` given `duration ≤ 0` (use `press` for an indefinite hold). Don't retry; the request shape is wrong. |

**Client-side (CLI / GameClient) — `-1xxx`:**

| Code | Meaning |
|---|---|
| `-1001` | Connection failure (daemon not running, port wrong, proxy hijacking localhost). Run `daemon status`. |
| `-1002` | Timeout waiting for a response. Daemon may be hung mid-frame; check Godot stderr. |
| `-1003` | Usage error (`combo` got no steps, malformed `--steps-json`, `combo -` from a TTY, a non-numeric `tap`/`wait-time` arg, a `scene-change` path not starting with `res://`/`uid://`, a `scene-reload`/`scene-change` `--timeout` outside `(0, 3600]`, a `set`/`call` value that fails JSON parsing, script path not found, or script missing `run(bridge)`). Always exits **64** (#111). Fix the invocation. |
| `-1004` | Local file IO error (e.g. `screenshot` can't write the destination — bad path, no write permission). **Not** a daemon problem. |
| `-1005` | `run <script>` user script raised an uncaught exception. The error message has the exception type + last-line summary; full traceback is on stderr. Fix the script, not the CLI. |
| `-1006` | Infra pre-condition failure (`daemon start` / `daemon stop` / `run`'s auto-start failed at the OS level — port conflict, Godot binary not found, PID file missing, etc.). Always exits **2** (#92). Fix the environment, not the invocation. |
| `-1099` | Internal client error (unforeseen exception). Bug in this CLI; please file an issue. Stderr has the full traceback. |

Server vs client ranges never overlap, so a single `code` field is unambiguous.

## Command catalogue

**Read:**
- `get <path> <prop> [<prop2> ...]` — read one or more node properties in a single atomic frame. Single-property result: `{"value": <encoded>, "type": "<GodotType>"}` (type field present only for compound Variants — Vector2, Color, etc.; absent for primitives like `bool`/`int`/`float`/`String`). Multi-property result: `{"values": {"<prop>": {"value": ..., "type"?: ...}, ...}}`. Sub-path form: `get <path> position:x` reads a scalar leaf of a compound Variant (e.g. returns `{"value": 1.5}` with no type field). Security note: sub-path reading can reach write-blacklisted nested attributes (e.g. `script:source_code`) — read-only diagnostic capability, intentional under localhost-only + debug-build gate.
- `text <path>` — read Label / Button text
- `exists <path>` — boolean existence check (exit-code-as-result)
- `visible <path>` — boolean visibility check (exit-code-as-result)
- `children <path> [type-filter]` — direct children
- `tree [depth] [--max-nodes N]` — full scene tree (default `--max-nodes 200`; on overflow, response includes `truncated: true` and `total_nodes: N`)
- `pressed` — currently held simulated input actions
- `actions [--all]` — InputMap actions (default filters `ui_*` builtins)

**Write / call:**
- `set <path> <prop> <json-value>` — write a property
- `call <path> <method> [json-args...]` — call any method
- `click <path>` — UI click

**Input simulation:**
- `press <action>` / `release <action>` — sticky press
- `tap <action> [duration] [--wait]` — press → wait → release
- `hold <action> <duration> [--wait]` — auto-release after N seconds (`duration` must be `> 0`; for an indefinite hold use `press`)
- `combo --steps-json '[...]' [--wait]` (or `combo file.json` / `combo -` for stdin) — sequence
- `combo-cancel` — abort running combo
- `release-all` — release everything

`press` / `tap` / `hold` / `combo` inject an `InputEventAction` through the engine's event pipeline, so both polling APIs (`is_action_pressed`, `get_vector`) **and** event callbacks (`_input`, `_unhandled_input`) will see the injected input. Note: `InputEventAction` carries no mouse coordinates — position-dependent `_gui_input` widgets need `click` instead.

`tap` / `hold` / `combo` are **async by default** — they return as soon as the input is armed, *before* the in-game motion finishes (see *Common pitfalls*). Add **`--wait`** to block until the action's duration elapses (game-time) so the next `get` reads the settled state — it folds an implicit `wait-time <duration>` into the same command/connection.

**Wait:**
- `wait-node <path> [timeout]` — block until node appears (exit 0=found, 1=timeout)
- `wait-time <seconds>` — wait N in-game seconds (matters for `--write-movie`). Server bounds: `0 ≤ seconds ≤ 3600`; passing out-of-range gets `-32602 "seconds must be ..."`. Client short-circuits `seconds <= 0` without an RPC.
- `wait-prop <path> <prop> <json-value> [--op eq|ne|gt|lt|ge|le] [--timeout N] [--tolerance N]` — block until property satisfies condition (exit 0=matched, 1=timeout). Example: `wait-prop /root/Player position:x 500 --op gt`. Default `--op eq`, `--timeout 5.0`, `--tolerance 0.0`.
- `wait-signal <path> <signal> [--timeout N]` — block until signal fires (exit 0=emitted, 1=timeout). Result on success: `{"emitted": true, "args": [...]}`. Result on miss: `{"emitted": false, "reason": "timeout"|"node_freed"}` — `"timeout"` means the signal never fired within the deadline; `"node_freed"` means the target node was freed during the wait (use this to distinguish a race from a stale path).
- `wait-frames <N> [--physics]` — advance exactly N process frames (or physics frames with `--physics`). Result: `{"success": true, "frames": N}`.

**Scene:**
- `scene-reload [--timeout N]` — reload the current scene and block until the new instance is ready (per-test isolation primitive). All previously cached node paths become stale after it returns.
- `scene-change <res://path.tscn> [--timeout N]` — switch to another scene and block until ready. Path must start with `res://` or `uid://` (checked before connecting). `--timeout` must be > 0 and <= 3600 (default 10).

**Time:**
- `time-scale [value]` — read (no arg) or set `Engine.time_scale`. Valid range `(0, 100]`. `wait-time` counts game time, so a higher scale speeds up the whole suite without changing wait semantics.
- `pause` / `unpause` — freeze / resume the scene tree (`get_tree().paused`). Idempotent. Returns `{"paused": true/false}`. Note: `wait-time` keeps counting while paused (its timer uses `process_always`), so you can use `wait-time` + `get` to verify a frozen state.
- `step-frames <n> [--physics]` — while paused, advance exactly N frames (1..3600) then stop (deterministic stepping for physics assertions). Requires `pause` first — otherwise error `1009`, exit 1. Returns `{"stepped": N, "paused": true}`.

**Render:**
- `screenshot <path> [--node <node-path>]` — write PNG (path is **required** as of 0.2.0). With `--node`, the full screenshot is cropped to that node's screen-space AABB (canvas/camera transform included) and the envelope reports the actual crop: `{"path": ..., "bytes": N, "node": ..., "region": [x, y, w, h]}` (viewport pixels, already clipped to the viewport). Errors: `1001` unknown node, `1010` bounds undeterminable, `1011` off-screen. Like any screenshot it needs a real renderer — headless daemons return `1006`.
- `sprite-info <node-path>` — aggregate "what is this node actually rendering" query for `Sprite2D` / `AnimatedSprite2D` / `TextureRect` (error `1010` for anything else). Pure property read — **works headless**. Key fields:
  - common: `type`, `visible`, `visible_in_tree`, `modulate` `[r,g,b,a]`; textures are reported as `{"path": "res://..."|null, "size": [w,h]}` (+ `atlas`/`atlas_region` when it's an `AtlasTexture`); `path` is `null` for runtime-built textures.
  - `Sprite2D`: `texture`, `flip_h/v`, `frame`, `frame_coords`, `hframes/vframes`, `region_enabled`, `region_rect`, and **`effective_region` `[x,y,w,h]`** — the atlas rect actually being drawn (region wins when enabled, else computed from the frame grid). Assert "the sprite shows frame N" against this instead of reading internal bookkeeping fields.
  - `AnimatedSprite2D`: `sprite_frames`, `animation`, `frame`, `playing`, `speed_scale`, `flip_h/v`, and **`frame_texture`** — the texture of the *current* frame (distinguishes FRONT vs BACK frames that no plain property exposes).
  - `TextureRect`: `texture`, `flip_h/v`, `stretch_mode`, `expand_mode`, `size`.

**Diagnostics:**
- `errors [--since MARKER] [--limit N]` — structured query of `push_error` / `push_warning` captured at runtime (Godot 4.5+ `Logger`; older engines → `1012`). Returns `{"errors": [{seq, type, message, source, file, line, unix_time, ticks_msec}], "marker": int, "dropped": int, "truncated": bool}`:
  - `type`: `"error"` / `"warning"` / `"script"` / `"shader"`; `source` is the GDScript call site (`res://x.gd:12 @ func`) from the backtrace — far more useful than `file`/`line` (the C++ origin).
  - **Cursor pattern**: grab a baseline with `errors --limit 0` (returns just `marker`), run your action, then `errors --since <marker>` to see *only what that action produced*. This is the primitive behind "this test must produce zero push_errors" — the only e2e-level defense against silently-swallowed failures.
  - `dropped > 0` means an error storm overflowed the ring buffer (last 1000 kept); `truncated` means more matched than `--limit` — page with `--since <returned marker>`.

### `set` / `call` security blacklist

The plugin refuses certain methods and properties to prevent agents from breaking the running scene. If you call them you'll get `-32602 "Blocked method/property: <name>"`. Notable entries (full list lives in the project's `LowLevelApi.gd` blacklist):

- **Methods**: `queue_free`, `free`, `set_script`, `set_meta`, `set` (reflection), `call` / `callv` / `call_deferred` (reflection), `connect` / `disconnect`, `add_child` / `remove_child`, anything matching `_*` (private convention).
- **Properties**: `script`, `_*` (private). `set` will refuse these; `get` mostly works but `_*` is filtered in scene-tree dumps.

Use the regular API (e.g. `set hp` instead of `call set hp`) — the blacklist exists exactly to push agents toward the typed surface. To remove a node, set its `visible` to false rather than `queue_free`-ing it.

### `set` / `call` value parsing

Each value is parsed as JSON first, falling back to a string if that fails. So:

```bash
godot-cli-control set /root/Player position '[100, 200]'   # array → Vector2
godot-cli-control set /root/Score   text     '"42"'        # explicit string "42"
godot-cli-control set /root/Score   text     hello         # implicit string "hello"
godot-cli-control set /root/Player  hp       30            # number 30
godot-cli-control call /root/Game start_game 1 '"easy"'    # int 1, string "easy"
```

**Array → Variant coercion (≥ 0.2.5):** the server reads the property's declared type and converts a numeric JSON array to the matching Godot variant. Layout is **axis-vector order** for matrix-like types: each row in the table below is one axis (3 or 4 floats), and the array is just those axes concatenated — `v[0..N-1]` = the first axis, then the next axis, etc.

| Variant | Length | Layout |
|---|---|---|
| `Vector2 / 2i` | 2 | `[x, y]` |
| `Vector3 / 3i` | 3 | `[x, y, z]` |
| `Vector4 / 4i` | 4 | `[x, y, z, w]` |
| `Rect2 / 2i` | 4 | `[x, y, w, h]` |
| `Color` | 3 or 4 | `[r, g, b]` (a=1) or `[r, g, b, a]` |
| `Plane` | 4 | `[normal.x, normal.y, normal.z, d]` ¹ |
| `Quaternion` | 4 | `[x, y, z, w]` ¹ |
| `AABB` | 6 | `[pos.x, pos.y, pos.z, size.x, size.y, size.z]` |
| `Basis` | 9 | `[x_axis.xyz, y_axis.xyz, z_axis.xyz]` |
| `Transform2D` | 6 | `[x_axis.xy, y_axis.xy, origin.xy]` |
| `Transform3D` | 12 | `[basis 9 axis-vector, origin.xyz]` |
| `Projection` | 16 | `[x_axis.xyzw, y_axis.xyzw, z_axis.xyzw, w_axis.xyzw]` |

¹ `Plane.normal` and `Quaternion` are **not auto-normalized** — pass unit vectors if you need correct rotation/distance semantics. (`Quaternion(0,1,0,1)` is accepted as-is but rotates wrong.)

So `position '[100, 200]'` → `Vector2(100, 200)`, `transform '[1,0,0, 0,1,0, 0,0,1, 10,20,30]'` → `Transform3D(IDENTITY, (10,20,30))`. Wrong length or non-numeric elements fail loud with `-32602 "value type mismatch ..."` instead of silently setting `(0, 0)` like pre-0.2.5 versions did.

**Sub-path + Array also fails loud.** `set <node> transform:origin '[10, 20, 30]'` is rejected with `-32602 "sub-path + Array is not supported"`: Godot's `Object.set("transform:origin", Array)` silently drops the Array (origin stays at `(0,0,0)`) — same class of footgun as the strict Variant checks above, so the server pre-empts it. Sub-paths are scalar-only (`set <node> position:x 1.8`); to write a whole compound Variant use the top-level Array form above.

**Footgun**: bare `null` / `true` / `false` / numeric strings parse as JSON literals first, **not** as strings. If you actually mean the string `"null"`, wrap it explicitly:

```bash
godot-cli-control set /root/Label text null       # ⚠️ stores Variant null!
godot-cli-control set /root/Label text '"null"'   # ✓ stores the string "null"
godot-cli-control set /root/Flag  on   true       # ⚠️ stores boolean true
godot-cli-control set /root/Flag  on   '"true"'   # ✓ stores the string "true"
```

**Escape hatch (preferred for LLM prompts):** pass `--text-value` to disable JSON parsing entirely:

```bash
godot-cli-control set /root/Label text null --text-value     # ✓ stores the string "null"
godot-cli-control set /root/Flag  on   true --text-value     # ✓ stores the string "true"
godot-cli-control call /root/Game start 42 easy --text-value # ✓ calls with ("42", "easy") — all strings
```

Use this when generating commands from a template — it removes the three-quote escaping headache (`'"null"'`).

### Variant encoding depth limit

`get` encodes property values recursively. Containers (Arrays, Dictionaries) nested deeper than **64 levels**, or self-referencing containers, are replaced with the sentinel string `"<max-depth-exceeded>"` rather than causing an error or hang. The sentinel is a diagnostic signal — not game data. If you see it, narrow your read: use a sub-path (`get /root/Node mydict:somekey`) or restructure your property access to avoid deeply nested containers.

### Tree truncation

`tree` caps output at 200 nodes by default to keep the JSON small enough for an LLM context window. When the cap is hit, the response carries explicit signals so you can decide whether to drill in:

```json
{"ok": true, "result": {
  "tree": { "...": "partial subtree" },
  "truncated": true,
  "total_nodes": 6000
}}
```

Responses are subject to a hard ceiling of 5000 nodes — beyond that you get `1005 "scene tree too large"` and must `--max-nodes` down or query a subtree:

- `tree --max-nodes 50` — quick overview
- `children /root/Game/Spawner` — drill into one branch
- `tree 1` — depth-1 only

### `combo` JSON schema

`combo` reads steps from one of three sources (mutually exclusive):
- `--steps-json '[...]'` — inline (preferred for ad-hoc agent use)
- `combo combo.json` — a file path
- `combo -` — read from stdin

Each step is:

- `{"action": "<InputMap action>", "duration": <seconds, default 0.1>}` — press the action, hold for `duration` seconds, then auto-release.
- `{"wait": <seconds>}` — pause without touching input.

Minimal inline example:

```bash
godot-cli-control combo --steps-json \
  '[{"action":"jump","duration":0.2},{"wait":0.5},{"action":"attack"}]'
```

Or `{"steps": [...]}` wrapper form is also accepted. Steps run strictly serially; while a combo is in flight any `press`/`release`/new `combo` is rejected with error `1004 "combo in progress"`. Use `release-all` (or `combo-cancel`) to abort.

### Recording a video / demo / screen capture

The daemon can drive Godot's [Movie Maker](https://docs.godotengine.org/en/stable/tutorials/animation/creating_movies.html) (`--write-movie`) so input you script through this skill is captured to disk. Pipeline: Godot writes raw `.avi`, `daemon stop` shells out to `ffmpeg` to transcode that into `.mp4` next to the original.

```bash
godot-cli-control daemon start --record --movie-path out.avi --fps 60
godot-cli-control click /root/Main/StartButton
godot-cli-control tap jump 0.3
godot-cli-control daemon stop          # → produces out.mp4
```

Key constraints:

- `--record` **requires** `--movie-path` (daemon refuses to start otherwise).
- `--record` needs a **real renderer**, so it cannot run with `--headless`: Godot Movie Maker's `add_frame()` reads the viewport texture, which the headless dummy renderer leaves null → SIGSEGV on the first frame. The daemon therefore **rejects `--record --headless` with exit code 2** before launching Godot. You don't need to pass `--gui`: when `--record` is set the daemon auto-opens a window even in a non-TTY (subagent / pipe / CI) shell that would otherwise default to headless.
- The `.mp4` is produced **only when `daemon stop` runs**; `kill -9` leaves the raw `.avi` behind.
- `ffmpeg` must be on `PATH` for transcoding. If transcoding fails, the raw `.avi` is kept and `daemon stop` exits with code `2` (transcode log at `.cli_control/ffmpeg.log`).
- `--fps` controls the **fixed simulation framerate** Godot runs at while recording — set it to your target video framerate.
- Output path is relative to cwd; use absolute paths if your script changes cwd.
- Long `wait-time` / `combo` / recording ops are bounded client-side by a fixed **600s** wall-time fail-safe (not a per-call timeout — game-time vs wall-time can't be predicted). A genuinely long operation (e.g. a > 10-minute recording) that trips it can raise the ceiling via `GODOT_CLI_LONG_OP_TIMEOUT=<seconds>` (e.g. `1800`); a non-positive / non-numeric value is ignored and falls back to 600s.

**Project-level defaults** (`.cli_control/config.json`, optional): set `{"idle_timeout": "30m"}` so `daemon start` / `run` auto-quit after idle without re-typing `--idle-timeout` every time. It's read only when you don't pass `--idle-timeout` (default `0`); an explicit flag wins. Bad JSON / a malformed duration there surfaces as a `-1003` usage error (exit 64).

## CLI reference

<!-- BEGIN cli_help (auto-generated by godot-cli-control init) -->
```
$ godot-cli-control --help
usage: godot-cli-control [-h] [-V] [--port PORT] [--json] [--text] [--no-json]
                         <command> ...

Godot CLI Control —— 通过命令行远程驱动 Godot 项目

positional arguments:
  <command>
    daemon        管理 Godot daemon 进程
    run           启动 daemon → 跑脚本 → 停 daemon
    init          在 Godot 项目根一键接入插件
    click         对 Control/Button 节点触发点击。
    screenshot    截屏并写 PNG 文件。**路径必填**（旧版本可省、把 base64 喷到 stdout —— 已删，避免撑爆 AI
                  上下文）。
    sprite-info   渲染态聚合查询（issue #101）：Sprite2D / AnimatedSprite2D /
                  TextureRect 的 texture、实际图集区域（effective_region /
                  frame_texture）、翻转、帧号、modulate、visible 一次拿齐。纯属性读，headless
                  可用。非 sprite 类节点 → 1010。
    errors        结构化查询运行期捕获的 push_error / push_warning（issue #103）。返回
                  {errors, marker, dropped, truncated}；--since 传上次 marker
                  只看新增（「本用例期间应零 push_error」断言的原语），--limit 0 纯拿基线。需 Godot
                  4.5+（Logger API），老引擎报 1012。
    tree          dump 当前场景树为 JSON。
    press         按下输入动作（持续按住，需配 release 释放）。
    release       释放之前 press 按下的输入动作。
    tap           短按动作（press → 等待 → release）。默认异步立即返回；加 --wait 阻塞到时长结束。
    hold          按住动作指定时长（秒），到点自动释放。默认命令立即返回（动作在游戏里持续该时长）；要读动作完成后的状态请加
                  --wait（或命令后先 wait-time <时长>）。
    combo         依次执行一段输入动作。三种喂法：位置 ``<file.json>`` / 位置 ``-`` (stdin) /
                  ``--steps-json '[...]'``。
    release-all   释放所有当前持有的输入动作。
    get           读节点属性（1 个或多个；多个时服务端同帧原子读，issue #100）。复合类型（Vector2 等）返回与 set
                  同 schema 的数组 + type 字段，可直接回灌 set（issue #99）。支持 sub-
                  path：position:x。
    set           写节点属性。value 优先按 JSON 解析（数字/数组/对象），失败退回字符串。加 --text-value 强制把
                  value 当字符串，避开 null/true/false/数字 footgun。
    call          调节点方法。每个参数同 set：先 JSON 解析，失败退回字符串。返回值原样（同 ``get`` 渲染规则）。
    text          读 Label / Button 的 text（get_text 的便捷形式）。
    exists        节点是否存在。退出码：0=true, 1=false, 2=连接/超时错误。shell ``if godot-cli-
                  control exists /root/Foo; then …`` 可用。
    visible       节点是否可见。退出码同 exists：0=true, 1=false, 2=infra error。
    children      列出节点的直接子节点（一层）。
    wait-node     轮询直到节点出现（或 timeout）。退出码：0=found, 1=timeout, 2=infra error。
    wait-time     按 game time 等待 N 秒（在 --write-movie 模式下与录像帧对齐）。
    wait-prop     逐帧轮询直到属性满足条件（或 timeout）。退出码：0=命中, 1=超时, 2=infra error。超时返回
                  reason（timeout/node_not_found/property_not_found）+
                  最后读到的值，便于诊断 typo。
    wait-signal   等信号发射（或 timeout），命中带回编码后的信号参数。退出码：0=命中, 1=超时, 2=infra
                  error。注意：必须先挂等待再触发动作（见 SKILL.md pitfall）。
    wait-frames   等 N 个 process 帧（--physics 等物理帧）。确定性帧推进，替代短 sleep。
    scene-reload  重载当前场景并阻塞到新场景 ready（per-test 隔离原语）。失败（无 current scene / 超时）报
                  1008，exit 1。注意：返回后此前缓存的所有节点路径/引用全部失效。
    scene-change  切换到指定场景并阻塞到新场景 ready。路径不存在/加载失败/超时 报 1008，exit 1。
    time-scale    读 / 写 Engine.time_scale（无参 = 读）。wait-time 按 game time
                  计，倍速后语义不变、墙钟变快。合法域 (0, 100]。注意：--record 下仍生效，录出的是加速画面。
    pause         暂停 SceneTree（get_tree().paused = true）。幂等；返回 {"paused":
                  true}。
    unpause       恢复 SceneTree（paused = false）。幂等；返回 {"paused": false}。
    step-frames   paused 状态下确定性推进 N 帧再停（物理断言银弹：推 N 个物理帧后状态必然确定）。必须先 pause，否则报
                  1009，exit 1。
    pressed       列出当前模拟器持有的输入动作（press + held 去重合并）。
    combo-cancel  取消正在运行的 combo（不影响 press/hold）。
    actions       列出运行项目的 InputMap 动作。默认过滤 ui_* 内置；加 ``--all`` 看全。

options:
  -h, --help      show this help message and exit
  -V, --version   show program's version number and exit
  --port PORT     RPC 子命令连接的 GameBridge 端口（默认从 .cli_control/port 读取，否则
                  9877）。注意：仅作用于 RPC 子命令，daemon start / run 启动 daemon 时请用其各自的
                  --port。
  --json          输出 JSON 信封（默认）
  --text          输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json       --text 别名

命令分组：

  Daemon 管理:
    daemon start    启动 Godot daemon（可选录制 / headless）
    daemon stop     停止当前 daemon
    daemon status   显示 daemon 状态（pid / port），exit 0=运行中，1=未运行
    daemon logs     输出 godot.log 尾部（--tail N；daemon 停了也能 post-mortem）
    run <script>    自动启停 daemon 并跑用户脚本（脚本需定义 run(bridge)）

  接入:
    init            在 Godot 项目根一键复制插件、patch project.godot

  RPC 一发命令（需先 daemon 在跑）:
    读：     get / text / exists / visible / children / tree / pressed / actions / sprite-info
    写：     set / call / click
    输入：   press / release / tap / hold / combo / combo-cancel / release-all
    等待：   wait-node / wait-time
    截图：   screenshot（--node 按节点裁剪）
    诊断：   errors（push_error 结构化增量查询）

输出契约（默认 --json，AI 友好）:
  成功： {"ok": true, "result": <data>}        单行 stdout，exit 0
  失败： {"ok": false, "error": {"code":N,"message":"..."}}
                                              单行 stdout，exit 1（RPC）/ 2（连接、用法）
  --text / --no-json 可切回旧的人类可读模式。

任意子命令后追加 -h 查看详情，例如：
  godot-cli-control click -h
  godot-cli-control combo -h        # 含 step JSON schema 与示例
  godot-cli-control daemon start -h

$ godot-cli-control daemon --help
usage: godot-cli-control daemon [-h] <action> ...

管理 Godot daemon 进程的启停与状态查询。

positional arguments:
  <action>
    start     启动 daemon
    stop      停止 daemon
    status    查询 daemon 状态
    logs      输出 godot.log 尾部（daemon 停了也能查）
    ls        列出所有正在运行的 daemon（跨项目）

options:
  -h, --help  show this help message and exit

$ godot-cli-control daemon start --help
usage: godot-cli-control daemon start [-h] [--record]
                                      [--movie-path MOVIE_PATH]
                                      [--headless | --gui] [--fps FPS]
                                      [--port PORT]
                                      [--idle-timeout IDLE_TIMEOUT]
                                      [--time-scale TIME_SCALE] [--json]
                                      [--text] [--no-json]

启动 Godot daemon 并写入 .cli_control/{godot.pid,port}。

options:
  -h, --help            show this help message and exit
  --record              启动后录制 demo（写到 .cli_control/movie_path）。需真实渲染器，不能与
                        --headless 同用；没指定时会自动开窗（即使非 TTY）。
  --movie-path MOVIE_PATH
                        demo 输出路径，默认 .cli_control 下自动命名
  --headless            无窗口模式。默认值：stdout 非 TTY 时自动 headless（CI / pipe /
                        agent）。与 --record 互斥（录制需真实渲染器）。
  --gui                 强制开窗。覆盖 isatty 自动判（例如 stdout 是 pipe 仍想看到窗口）。
  --fps FPS             录制帧率，默认 30
  --port PORT           GameBridge 监听端口（默认 0 = OS 自动分配；写入 .cli_control/port）
  --idle-timeout IDLE_TIMEOUT
                        空闲超时（如 30m / 2h / 90s / 0=关闭，默认关）。开启后 Godot 端 Timer 自动
                        quit。不传时回退读 .cli_control/config.json 的
                        idle_timeout（issue #44），省得每次手敲。
  --time-scale TIME_SCALE
                        启动即设 Engine.time_scale（>0 且 <=100），整套 e2e 提速用
  --json                输出 JSON 信封（默认）
  --text                输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json             --text 别名

$ godot-cli-control daemon stop --help
usage: godot-cli-control daemon stop [-h] [--all | --project PROJECT] [--json]
                                     [--text] [--no-json]

停止 daemon。无 flag 时停 cwd 项目；--all 停所有注册的 daemon；--project <path> 停指定项目。

options:
  -h, --help         show this help message and exit
  --all              停止注册表中所有运行中的 daemon
  --project PROJECT  停止指定项目根的 daemon（绝对/相对路径均可）
  --json             输出 JSON 信封（默认）
  --text             输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json          --text 别名

$ godot-cli-control daemon status --help
usage: godot-cli-control daemon status [-h] [--json] [--text] [--no-json]

打印 daemon 状态到 stdout 并以 exit code 表示：0 = 运行中（输出 running pid=<pid>
port=<port>），1 = 未运行（输出 stopped）。默认输出 JSON 信封；加 --text 切回旧的字符串格式。

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

$ godot-cli-control daemon logs --help
usage: godot-cli-control daemon logs [-h] [--tail N] [--json] [--text]
                                     [--no-json]

直接输出 .cli_control/godot.log 的最后 N 行（JSON 信封包裹），免去先 daemon status 拿路径再
tail。纯客户端读文件：daemon 已退出时同样可用（post-mortem 排查启动失败/崩溃）。无日志文件 → -1006，exit 2。

options:
  -h, --help  show this help message and exit
  --tail N    返回最后 N 行（1..1000，默认 50）
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

$ godot-cli-control daemon ls --help
usage: godot-cli-control daemon ls [-h] [--json] [--text] [--no-json]

扫描全局注册表（POSIX ~/.local/state/godot-cli-control/daemons/；Windows
%LOCALAPPDATA%\godot-cli-control\daemons\），列出所有探活通过的 daemon。死记录会被自动清理。

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

$ godot-cli-control run --help
usage: godot-cli-control run [-h] [--record] [--movie-path MOVIE_PATH]
                             [--headless | --gui] [--fps FPS] [--port PORT]
                             [--idle-timeout IDLE_TIMEOUT] [--no-gui-auto]
                             [--json] [--text] [--no-json]
                             script

若 daemon 未运行则先启动，加载用户脚本调用其 run(bridge) 函数，脚本结束后停掉刚启动的 daemon（已在跑的 daemon 保持原状）。脚本里抛任何异常都会以 exit code 1 退出并打印 traceback。

positional arguments:
  script                用户脚本路径，需定义 run(bridge)

options:
  -h, --help            show this help message and exit
  --record              启动后录制 demo（写到 .cli_control/movie_path）。需真实渲染器，不能与
                        --headless 同用；没指定时会自动开窗（即使非 TTY）。
  --movie-path MOVIE_PATH
                        demo 输出路径，默认 .cli_control 下自动命名
  --headless            无窗口模式。默认值：stdout 非 TTY 时自动 headless（CI / pipe /
                        agent）。与 --record 互斥（录制需真实渲染器）。
  --gui                 强制开窗。覆盖 isatty 自动判（例如 stdout 是 pipe 仍想看到窗口）。
  --fps FPS             录制帧率，默认 30
  --port PORT           GameBridge 监听端口（默认 0 = OS 自动分配；写入 .cli_control/port）
  --idle-timeout IDLE_TIMEOUT
                        空闲超时（如 30m / 2h / 90s / 0=关闭，默认关）。开启后 Godot 端 Timer 自动
                        quit。不传时回退读 .cli_control/config.json 的
                        idle_timeout（issue #44），省得每次手敲。
  --no-gui-auto         禁用脚本静态检测自动 GUI。默认含 screenshot 调用的脚本在非 TTY （subagent /
                        pipe / CI）下也强制开窗 —— headless dummy renderer 拿不到
                        viewport texture，截图会 1006 fail。
  --json                输出 JSON 信封（默认）
  --text                输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json             --text 别名

脚本示例 (my_script.py):
  def run(bridge):
      bridge.wait_for_node("/root/Main/StartButton", timeout=5)
      bridge.click("/root/Main/StartButton")
      bridge.wait(0.5)
      assert bridge.get_text("/root/Main/Score") == "0"

bridge 是 GameClient 的同步包装，方法名一致、无需 await。
脚本同目录的兄弟模块（from helpers import foo）可正常 import。

$ godot-cli-control init --help
usage: godot-cli-control init [-h] [--path PATH] [--force | --keep-addon]
                              [--skills-no-clobber] [--no-gitignore]
                              [--no-skills | --skills-only] [--json] [--text]
                              [--no-json]

复制 addons/godot_cli_control 到目标项目、patch project.godot 启用插件、校验 GODOT_BIN、在 .gitignore 忽略 .cli_control/。

options:
  -h, --help           show this help message and exit
  --path PATH          目标 Godot 项目根（默认当前目录）
  --force              覆盖已存在的 addons/godot_cli_control（现已是默认行为，本 flag 仅为兼容保留）
  --keep-addon         已存在 addons/godot_cli_control 时跳过插件复制（保留本地版本，不随 CLI
                       升级刷新；默认会覆盖以同步版本）
  --skills-no-clobber  写 skill 时跳过已存在的 .claude/.codex SKILL.md（默认会覆盖以保证版本与 CLI
                       帮助同步）。与 --no-skills / --skills-only 都兼容。
  --no-gitignore       跳过往项目根 .gitignore 追加 .cli_control/（默认会追加，忽略 daemon
                       的机器本地状态目录）。--skills-only 模式下本就跳过。
  --no-skills          跳过 .claude/.codex skill 写入
  --skills-only        只写 skill 文件，跳过插件复制 / project.godot patch / godot_bin 检测
  --json               输出 JSON 信封（默认）
  --text               输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json            --text 别名

GODOT_BIN 查找顺序：
  1. 环境变量 GODOT_BIN
  2. 项目根 .cli_control/godot_bin 文件（init 检测到时会写入）
  3. macOS /Applications/Godot*.app/Contents/MacOS/Godot
  4. PATH 上的 godot4 / godot / Godot
  5. Windows Program Files\Godot*\Godot*.exe
都没找到时 init 会打 warning，daemon start 会直接报错。
可以手动 `export GODOT_BIN=/path/to/godot` 或写到 .cli_control/godot_bin。

$ godot-cli-control click --help
usage: godot-cli-control click [-h] [--json] [--text] [--no-json] node_path

对 Control/Button 节点触发点击。

positional arguments:
  node_path   绝对节点路径（必须以 /root/ 开头），如 /root/Main/StartButton

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control click /root/Main/StartButton

$ godot-cli-control screenshot --help
usage: godot-cli-control screenshot [-h] [--node NODE_PATH] [--json] [--text]
                                    [--no-json]
                                    output_path

截屏并写 PNG 文件。**路径必填**（旧版本可省、把 base64 喷到 stdout —— 已删，避免撑爆 AI 上下文）。

positional arguments:
  output_path       PNG 输出路径（必填）

options:
  -h, --help        show this help message and exit
  --node NODE_PATH  按该节点的屏幕 AABB 裁剪截图（issue #101），产出小图供像素级断言。节点在屏幕外/零尺寸 →
                    1011；非 CanvasItem/算不出边界 → 1010。
  --json            输出 JSON 信封（默认）
  --text            输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json         --text 别名

示例:
  godot-cli-control screenshot out.png --node /root/Game/Player/Sprite

$ godot-cli-control sprite-info --help
usage: godot-cli-control sprite-info [-h] [--json] [--text] [--no-json]
                                     node_path

渲染态聚合查询（issue #101）：Sprite2D / AnimatedSprite2D / TextureRect 的 texture、实际图集区域（effective_region / frame_texture）、翻转、帧号、modulate、visible 一次拿齐。纯属性读，headless 可用。非 sprite 类节点 → 1010。

positional arguments:
  node_path   绝对节点路径，如 /root/Game/Player/Sprite

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control sprite-info /root/Game/Player/Sprite

$ godot-cli-control errors --help
usage: godot-cli-control errors [-h] [--since MARKER] [--limit N] [--json]
                                [--text] [--no-json]

结构化查询运行期捕获的 push_error / push_warning（issue #103）。返回 {errors, marker, dropped, truncated}；--since 传上次 marker 只看新增（「本用例期间应零 push_error」断言的原语），--limit 0 纯拿基线。需 Godot 4.5+（Logger API），老引擎报 1012。

options:
  -h, --help      show this help message and exit
  --since MARKER  只看 seq > MARKER 的新增（传上一次响应的 marker，实现「本用例期间」语义）
  --limit N       最多返回 N 条（0..1000；0 = 纯基线查询，只拿 marker 不取数据）
  --json          输出 JSON 信封（默认）
  --text          输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json       --text 别名

示例:
  godot-cli-control errors --since 42

$ godot-cli-control tree --help
usage: godot-cli-control tree [-h] [--max-nodes MAX_NODES] [--json] [--text]
                              [--no-json]
                              [depth]

dump 当前场景树为 JSON。

positional arguments:
  depth                 遍历深度，默认 3

options:
  -h, --help            show this help message and exit
  --max-nodes MAX_NODES
                        节点数软上限（默认 200）。超出时服务端截断子节点并返回 {truncated: true,
                        total_nodes: N}，agent 据此决定是否拆分子树。
  --json                输出 JSON 信封（默认）
  --text                输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json             --text 别名

示例:
  godot-cli-control tree 3

$ godot-cli-control press --help
usage: godot-cli-control press [-h] [--json] [--text] [--no-json] action

按下输入动作（持续按住，需配 release 释放）。

positional arguments:
  action      InputMap 动作名，如 jump

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control press jump

$ godot-cli-control release --help
usage: godot-cli-control release [-h] [--json] [--text] [--no-json] action

释放之前 press 按下的输入动作。

positional arguments:
  action      InputMap 动作名

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control release jump

$ godot-cli-control tap --help
usage: godot-cli-control tap [-h] [--wait] [--json] [--text] [--no-json]
                             action [duration]

短按动作（press → 等待 → release）。默认异步立即返回；加 --wait 阻塞到时长结束。

positional arguments:
  action      InputMap 动作名
  duration    按下时长（秒），默认 0.1

options:
  -h, --help  show this help message and exit
  --wait      阻塞到动作时长（game-time）结束再返回，再读状态即结算后值；默认异步立即返回。等价于命令后再跑一次 wait-time
              <时长>，但复用同一连接。
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control tap jump 0.2

$ godot-cli-control hold --help
usage: godot-cli-control hold [-h] [--wait] [--json] [--text] [--no-json]
                              action duration

按住动作指定时长（秒），到点自动释放。默认命令立即返回（动作在游戏里持续该时长）；要读动作完成后的状态请加 --wait（或命令后先 wait-time <时长>）。

positional arguments:
  action      InputMap 动作名
  duration    按住时长（秒，必须 > 0）

options:
  -h, --help  show this help message and exit
  --wait      阻塞到动作时长（game-time）结束再返回，再读状态即结算后值；默认异步立即返回。等价于命令后再跑一次 wait-time
              <时长>，但复用同一连接。
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control hold jump 1.5

$ godot-cli-control combo --help
usage: godot-cli-control combo [-h] [--steps-json STEPS_JSON] [--wait]
                               [--json] [--text] [--no-json]
                               [json_file]

依次执行一段输入动作。三种喂法：位置 ``<file.json>`` / 位置 ``-`` (stdin) / ``--steps-json '[...]'``。

positional arguments:
  json_file             JSON 文件路径，或 ``-`` 从 stdin 读；可为 [...steps] 或 {"steps":
                        [...]}

options:
  -h, --help            show this help message and exit
  --steps-json STEPS_JSON
                        直接传 JSON 字符串，不需要文件（与位置参数互斥）
  --wait                阻塞到动作时长（game-time）结束再返回，再读状态即结算后值；默认异步立即返回。等价于命令后再跑一次
                        wait-time <时长>，但复用同一连接。
  --json                输出 JSON 信封（默认）
  --text                输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json             --text 别名

示例:
  godot-cli-control combo --steps-json '[{"action":"jump","duration":0.2}]'

step schema（每个 step 二选一，按数组顺序串行执行）:
  {"action": "<InputMap 动作名>", "duration": <秒，默认 0.1>}
      —— 按下 action，等 duration 秒后自动释放
  {"wait": <秒>}
      —— 不动作，纯等待

最小可跑示例:
  godot-cli-control combo --steps-json \
    '[{"action":"jump","duration":0.2},{"wait":0.3},{"action":"attack"}]'

或从文件读：
  godot-cli-control combo combo.json

或从 stdin 读：
  cat combo.json | godot-cli-control combo -

中途可用 release-all 终止。combo 运行期间任何 press / release /
再开 combo 都会被服务端 1004 拒绝（不支持重叠按键）。

$ godot-cli-control release-all --help
usage: godot-cli-control release-all [-h] [--json] [--text] [--no-json]

释放所有当前持有的输入动作。

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control release-all

$ godot-cli-control get --help
usage: godot-cli-control get [-h] [--json] [--text] [--no-json]
                             node_path props [props ...]

读节点属性（1 个或多个；多个时服务端同帧原子读，issue #100）。复合类型（Vector2 等）返回与 set 同 schema 的数组 + type 字段，可直接回灌 set（issue #99）。支持 sub-path：position:x。

positional arguments:
  node_path   绝对节点路径，如 /root/Main
  props       属性名，1 个或多个；支持 sub-path 如 position:x

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control get /root/Player position visible

$ godot-cli-control set --help
usage: godot-cli-control set [-h] [--text-value] [--json] [--text] [--no-json]
                             node_path prop value

写节点属性。value 优先按 JSON 解析（数字/数组/对象），失败退回字符串。加 --text-value 强制把 value 当字符串，避开 null/true/false/数字 footgun。

positional arguments:
  node_path     绝对节点路径
  prop          属性名
  value         JSON 字面量或字符串。例：'42' / '"hello"' / '[10, 20]' / 'hello'

options:
  -h, --help    show this help message and exit
  --text-value  把 value 当字面字符串，不走 JSON 解析（避开 'null'/'true'/数字 footgun）
  --json        输出 JSON 信封（默认）
  --text        输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json     --text 别名

示例:
  godot-cli-control set /root/Main/Score text "42"

$ godot-cli-control call --help
usage: godot-cli-control call [-h] [--text-value] [--json] [--text]
                              [--no-json]
                              node_path method [args ...]

调节点方法。每个参数同 set：先 JSON 解析，失败退回字符串。返回值原样（同 ``get`` 渲染规则）。

positional arguments:
  node_path     绝对节点路径，如 /root/Main
  method        节点上的方法名
  args          方法参数；每个先按 JSON 解析失败 fallback 字符串

options:
  -h, --help    show this help message and exit
  --text-value  把所有 args 当字面字符串，不走 JSON 解析
  --json        输出 JSON 信封（默认）
  --text        输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json     --text 别名

示例:
  godot-cli-control call /root/Main start_game 1 "easy"

$ godot-cli-control text --help
usage: godot-cli-control text [-h] [--json] [--text] [--no-json] node_path

读 Label / Button 的 text（get_text 的便捷形式）。

positional arguments:
  node_path   绝对节点路径

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control text /root/Main/Title

$ godot-cli-control exists --help
usage: godot-cli-control exists [-h] [--json] [--text] [--no-json] node_path

节点是否存在。退出码：0=true, 1=false, 2=连接/超时错误。shell ``if godot-cli-control exists /root/Foo; then …`` 可用。

positional arguments:
  node_path   绝对节点路径

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control exists /root/Main/Boss

$ godot-cli-control visible --help
usage: godot-cli-control visible [-h] [--json] [--text] [--no-json] node_path

节点是否可见。退出码同 exists：0=true, 1=false, 2=infra error。

positional arguments:
  node_path   绝对节点路径

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control visible /root/Main/Hud

$ godot-cli-control children --help
usage: godot-cli-control children [-h] [--json] [--text] [--no-json]
                                  node_path [type_filter]

列出节点的直接子节点（一层）。

positional arguments:
  node_path    绝对节点路径
  type_filter  可选类型过滤，如 Button / Label

options:
  -h, --help   show this help message and exit
  --json       输出 JSON 信封（默认）
  --text       输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json    --text 别名

示例:
  godot-cli-control children /root/Main

$ godot-cli-control wait-node --help
usage: godot-cli-control wait-node [-h] [--json] [--text] [--no-json]
                                   node_path [timeout]

轮询直到节点出现（或 timeout）。退出码：0=found, 1=timeout, 2=infra error。

positional arguments:
  node_path   绝对节点路径
  timeout     超时秒，默认 5

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control wait-node /root/Main/StartButton 5

$ godot-cli-control wait-time --help
usage: godot-cli-control wait-time [-h] [--json] [--text] [--no-json] seconds

按 game time 等待 N 秒（在 --write-movie 模式下与录像帧对齐）。

positional arguments:
  seconds     等待秒数（服务端范围 0 ≤ seconds ≤ 3600；client 在 ≤0 时短路返回成功）

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control wait-time 0.5

$ godot-cli-control wait-prop --help
usage: godot-cli-control wait-prop [-h] [--op {eq,ne,gt,lt,ge,le}]
                                   [--timeout TIMEOUT] [--tolerance TOLERANCE]
                                   [--json] [--text] [--no-json]
                                   node_path prop value

逐帧轮询直到属性满足条件（或 timeout）。退出码：0=命中, 1=超时, 2=infra error。超时返回 reason（timeout/node_not_found/property_not_found）+ 最后读到的值，便于诊断 typo。

positional arguments:
  node_path             绝对节点路径
  prop                  属性名（支持 sub-path 如 position:x）
  value                 期望值（JSON-or-string，同 set 的 value 规则）

options:
  -h, --help            show this help message and exit
  --op {eq,ne,gt,lt,ge,le}
                        比较运算符，默认 eq；gt/lt/ge/le 仅数值
  --timeout TIMEOUT     超时秒（0..3600，默认 5）
  --tolerance TOLERANCE
                        float eq/ne 容差（默认 0=精确比较）
  --json                输出 JSON 信封（默认）
  --text                输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json             --text 别名

示例:
  godot-cli-control wait-prop /root/Player position:x 500 --op gt --timeout 3

$ godot-cli-control wait-signal --help
usage: godot-cli-control wait-signal [-h] [--json] [--text] [--no-json]
                                     node_path signal_name [timeout]

等信号发射（或 timeout），命中带回编码后的信号参数。退出码：0=命中, 1=超时, 2=infra error。注意：必须先挂等待再触发动作（见 SKILL.md pitfall）。

positional arguments:
  node_path    绝对节点路径
  signal_name  信号名，如 body_entered
  timeout      超时秒（0..3600，默认 5）

options:
  -h, --help   show this help message and exit
  --json       输出 JSON 信封（默认）
  --text       输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json    --text 别名

示例:
  godot-cli-control wait-signal /root/Area door_opened 3

$ godot-cli-control wait-frames --help
usage: godot-cli-control wait-frames [-h] [--physics] [--json] [--text]
                                     [--no-json]
                                     frames

等 N 个 process 帧（--physics 等物理帧）。确定性帧推进，替代短 sleep。

positional arguments:
  frames      等待帧数（1..3600）

options:
  -h, --help  show this help message and exit
  --physics   等 physics_frame（默认 process_frame）
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control wait-frames 3 --physics

$ godot-cli-control scene-reload --help
usage: godot-cli-control scene-reload [-h] [--timeout TIMEOUT] [--json]
                                      [--text] [--no-json]

重载当前场景并阻塞到新场景 ready（per-test 隔离原语）。失败（无 current scene / 超时）报 1008，exit 1。注意：返回后此前缓存的所有节点路径/引用全部失效。

options:
  -h, --help         show this help message and exit
  --timeout TIMEOUT  等新场景 ready 的超时秒（>0 且 <=3600，默认 10）
  --json             输出 JSON 信封（默认）
  --text             输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json          --text 别名

示例:
  godot-cli-control scene-reload

$ godot-cli-control scene-change --help
usage: godot-cli-control scene-change [-h] [--timeout TIMEOUT] [--json]
                                      [--text] [--no-json]
                                      scene_path

切换到指定场景并阻塞到新场景 ready。路径不存在/加载失败/超时 报 1008，exit 1。

positional arguments:
  scene_path         目标场景资源路径（res:// 或 uid://）

options:
  -h, --help         show this help message and exit
  --timeout TIMEOUT  等新场景 ready 的超时秒（>0 且 <=3600，默认 10）
  --json             输出 JSON 信封（默认）
  --text             输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json          --text 别名

示例:
  godot-cli-control scene-change res://levels/level2.tscn

$ godot-cli-control time-scale --help
usage: godot-cli-control time-scale [-h] [--json] [--text] [--no-json] [value]

读 / 写 Engine.time_scale（无参 = 读）。wait-time 按 game time 计，倍速后语义不变、墙钟变快。合法域 (0, 100]。注意：--record 下仍生效，录出的是加速画面。

positional arguments:
  value       新倍速（>0 且 <=100）；省略则读当前值

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control time-scale 5

$ godot-cli-control pause --help
usage: godot-cli-control pause [-h] [--json] [--text] [--no-json]

暂停 SceneTree（get_tree().paused = true）。幂等；返回 {"paused": true}。

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control pause

$ godot-cli-control unpause --help
usage: godot-cli-control unpause [-h] [--json] [--text] [--no-json]

恢复 SceneTree（paused = false）。幂等；返回 {"paused": false}。

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control unpause

$ godot-cli-control step-frames --help
usage: godot-cli-control step-frames [-h] [--physics] [--json] [--text]
                                     [--no-json]
                                     frames

paused 状态下确定性推进 N 帧再停（物理断言银弹：推 N 个物理帧后状态必然确定）。必须先 pause，否则报 1009，exit 1。

positional arguments:
  frames      推进帧数（1..3600）

options:
  -h, --help  show this help message and exit
  --physics   推进 physics_frame（默认 process_frame）
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control step-frames 3 --physics

$ godot-cli-control pressed --help
usage: godot-cli-control pressed [-h] [--json] [--text] [--no-json]

列出当前模拟器持有的输入动作（press + held 去重合并）。

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control pressed

$ godot-cli-control combo-cancel --help
usage: godot-cli-control combo-cancel [-h] [--json] [--text] [--no-json]

取消正在运行的 combo（不影响 press/hold）。

options:
  -h, --help  show this help message and exit
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control combo-cancel

$ godot-cli-control actions --help
usage: godot-cli-control actions [-h] [--all] [--json] [--text] [--no-json]

列出运行项目的 InputMap 动作。默认过滤 ui_* 内置；加 ``--all`` 看全。

options:
  -h, --help  show this help message and exit
  --all       包含 ui_* 内置动作（默认仅项目自定义动作）
  --json      输出 JSON 信封（默认）
  --text      输出旧的人类可读字符串（不再加信封；errors 走 stderr）
  --no-json   --text 别名

示例:
  godot-cli-control actions
```
<!-- END cli_help -->

## Python `GameClient` API

`from godot_cli_control.client import GameClient` — async WebSocket client; use as `async with GameClient() as client:`. **With no `port` argument it auto-discovers from `.cli_control/port`** (the same file the daemon writes and the CLI reads), falling back to `9877` if absent — so a no-arg `GameClient()` connects to a running daemon out of the box. Pass `GameClient(port=N)` only to override. (`GameBridge()` in `run` scripts auto-discovers identically.) **Every method below has a 1-line CLI equivalent above; only reach for Python when you need to keep a client open across many steps without the connection-per-call overhead.**

Errors raise `RpcError(code, message)` (a `RuntimeError` subclass) that preserves the server's error code — useful for retrying `1004 "combo in progress"`.

| Method | CLI equivalent |
|---|---|
| `await client.click(path)` | `click <path>` |
| `await client.get_property(path, prop)` | `get <path> <prop>` — returns bare value only (no type field); use `client.request("get_property", ...)` to get `{"value", "type"}` shape |
| `await client.get_properties(path, props)` | `get <path> <prop1> <prop2> ...` — returns `{prop: bare_value, ...}` dict (no type fields); use `client.request("get_properties", ...)` for full shape |
| `await client.set_property(path, prop, value)` | `set <path> <prop> <json-value>` |
| `await client.call_method(path, method, args)` | `call <path> <method> [json-args...]` |
| `await client.get_text(path)` | `text <path>` |
| `await client.node_exists(path)` | `exists <path>` |
| `await client.is_visible(path)` | `visible <path>` |
| `await client.get_children(path)` | `children <path>` |
| `await client.screenshot(node=None)` | `screenshot <path> [--node <node-path>]` — returns PNG bytes; pass `node` to crop to that node's screen rect |
| `await client.screenshot_raw(node=None)` | raw response incl. `region` (the actual crop rect the CLI envelope shows) |
| `await client.sprite_info(path)` | `sprite-info <node-path>` |
| `await client.errors(since=0, limit=100)` | `errors [--since MARKER] [--limit N]` — `limit=0` is a marker-only baseline query |
| `await client.get_scene_tree(depth, max_nodes=None)` | `tree [depth] [--max-nodes N]` |
| `await client.wait_for_node(path, timeout)` | `wait-node <path> [timeout]` |
| `await client.wait_game_time(seconds)` | `wait-time <seconds>` |
| `await client.wait_property(path, prop, value, op, timeout, tolerance)` | `wait-prop <path> <prop> <json-value> [--op ...] [--timeout N] [--tolerance N]` |
| `await client.wait_signal(path, signal, timeout)` | `wait-signal <path> <signal> [--timeout N]` |
| `await client.wait_frames(frames, physics)` | `wait-frames <N> [--physics]` |
| `await client.action_press(action)` | `press <action>` |
| `await client.action_release(action)` | `release <action>` |
| `await client.action_tap(action, duration)` | `tap <action> [duration]` |
| `await client.hold(action, duration)` | `hold <action> <duration>` |
| `await client.combo(steps)` | `combo --steps-json '...'` |
| `await client.combo_cancel()` | `combo-cancel` |
| `await client.release_all()` | `release-all` |
| `await client.get_pressed()` | `pressed` |
| `await client.list_input_actions(include_builtin)` | `actions [--all]` |
| `await client.time_scale(value=None)` | `time-scale [value]` — `value=None` reads current; pass a float to set |
| `await client.pause()` | `pause` |
| `await client.unpause()` | `unpause` |
| `await client.step_frames(frames, physics=False)` | `step-frames <n> [--physics]` — requires tree to be paused (error `1009` otherwise) |

## `def run(bridge)` script mode

For multi-step scenarios that don't fit a single CLI call (and where keeping one client connection alive matters for performance), write a Python script with a `run(bridge)` entry point and invoke it via `godot-cli-control run my_script.py`. The runner auto-starts the daemon (if not already running) and tears it down on exit.

```python
# my_script.py
def run(bridge):
    bridge.click("/root/Main/StartButton")
    bridge.wait(0.5)
    assert bridge.get_text("/root/Main/Score") == "0"
```

`bridge` is a synchronous wrapper around `GameClient` — same method names, no `await`. Sibling-imports work (the script's directory is on `sys.path`).

**Exit code (when `run` started the daemon itself):**
- `0` — script succeeded and daemon stopped cleanly.
- `1` — script raised (envelope carries `code: -1005` with the exception summary; full traceback on stderr).
- `2` — script-path / daemon-start failed, **or** the script succeeded but the auto-`daemon stop` afterwards hit an ffmpeg transcode failure (success envelope still emits, with `daemon_stop_warning` populated; raw `.avi` is preserved).
- `64` — argparse usage error (e.g. malformed `--idle-timeout`).

## pytest plugin (preferred for end-to-end test suites)

`pip install godot-cli-control[pytest]` registers a `pytest11` entry-point that exposes four fixtures, so a Godot e2e test is a one-liner:

```python
def test_jump(godot_daemon, bridge):
    bridge.click("/root/Game/Start")
    bridge.tap("jump")
    assert bridge.get_property("/root/Player", "on_floor") is False
```

- **`godot_daemon`** *(session-scoped)*: starts the daemon for the whole test session and stops it at teardown. If a daemon was **already running** when the session started, the fixture leaves it alone — neither restarts nor kills it. Same test file works in CI and during interactive development.
- **`bridge`** *(function-scoped)*: a fresh `GameBridge` per test; on teardown it calls `release_all()` and closes the socket so a `hold`/`press` left dangling by one test can't bleed into the next. It also restores engine-global time state: best-effort `unpause` + reset `Engine.time_scale` to the value snapshotted at test setup (so `--godot-cli-time-scale 5` suite-wide acceleration survives), preventing a test that crashed after `pause` or `time-scale` from freezing / fast-forwarding every later test.
- **`fresh_scene`** *(function-scoped)*: reloads the current scene before the test so it starts from a clean state; yields the same `bridge` object. Use this as a lightweight per-test isolation primitive whenever leftover scene state between tests would cause flakiness — it's cheaper than restarting the daemon. Requires the project to actually *have* a current scene: in autoload-only / no-main-scene startup states (`get_tree().current_scene == null`) the setup's `scene_reload` fails loudly with error `1008` before the test body runs — drive the project into a scene first (e.g. `scene-change res://...`), or don't use this fixture for those cases.
- **`no_push_errors`** *(function-scoped, opt-in)*: the test fails if the game emitted any new `push_error` during it — the e2e defense against silently-swallowed failures (business assertions green, but the game logged an error). Snapshots the `errors` marker at setup, queries the increment at teardown; yields the same `bridge` object. Warnings don't fail it (query `bridge.errors()` yourself for stricter policies). Two caveats: the failure surfaces as a pytest **ERROR** (teardown-phase) rather than FAIL — same red, different label; and it needs Godot 4.5+ (`Logger` API) — on older engines setup raises `RpcError 1012` (fail-loud beats fake-green).

On a test failure (call phase, test used `bridge`, daemon **not** headless), the plugin also saves a screenshot to `<project_root>/.cli_control/failures/<nodeid>.png` automatically and notes the path in the report sections — CI debugging goes from "re-run with extra logging" to "look at the picture". Headless runs skip this (dummy renderer can't screenshot); screenshot failures never mask the original test failure.

```python
def test_score_resets_on_reload(godot_daemon, fresh_scene):
    # fresh_scene is the bridge — scene was already reloaded at setup
    assert fresh_scene.get_property("/root/Game", "score") == 0
    fresh_scene.call_method("/root/Game", "add_score", [10])
    assert fresh_scene.get_property("/root/Game", "score") == 10
    # next test that uses fresh_scene starts from score == 0 again
```

Pytest CLI options the plugin adds:

| Option | Default | Purpose |
|---|---|---|
| `--godot-cli-port` | `(auto)` | GameBridge port. Default: read from `.cli_control/port` (which the daemon writes when it starts). |
| `--godot-cli-no-headless` | off (i.e. headless) | Drop `--headless`, open a real Godot window |
| `--godot-cli-project-root` | `pytest rootdir` | Override the Godot project root |
| `--godot-cli-time-scale` | `None` (engine default = 1.0) | Set `Engine.time_scale` at daemon startup (e.g. `5` to run the whole suite at 5× speed). Passed as `--cli-time-scale=N` to Godot; valid range `(0, 100]`. |

If the entry-point isn't picking up automatically (rare — usually means an editable install glitch), fall back to listing it in `conftest.py`:

```python
pytest_plugins = ["godot_cli_control.pytest_plugin"]
```

## Module entry-point

`godot-cli-control` and `python -m godot_cli_control` are equivalent — same `main()`. Use `python -m` form when:

- The console script isn't on `PATH` (e.g. a venv that wasn't activated).
- You're shelling out from another Python process that imports the package and wants to be sure it hits the same install.

## Common pitfalls

- **`{"ok": false, "error": {"code": -1001, ...}}` on every RPC** — daemon isn't running. Run `godot-cli-control daemon status` to confirm, then `daemon start`.
- **Node paths must be absolute** — start with `/root/...`. Relative paths return `node not found`.
- **`InvalidMessage` / `did not receive a valid HTTP response`** — `all_proxy` / `http_proxy` env var is hijacking localhost. The client sets `proxy=None` to defend, but if you see weird handshake errors, `unset all_proxy` first.
- **Daemon won't start** — check `.cli_control/godot_bin` exists and points at a real Godot 4 binary, or `export GODOT_BIN=/path/to/godot`. See `godot-cli-control init -h` for the full lookup chain.
- **Output flags work in any position** — `--json` / `--text` / `--no-json` are accepted both before and after subcommands as of this fix.
- **There are two independent `--port` flags — don't confuse them:**
  - Top-level `godot-cli-control --port N <subcommand>`: the GameBridge port an RPC subcommand connects to (auto-discovered from `.cli_control/port`; override only when needed). **Must come before the subcommand.**
  - `daemon start --port N`: the port the daemon itself listens on. This is a local flag of `start`, so — like any other `daemon` flag — its position doesn't matter.
- **`combo` rejects everything with `1004`** — a combo is already running. Call `combo-cancel` (or `release-all`) to abort.
- **`hold` / `press` persist after the command returns** — by design. Each CLI command is its own short-lived connection that closes *cleanly*, and a clean close does **not** release inputs. `hold <action> <dur>` auto-releases after `<dur>` seconds (its timer keeps running in the daemon); a sticky `press <action>` stays held until you call `release <action>` / `release-all` (or the daemon's idle-timeout shuts it down). If a character looks stuck moving, you probably left a `press` dangling — run `release-all`. (An *abnormal* drop — your client crashing or being killed mid-session — does trigger a safety `release-all`, so stuck keys can't outlive a dead client.)
- **`hold` / `tap` / `combo` return *before* the motion finishes — use `--wait` (or `wait-time`) before reading state.** These input commands are asynchronous: `hold move_right 1.0` returns in ~0.4s (it just arms a release-timer in the daemon), but the character keeps moving for the full `1.0` in-game second. If you `get position` immediately you read a *mid-motion* value (e.g. `x=415` instead of the settled `x=540`). Two fixes: ① pass **`--wait`** (`hold move_right 1.0 --wait` blocks until the duration elapses, then `get … position` reads the settled value) — one command, one connection; ② or do it explicitly: `hold move_right 1.0` → `wait-time 1.0` → `get … position`. `--wait` works on `tap` (default `0.1`s) and `combo` (waits the summed step durations) too. Either way, also account for any physics/animation that plays out over extra frames after the input lands.
- **`tree` returns `1005 "scene tree too large"`** — your scene has more than 5000 visible nodes (a Grid / spawned-bullets situation). Pass `--max-nodes 200` to cap, or `children <path>` for one specific subtree.
- **`set` with a string that *looks* like JSON** — value parser parses JSON first. To force a literal `"42"` string, pass `'"42"'`; to set a literal hash sign or array text, JSON-encode it.
- **`daemon start` opens a window when I expected headless** — your stdout is a TTY (interactive terminal). Pass `--headless` explicitly, or shell out from a context where stdout is piped.
- **`run <script>` opens a window even though stdout is piped** — by design. `run` grep's the script for `screenshot` and force-flips to GUI when found, so `bridge.screenshot(...)` doesn't 1006-fail under the dummy renderer. Pass `--no-gui-auto` to disable detection; explicit `--headless` always wins. See issue #65.
- **`screenshot` used to fail with `1006` on the first call** — fixed. GameBridge now waits for the viewport's first frame before opening the port, so `connect succeeded` implies `viewport has rendered ≥ once`. The magic `bridge.wait(1.5)` before the first screenshot in older example scripts is no longer needed.
- **`press`/`tap`/`hold`/`combo` inject `InputEventAction` (no mouse coordinates) — position-dependent `_gui_input` widgets need `click` instead.** These commands route through the engine's event pipeline, so `_input` / `_unhandled_input` callbacks receive the event; however, `InputEventAction` does not carry a screen position. UI controls that rely on cursor position (e.g. `TextureButton` with a custom shape, `TouchScreenButton`) won't fire `_gui_input` correctly — use `click <path>` for those.
- **`wait-signal` must be armed before the action that fires it — each CLI call opens a new connection.** Every invocation of `godot-cli-control` is a fresh process that connects, makes the request, and disconnects. If you call `wait-signal` after the signal has already fired, you'll always timeout. Shell pattern: `godot-cli-control wait-signal /root/A my_signal & godot-cli-control tap jump; wait` — background the wait first, then trigger the action. If you need both on a single connection, use a `run` script (`def run(bridge): ...`) with `client.wait_signal(...)`.
- **Replace magic `wait-time` sleeps with `wait-prop` or `wait-frames`.** Fixed `wait-time 0.3` guesses are fragile — they're too long when the game is fast, too short under load. Prefer: `wait-prop /root/Player on_floor true` (wait for state) or `wait-frames 4` (wait for a specific number of frames to render). These are more reliable and often 2-10× faster.
- **`get` on a compound Variant returns an array + type — you can round-trip it straight into `set`.** `get /root/Player position` returns `{"value": [-2480.0, 1400.0], "type": "Vector2"}`. That `value` array is the exact format `set` accepts: `set /root/Player position '[-2480.0, 1400.0]'`. No conversion needed.
- **Arrays/Dicts nested inside compound Variants encode as arrays but carry no `type` field — use a sub-path to read a typed leaf.** For example, a `Dictionary` property that happens to contain a `Vector3` will give you an untyped array. If you need the type, use `get <node> mydict:somekey` to read the leaf directly and get its type.
- **Sub-path reading a non-existent leaf returns `null` — indistinguishable from a real `null` value (typo only detected at the top-level name).** `get /root/Node position:typo` returns `{"value": null}` with no error — the `":" `suffix is not validated beyond checking that `"position"` exists as a top-level property. Verify your leaf name carefully; the only error you'll get is `1002` if the part before `":"` itself doesn't exist.
- **Python API (`bridge.get_property` / `bridge.get_properties`) returns bare values only — no `type` field.** These convenience methods strip the `type` from the server response to reduce boilerplate. When you need the `type` field (e.g. to distinguish `Vector2` from a plain 2-element array), go through `client.request("get_property", {"path": ..., "property": ...})` directly.
- **After `scene-change` / `scene-reload`, all node paths from the previous scene are stale** — re-locate nodes with `wait-node` before touching them. The new scene root is a brand-new tree; any path cached from the old scene will return `1001 "node not found"`.
- **"Tests green but the game logged errors" is undetectable unless you assert it — use `no_push_errors` (pytest) or the `errors --since` cursor (shell).** Business-level assertions can't see a `push_error` the game swallowed (the classic: a sprite fails to load, the NPC silently doesn't render, every position check still passes). Baseline `errors --limit 0` → act → `errors --since <marker>`. Needs Godot 4.5+ (error `1012` otherwise). Note `errors` only sees what was pushed *after* the bridge booted, and its ring keeps the last 1000 entries (`dropped > 0` = storm overflow).
- **`daemon logs --tail N` works after the daemon died — use it first when the daemon won't start or vanished.** It reads `.cli_control/godot.log` client-side (no RPC, no connection). Don't `daemon status` → copy path → shell `tail` — `logs` is that, in one envelope.
- **Asserting "what does this sprite actually show" — use `sprite-info` (headless-safe), not internal bookkeeping fields; use `screenshot --node` only when you truly need pixels.** `sprite-info`'s `effective_region` / `frame_texture` tell you which atlas rect / frame texture is being drawn — that covers most "is it facing left / showing frame N / mirrored" assertions without a renderer. `screenshot --node` needs a real renderer (headless → `1006` like any screenshot) and is for pixel-level comparison; its `1010` (can't bound the node) usually means you pointed at a container — aim at the child sprite instead; `1011` means the node is off-screen right now.
- **`scene-reload` returning means the OLD scene instance was freed — never reuse node references/paths cached before the reload.** The command blocks until the new scene is ready, but the path strings that were valid in the old scene may now point to different nodes or nothing at all. Always re-query after a reload.
- **`fresh_scene` (pytest fixture) errors with `1008` before the test body even runs — the project has no current scene.** Autoload-only / no-main-scene startup states have `get_tree().current_scene == null`, and the fixture's setup calls `scene_reload`, which then fails loudly with `1008`. This is intended (the fixture's contract is "this test starts with a clean *scene*"), not a daemon problem. Either drive the project into a scene first (`scene-change res://...`) or don't request `fresh_scene` for those tests.
- **`step-frames` requires `pause` first (error `1009`) — the intended pattern is `pause` → `step-frames` → assert → `unpause`.** Error `1009` means the precondition (tree is paused) was not met; it is distinct from `-32602` (bad param value) and `-1003` (CLI usage error). If you get `1009`, call `pause` before `step-frames`.
- **`time-scale` also shortens the wall-clock duration of `wait-time` (game-time semantics unchanged) — don't "compensate" wait times after scaling.** `wait-time 1.0` always waits 1 in-game second regardless of `Engine.time_scale`. At `time_scale=5`, that 1 game-second completes in 0.2 wall-clock seconds — don't multiply your wait values, they're already correct.
- **`pause` / `time-scale` are engine-global state — in raw CLI sequences and `run` scripts, restore them in `try/finally`; only the pytest `bridge` fixture restores them for you.** The daemon outlives your command/script: if it dies between `pause` and `unpause` (or after `time-scale 5`), every later command runs against a frozen / fast-forwarded tree, and the symptom ("everything mysteriously stuck") doesn't point back at time control. The pytest plugin's `bridge` fixture undoes both at teardown (best-effort `unpause` + `time_scale` back to its setup-time snapshot); outside pytest the cleanup is on you.
- **With `--record` (Movie Maker fixed-FPS), `time_scale` still applies — the captured video plays back sped-up.** Don't combine `--record` with a high `time_scale` unless you intentionally want a fast-forward video. Movie Maker renders a fixed number of frames per game-time at the configured `--fps`; with a higher `time_scale` each rendered frame covers more game-time, so the frame count stays the same but the animation appears fast-forwarded at normal playback speed.

---

Generated from godot-cli-control v0.2.15.dev46+gcbba9e534.d20260605. Re-run `godot-cli-control init --skills-only` to refresh.
