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
godot-cli-control daemon status            # exit 0 = running, 1 = stopped
godot-cli-control daemon stop              # stop cwd-project daemon (rc 0; rc 2 = ffmpeg transcode failed)
godot-cli-control daemon stop --project /path/to/other/godot/project
godot-cli-control daemon stop --all        # stop every registered daemon; exit 3 if any failed
godot-cli-control daemon ls                # list all running daemons (cross-project, walks the registry)
```

- **`daemon status` payload when running**: `{"state": "running", "pid": N, "port": M}`.
- **`daemon status` payload when stopped**: `{"state": "stopped"}`. If the previous launch wrote `.cli_control/godot.log` or recorded an exit code, the envelope also includes `"last_log": "<path>"` and/or `"last_exit_code": <int>` — use these to diagnose why the daemon died without manually grepping under `.cli_control/`.
- **`daemon ls` payload**: `{"daemons": [{"project_root", "pid", "port", "started_at", "godot_bin", "log_path"}, ...]}`. Dead records (PID gone) are auto-pruned on each call, so this is the canonical list of *actually-alive* daemons across all projects on the machine.
- **`daemon stop --all` payload**: `{"stopped": [{"project_root","pid","port","rc"[, "error"]}, ...], "rc": 0|3}`. Each entry's `rc` is the per-project stop result; the top-level `rc` is the aggregate exit code.

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
| `-1003` | Usage error (`combo` got no steps, malformed `--steps-json`, `combo -` from a TTY, a non-numeric `tap`/`wait-time` arg, a `set`/`call` value that fails JSON parsing, script path not found, or script missing `run(bridge)`). Always exits **64** (#111). Fix the invocation. |
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

**Render:**
- `screenshot <path>` — write PNG (path is **required** as of 0.2.0)

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
{{cli_help}}
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
| `await client.screenshot()` | `screenshot <path>` |
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

`pip install godot-cli-control[pytest]` registers a `pytest11` entry-point that exposes two fixtures, so a Godot e2e test is a one-liner:

```python
def test_jump(godot_daemon, bridge):
    bridge.click("/root/Game/Start")
    bridge.tap("jump")
    assert bridge.get_property("/root/Player", "on_floor") is False
```

- **`godot_daemon`** *(session-scoped)*: starts the daemon for the whole test session and stops it at teardown. If a daemon was **already running** when the session started, the fixture leaves it alone — neither restarts nor kills it. Same test file works in CI and during interactive development.
- **`bridge`** *(function-scoped)*: a fresh `GameBridge` per test; on teardown it calls `release_all()` and closes the socket so a `hold`/`press` left dangling by one test can't bleed into the next.
- **`fresh_scene`** *(function-scoped)*: reloads the current scene before the test so it starts from a clean state; yields the same `bridge` object. Use this as a lightweight per-test isolation primitive whenever leftover scene state between tests would cause flakiness — it's cheaper than restarting the daemon.

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
- **`scene-reload` returning means the OLD scene instance was freed — never reuse node references/paths cached before the reload.** The command blocks until the new scene is ready, but the path strings that were valid in the old scene may now point to different nodes or nothing at all. Always re-query after a reload.

---

Generated from godot-cli-control v{{version}}. Re-run `godot-cli-control init --skills-only` to refresh.
