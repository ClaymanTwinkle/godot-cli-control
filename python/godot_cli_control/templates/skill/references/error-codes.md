# Error code reference

Three numeric ranges cohabit in `error.code`; they never overlap, so a single `code` field is unambiguous. The range tells you who is wrong; the code tells you retry-vs-fail.

**`error.hint`**: most errors also carry an optional `hint` field with the concrete next step — trust it before this table. Server-side hints (`1xxx` / `-32601`) come from the addon (old, un-synced addons don't send them — re-run `init`); client-side hints (`-1xxx`) come from the CLI. Codes whose `message` is already case-specific (`-32602`, `-1003`, `-1005`) carry no hint.

## Server-side (Godot plugin) — positive integers

| Code | Meaning |
|---|---|
| `1001` | Node not found at the given path. Most common — usually a wrong or not-yet-loaded path. Retry after `wait-node`, or locate the node with `find`. |
| `1002` | Property not found on the node, or sub-path leaf typo on a closed compound type (message lists the valid leaves). Don't retry; inspect with `tree`. |
| `1003` | Method not found on the node, **or** unknown InputMap action passed to `press`/`release`/`tap`/`hold`/`combo` (`"Unknown action: <name>"`). Schema error — don't retry. For methods inspect with `tree`; for actions run `actions` (or `actions --all`). |
| `1004` | Combo already in progress. Call `combo-cancel` (or `release-all`) and re-issue. Safe to retry after that. |
| `1005` | Scene tree too large to serialize. Pass `--max-nodes` or query a subtree (`tree <path>` / `children <path>`). Don't retry as-is. |
| `1006` | Resource transiently unavailable (e.g. screenshot during scene transition, or screenshot on a **headless** daemon — the dummy renderer can't read the viewport). Under a real renderer this is rare (`screenshot` retries internally ~30 frames); if you still see it, retry after `wait-time 0.05`. On headless it is permanent — restart the daemon with `--gui`. |
| `1007` | Signal not found on the node (`wait-signal` schema error). Permanent — inspect with `tree` to list signals. |
| `1008` | Scene unavailable (`scene-reload` / `scene-change`): no current scene, scene file missing / failed to load, or timed out waiting for readiness. Missing file is permanent — fix the path; timeout usually means the scene itself fails to load — inspect `daemon logs`. |
| `1009` | NOT_PAUSED: `step-frames` called while the tree is not paused. State precondition error (not a parameter error) — call `pause` first. Don't confuse with `-32602` (bad param) or `-1003` (CLI usage). |
| `1010` | UNSUPPORTED_NODE_TYPE: `sprite-info` on a non-sprite node, or `screenshot --node` on a node whose bounds can't be determined (not a CanvasItem / nothing to measure). Permanent — pick a different node (often the child sprite) or command. |
| `1011` | NODE_NOT_ON_SCREEN: `screenshot --node` computed a rect that doesn't intersect the viewport. State-class error — move the camera/node or wait for it to enter view, then retry. |
| `1012` | FEATURE_UNAVAILABLE: the engine lacks an API this RPC needs. Currently only `errors` (requires Godot 4.5+ `Logger`). Permanent for that engine. |
| `1013` | WRITE_FAILED: the **daemon** couldn't write the screenshot PNG (parent dir missing / no permission). The CLI pre-creates parent dirs, so via the CLI this usually means permissions; raw-RPC callers must create dirs themselves. Distinct from `-1004` (the *CLI* couldn't write locally). Permanent — fix the path. |
| `1014` | DRAG_IN_PROGRESS: a `drag` issued while another is interpolating. One mouse drag at a time — wait for it to finish or `release-all` to cancel. |
| `1015` | EMIT_SIGNAL_DISABLED: `emit-signal` called but the daemon wasn't started with `--allow-emit-signal`. Restart the daemon with the flag (explicit opt-in on top of debug-build + localhost). `call <node> emit_signal` is always blocked by the blacklist regardless. |
| `1016` | RESPONSE_TOO_LARGE: a single response exceeded the daemon's outbound WebSocket buffer (default 10 MB, `godot_cli_control/outbound_buffer_mb`). Almost always a bytes-API screenshot on a hiDPI/4K frame — pass a file path so the daemon writes to disk, or raise the buffer. Retrying re-overflows. |

## JSON-RPC standard — negative integers `-32xxx`

| Code | Meaning |
|---|---|
| `-32600` | Malformed request (missing / non-string `method`). Client bug; should never reach an agent. |
| `-32601` | Unknown method name. Client + plugin versions drifted — re-run `godot-cli-control init` to sync the addon. |
| `-32602` | Invalid params: missing required field, blocked method/property (security blacklist), out-of-range value, value-type mismatch on `set`/`call` (wrong array length, non-numeric element, array to a scalar param, inconvertible scalar, wrong arg count), node-isn't-clickable (e.g. `click` on a Node2D), or `hold` with `duration ≤ 0`. Don't retry; the request shape is wrong. |

## Client-side (CLI / GameClient) — `-1xxx`

| Code | Meaning |
|---|---|
| `-1001` | Connection failure (daemon not running, wrong port, proxy hijacking localhost). Run `daemon status`. |
| `-1002` | Timeout waiting for a response. Daemon may be hung mid-frame; check `daemon logs`. |
| `-1003` | Usage error: bad/missing arguments caught by argparse or preflight (`combo` without steps, non-numeric `tap`/`wait-time`, a `scene-change` path not starting with `res://`/`uid://`, invalid `--timeout`, unparseable `set`/`call` value, script path not found / missing `run(bridge)`, multi-instance targeting errors). Always exits **64**. Fix the invocation. |
| `-1004` | Local file IO error in the CLI process (e.g. can't create the screenshot destination's parent dir). **Not** a daemon problem (that's `1013`). |
| `-1005` | `run <script>`: the user script raised. Message carries the exception summary; full traceback on stderr. Fix the script. |
| `-1006` | Infra pre-condition failure (`daemon start`/`stop`/`run` auto-start failed at the OS level — port conflict, Godot binary not found, PID file missing …). Always exits **2**. Fix the environment. |
| `-1099` | Internal client error (unforeseen exception). Bug in this CLI — please file an issue; stderr has the traceback. |
