# Command semantics reference

Full per-command semantics. For exact flags run `godot-cli-control <cmd> -h`; for the one-line catalogue see `../SKILL.md`.

## Contents

- [JSON envelope examples](#json-envelope-examples)
- [Read commands](#read-commands) (`get` sub-paths, `tree` truncation, `find`)
- [Write / call](#write--call) (security blacklist, JSON→Variant coercion tables)
- [Input simulation](#input-simulation) (`combo` schema, coordinate-level mouse)
- [Wait commands](#wait-commands)
- [Scene / time](#scene--time)
- [Render / diagnostics](#render--diagnostics)

## JSON envelope examples

```bash
$ godot-cli-control exists /root/Main
{"ok": true, "result": true}

$ godot-cli-control click /root/DoesNotExist
{"ok": false, "error": {"code": 1001, "message": "Node not found: /root/DoesNotExist", "hint": "path must start with /root; locate by text/type with `find`, or `wait-node <path>` if it may not be loaded yet"}}

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

$ godot-cli-control run my_script.py
{"ok": true, "result": {"exit_code": 0, "script": "my_script.py"}}

$ godot-cli-control run broken.py
{"ok": false, "error": {"code": -1005, "message": "运行失败：RuntimeError: assertion failed"}}
```

`init` and `run` honour the same envelope. In `run --json` mode the user script's `print()` output is redirected to stderr so the envelope stays on a single stdout line.

## Read commands

### `get <path> <prop> [<prop2> ...]`

- Single property → `{"value": <encoded>, "type": "<GodotType>"}`; the `type` field is present only for compound Variants (Vector2, Color, …), absent for primitives (`bool`/`int`/`float`/`String`).
- Multiple properties → `{"values": {"<prop>": {"value": ..., "type"?: ...}, ...}}`, read atomically in the same frame.
- Sub-path form `get <path> position:x` reads a scalar leaf of a compound Variant (returns `{"value": 1.5}`, no type field).
- **Leaf typo fail-loud**: on a *closed* compound type — Vector2/3/4 (incl. `i` variants), Color, Rect2/Rect2i, Transform2D/3D, Basis, Plane, Quaternion, AABB, Projection — an unknown leaf fails with `1002` listing the valid leaves; nested paths validate per level (`transform:basis:typo` also fails loud). Open/dynamic types (Dictionary, Array, Object) still return `{"value": null}` for unknown leaves (keys can't be enumerated safely).
- Security note: sub-path reading can reach write-blacklisted nested attributes (e.g. `script:source_code`) — intentional read-only diagnostics under the localhost + debug-build gate.

### Variant encoding depth limit

`get` encodes recursively. Containers nested deeper than **64 levels**, or self-referencing containers, are replaced with the sentinel string `"<max-depth-exceeded>"`. If you see it, narrow the read with a sub-path (`get /root/Node mydict:somekey`).

### `tree [path] [depth] [--max-nodes N]`

- Omit `path` → current scene root. Pass an absolute path (`tree /root`, `tree /root/GameUI 2`) to dump that subtree — this is how you reach autoload singletons (siblings of the current scene under `/root`). A first arg starting with `/` is the path, otherwise it's the depth (`tree 2` = depth 2).
- Default `--max-nodes 200` keeps the JSON LLM-friendly. On overflow the response carries `"truncated": true` and `"total_nodes": N`:

```json
{"ok": true, "result": {"tree": {"...": "partial subtree"}, "truncated": true, "total_nodes": 6000}}
```

- Hard server ceiling: 5000 nodes → `1005 "scene tree too large"`. Narrow with `--max-nodes`, `tree <path>`, `children <path>`, or `tree 1`.

### `find` — server-side node search

`find [--from <path>] [--type <Class>] [--exact <text>|--contains <substr>] [--name-pattern <glob>] [--limit N]`

- The canonical way to locate programmatically-built UI whose node paths are anonymous and unstable (`@Button@12` — renumbered every run). One RPC folds the whole traversal.
- Filters AND together; at least one required (checked pre-flight, exit 64).
- `--type` matches subclasses (`--type BaseButton` finds `Button`s) and `class_name` script classes. `--exact` / `--contains` match the node's `text` property (mutually exclusive; `--exact` is NOT `--text` — that's the global output toggle). `--name-pattern` is a case-sensitive glob (`*`/`?`) on the node name.
- Searches from `/root` by default (popups + autoloads included); scope with `--from`.
- Returns `{"matches": [{name, type, path, text?, visible?}], "truncated"?: true}` in BFS order (shallowest first); default `--limit 20`, server cap 500.
- Exit codes: 0 = ≥ 1 match, 1 = zero matches — so `find --contains OK && ...` works.
- **Just want to click what you'd find?** Use `click` with the same filters directly (next section) — one atomic RPC, no path parsing, no stale-path window.

## Write / call

### `click` — by path or by filters (atomic find+click)

Two mutually exclusive ways to target (both flags → `-32602`; CLI preflight rejects earlier with `-1003`):

- `click <path>` — the original form (BaseButton → emits `pressed`; other Controls → synthesized `gui_input` click; Area2D → `input_event`).
- `click --contains <substr> | --exact <text> [--type <Class>] [--name-pattern <glob>] [--from <path>]` — find-style filters resolved **server-side in the same frame**, then clicked. The one-liner for "click the button labeled X" on programmatically-built UI; replaces the `find` → parse `matches[0].path` → `click` three-step and its stale-path race.
- The filters must match **exactly one** node: 0 matches → `1001`; ≥ 2 → `1017` with the candidate paths in the message — narrow with `--exact` / `--type` / `--from`, or `find` first and click the path. Deliberately fail-loud: BFS order is not something an agent should bet a click on.
- On success the filter form returns `{"success": true, "path": "<actual node clicked>"}` — cache the path if you need to hit the same node again cheaply.

### Security blacklist

The plugin refuses methods/properties that could break the scene or escalate: `queue_free`, `free`, `set_script`, `set_meta`, reflection (`set`, `call`, `callv`, `call_deferred`), `connect`/`disconnect`, `add_child`/`remove_child`, anything `_*` (private). Blocked → `-32602 "Blocked method/property: <name>"`. Use the typed surface instead (e.g. `set hp`, not `call set hp`); to "remove" a node set `visible=false` rather than `queue_free`. Third-party projects extend via the `godot_cli_control/method_blacklist_extra` ProjectSettings (additive only).

`emit_signal` is special: always blacklisted through `call`, but available as the dedicated `emit-signal` subcommand when the daemon was started with `--allow-emit-signal` (debug-build + localhost + explicit opt-in; otherwise server error `1015`).

### Value parsing (`set` / `call` / `emit-signal` args)

Each value parses as JSON first, falling back to string:

```bash
godot-cli-control set /root/Player position '[100, 200]'   # array → Vector2
godot-cli-control set /root/Score   text     '"42"'        # explicit string "42"
godot-cli-control set /root/Score   text     hello         # implicit string "hello"
godot-cli-control set /root/Player  hp       30            # number 30
godot-cli-control call /root/Game start_game 1 '"easy"'    # int 1, string "easy"
```

**Footgun**: bare `null` / `true` / `false` / numeric strings parse as JSON literals, **not** strings. Escape hatch (preferred for generated commands): `--text-value` disables JSON parsing entirely — `set /root/Label text null --text-value` stores the string `"null"`.

### Array → Variant coercion (`set`)

The server reads the property's declared type and converts a numeric JSON array to the matching Variant. Layout is **axis-vector order** — each row below is one axis, the array is the axes concatenated:

| Variant | Length | Layout |
|---|---|---|
| `Vector2 / 2i` | 2 | `[x, y]` |
| `Vector3 / 3i` | 3 | `[x, y, z]` |
| `Vector4 / 4i` | 4 | `[x, y, z, w]` |
| `Rect2 / 2i` | 4 | `[x, y, w, h]` |
| `Color` | 3 or 4 | `[r, g, b]` (a=1) or `[r, g, b, a]` |
| `Plane` | 4 | `[normal.x, normal.y, normal.z, d]` ¹ |
| `Quaternion` | 4 | `[x, y, z, w]` ¹ |
| `AABB` | 6 | `[pos.xyz, size.xyz]` |
| `Basis` | 9 | `[x_axis.xyz, y_axis.xyz, z_axis.xyz]` |
| `Transform2D` | 6 | `[x_axis.xy, y_axis.xy, origin.xy]` |
| `Transform3D` | 12 | `[basis 9 axis-vector, origin.xyz]` |
| `Projection` | 16 | `[x_axis.xyzw, y_axis.xyzw, z_axis.xyzw, w_axis.xyzw]` |

¹ Not auto-normalized — pass unit vectors for correct rotation/distance semantics.

Wrong length / non-numeric elements fail loud with `-32602 "value type mismatch ..."` (never a silent `(0, 0)`).

- **Sub-path + Array is rejected** (`-32602`): Godot's `set_indexed` would silently drop it. Sub-paths are scalar-only (`set <node> position:x 1.8`); write whole compounds via the top-level array form.
- **Sub-path leaf typo fails loud with `1002`** (symmetric with `get`): `set <node> position:zz 5` lists the valid leaves instead of silently no-oping with `{"success": true}`.

### `call` typed arguments

Same coercion, driven by the method signature (`get_method_list()`): `call /root/Mob enable_wander '[0, 0, 640, 480]'` reaches `func enable_wander(rect: Rect2)` as a real `Rect2`. Fails loud with `-32602` on: wrong array length / non-numeric element, a JSON array handed to a scalar/Object parameter, wrong argument count (honors optional params and varargs), or a scalar the engine can't implicitly convert (numeric group `bool`/`int`/`float` and string group `String`/`StringName`/`NodePath` convert within their group; a string for an `int` param fails). A successful-looking `call` that silently did nothing is not possible for typed methods — trust the envelope.

`get` on a compound returns the exact array `set` accepts — round-trip without conversion.

## Input simulation

### Action-level: `press` / `release` / `tap` / `hold` / `combo`

Inject an `InputEventAction` through the engine's event pipeline: both polling (`is_action_pressed`, `get_vector`) **and** callbacks (`_input`, `_unhandled_input`) see them. `InputEventAction` carries **no mouse coordinates** — for position-dependent widgets use the coordinate-level commands below.

- `tap`/`hold`/`combo` are async by default — they return as soon as the input is armed. `--wait` blocks until the duration elapses (game-time), folding an implicit `wait-time` into the same connection.
- `hold` requires `duration > 0`; for an indefinite hold use `press`.
- Unknown action → `1003` `"Unknown action: <name>"`; list valid ones with `actions` (or `actions --all`).

### `combo` step schema

Steps from one of three mutually-exclusive sources: `--steps-json '[...]'` (inline, preferred), `combo file.json`, or `combo -` (stdin). Each step:

- `{"action": "<InputMap action>", "duration": <seconds, default 0.1>}` — press, hold `duration`, release
- `{"wait": <seconds>}` — pause without touching input

```bash
godot-cli-control combo --steps-json \
  '[{"action":"jump","duration":0.2},{"wait":0.5},{"action":"attack"}]'
```

A `{"steps": [...]}` wrapper is also accepted. Steps run strictly serially; while a combo is in flight any `press`/`release`/new `combo` → `1004 "combo in progress"` (abort with `combo-cancel` or `release-all`).

### Coordinate-level: `click-at` / `mouse-move` / `drag`

- Coordinates are **viewport physical pixels** (same system as `screenshot --node`); `--node` / `--from-node` / `--to-node` target a node's screen center.
- Injected via `Viewport.push_input`, so `_input` / `_gui_input` / physics picking see correct `position` / `relative` / `button_mask` — **but the global `Input` singleton is NOT updated**: `get_global_mouse_position()` / `is_mouse_button_pressed()` polling won't see it; read mouse state from the event.
- `Area2D` picking additionally requires the project's *physics object picking* enabled.
- `drag` interpolates over `--duration` (game-time, scaled by `time_scale`) in `--steps` increments with the button held. One drag at a time (`1014` if another is mid-flight); `release-all` cancels an in-flight drag and emits the pending mouse-up.

## Wait commands

- `wait-node <path> [timeout]` — exit 0 = found, 1 = timeout.
- `wait-time <seconds>` — in-game seconds (matters under `--write-movie` and `time_scale`). Server bounds `0 ≤ s ≤ 3600` (`-32602` outside); `s ≤ 0` short-circuits client-side.
- `wait-prop <path> <prop> <json-value> [--op eq|ne|gt|lt|ge|le] [--timeout N] [--tolerance N]` — e.g. `wait-prop /root/Player position:x 500 --op gt`. Defaults: `--op eq`, `--timeout 5.0`, `--tolerance 0.0`. Timeout result carries `reason` (`timeout` / `node_not_found` / `property_not_found`).
- `wait-signal <path> <signal> [timeout] [--trigger '<subcommand>']` — `--trigger` arms the wait, then runs the given RPC subcommand on the same connection, then waits. The `timeout` covers only the signal wait, not the trigger's execution. Success: `{"emitted": true, "args": [...], "trigger_result": {...}}`. Miss: `{"emitted": false, "reason": "timeout"|"node_freed"}` (`node_freed` = target freed mid-wait — distinguishes a race from a stale path); with `--trigger`, the miss envelope still includes `trigger_result` so you can tell "trigger failed" from "game never emitted". `--trigger` accepts a single RPC subcommand; for multi-step triggers pass a `combo`.
- `wait-frames <N> [--physics]` — advance exactly N process (or physics) frames. Result `{"success": true, "frames": N}`.

## Scene / time

- `scene-reload [--timeout N]` — reload current scene, block until ready (per-test isolation primitive). Failure (no current scene / file missing / load timeout) → `1008`.
- `scene-change <res://path.tscn> [--timeout N]` — path must start with `res://` or `uid://` (pre-flight); `--timeout` in `(0, 3600]`, default 10.
- Both invalidate every previously cached node path.
- `time-scale [value]` — read (no arg) / set `Engine.time_scale`, range `(0, 100]`. `wait-time` counts game time, so acceleration doesn't change wait semantics — wall-clock just shrinks.
- `pause` / `unpause` — `get_tree().paused`; idempotent; returns `{"paused": bool}`. `wait-time` keeps counting while paused (`process_always` timer), so `wait-time` + `get` can verify a frozen state.
- `step-frames <n> [--physics]` — while paused, advance exactly N frames (1..3600). Requires `pause` first (else `1009`). Returns `{"stepped": N, "paused": true}`.

## Render / diagnostics

### `screenshot <path> [--node <node-path>]`

- The PNG is written **by the daemon directly to disk** (CLI resolves to absolute path and creates parent dirs) — no payload crosses the WebSocket, so hiDPI / 4K captures work at any size.
- `--node` crops to the node's screen-space AABB (canvas/camera transform included); envelope reports `{"path", "bytes", "node", "region": [x,y,w,h]}` in viewport pixels.
- Errors: `1001` unknown node, `1010` bounds undeterminable (aim at the child sprite, not a container), `1011` off-screen, `1013` daemon can't write the path, `1006` headless / no renderer.
- The server calls `RenderingServer.force_draw()` before capture — freshest frame even when the window is occluded. `"stale_suspect": true` appears when bytes are identical to the previous screenshot — a risk hint, not proof (static scenes trigger it too); for certainty `wait-frames 2` and re-shoot.

### `sprite-info <node-path>`

Aggregate "what is this node actually rendering" for `Sprite2D` / `AnimatedSprite2D` / `TextureRect` (`1010` for others). Pure property read — **works headless**. Key fields:

- common: `type`, `visible`, `visible_in_tree`, `modulate` `[r,g,b,a]`; textures as `{"path": "res://..."|null, "size": [w,h]}` (+ `atlas`/`atlas_region` for AtlasTexture); `path` null for runtime-built textures.
- `Sprite2D`: `flip_h/v`, `frame`, `frame_coords`, `hframes/vframes`, `region_enabled`, `region_rect`, and **`effective_region` `[x,y,w,h]`** — the atlas rect actually drawn (region wins when enabled, else computed from the frame grid). Assert "shows frame N" against this.
- `AnimatedSprite2D`: `sprite_frames`, `animation`, `frame`, `playing`, `speed_scale`, `flip_h/v`, and **`frame_texture`** — the current frame's texture (distinguishes FRONT vs BACK frames no plain property exposes).
- `TextureRect`: `texture`, `flip_h/v`, `stretch_mode`, `expand_mode`, `size`.

### `errors [--since MARKER] [--limit N]`

Structured query of `push_error` / `push_warning` captured at runtime (Godot 4.5+ `Logger`; older engines → `1012`). Returns `{"errors": [{seq, type, message, source, file, line, unix_time, ticks_msec}], "marker", "dropped", "truncated"}`:

- `type`: `"error"` / `"warning"` / `"script"` / `"shader"`; `source` is the GDScript call site (`res://x.gd:12 @ func`) — far more useful than `file`/`line` (the C++ origin).
- **Cursor pattern**: `errors --limit 0` for a baseline marker → run your action → `errors --since <marker>` to see only what that action produced. This is the primitive behind "this test must produce zero push_errors".
- `dropped > 0` = ring-buffer overflow (last 1000 kept); `truncated` = more matched than `--limit` (page with `--since <returned marker>`).
- Only sees what was pushed after the bridge booted.
