---
name: godot-cli-control
description: Use when driving a Godot 4 game from a script or terminal — clicking buttons, simulating input actions, taking screenshots, dumping the scene tree, reading or writing node properties, calling node methods, listing InputMap actions, writing pytest end-to-end tests against a live Godot scene (via the bundled pytest plugin / `godot_daemon` + `bridge` fixtures, or the `godot_instances` multi-instance factory for server + client multiplayer e2e), or recording video / screen capture / demo replays (Godot Movie Maker, `--write-movie`, auto-transcoded to mp4 via ffmpeg). Trigger when the user mentions godot-cli-control, the godot-cli-control CLI/daemon, the `bridge` / `godot_daemon` / `godot_instances` pytest fixtures, or asks to automate / scrape / black-box-test / record / capture / film / e2e-test a Godot scene.
---

# godot-cli-control

WebSocket bridge for headless / scripted control of Godot 4 scenes. A daemon process owns a running Godot instance; clients (the CLI or the Python `GameClient`) send JSON-RPC over `ws://127.0.0.1:<port>` to click nodes, read & write properties, call methods, simulate input, dump the scene tree, take screenshots, and record movies.

## How to use this skill (read on demand)

This file is the core contract — enough for most sessions. Details live in `references/` next to this file; read a reference **only when you need it**:

| You need … | Read |
|---|---|
| Exact flags / syntax of one command | `godot-cli-control <cmd> -h` (live, always current — no static copy here) |
| Full command semantics: `get` sub-paths, `set`/`call` JSON→Variant coercion tables, `find` filters, `combo` schema, `tree` truncation, coordinate-level mouse | `references/commands.md` |
| An error code you don't recognize / retry-vs-fail decision | `references/error-codes.md` |
| Daemon flags, named instances, `--instance all` broadcast, project config defaults | `references/daemon-multi-instance.md` |
| Recording video (Movie Maker), transcode pipeline, filming a pre-evolved world | `references/recording.md` |
| pytest fixtures, `GameClient` (async Python), `def run(bridge)` scripts | `references/python-and-pytest.md` |
| Something behaves unexpectedly and the top pitfalls below don't cover it | `references/pitfalls.md` (the full battle-tested list) |

If `references/` is missing, this install is from an older version — re-run `godot-cli-control init --skills-only`.

## AI Quickstart

**The shell CLI is canonical.** Everything you can do from Python you can do from `godot-cli-control <subcommand>`. Default to the shell — drop to a `def run(bridge):` script only when you genuinely need one connection across many steps.

**Output is JSON by default.** Every RPC subcommand prints a single-line envelope on stdout:

- success: `{"ok": true, "result": <data>}`
- error:   `{"ok": false, "error": {"code": <int>, "message": "...", "hint": "next step (optional)"}}`

Pipe straight into `jq` or `json.loads`. `--text` / `--no-json` switch to legacy human-readable output.

**Node paths must be absolute** — start with `/root/`. Relative paths return "node not found".

```bash
# One-time per Godot project (already done if you're reading this file):
godot-cli-control init

# Per session (single instance):
godot-cli-control daemon start --headless         # boots Godot in the background
godot-cli-control daemon status                   # exit 0 = running, 1 = stopped
godot-cli-control tree 2 | jq .result             # confirm RPC works
# ... your work ...
godot-cli-control daemon stop

# Per session (multiple instances, e.g. server + client):
godot-cli-control daemon start --name server --headless
godot-cli-control daemon start --name client1 --headless
godot-cli-control --instance server tree 2 | jq .result
godot-cli-control --instance client1 click /root/Game/JoinButton
godot-cli-control daemon stop --all --project .
```

> `daemon start` autodetects headless mode by checking `stdout.isatty()`: pipes, CI, and agent shell-outs run headless by default; an interactive terminal gets a window. `--headless` / `--gui` override. `run <script>` additionally force-flips to GUI when the script (or its same-directory imports) mentions `screenshot` — headless daemons can't render; `--no-gui-auto` disables the detection.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (or boolean true: `exists` / `visible` / `wait-*` hit / `find` matched) |
| 1 | RPC error from the server; also the semantic false: `exists`/`visible`=false, `wait-*`=timeout, `find`=zero matches, `daemon status`=stopped |
| 2 | Connection / IO error (daemon not running) or infra pre-condition failure (daemon failed to start) — client code `-1006` |
| 3 | Aggregate partial/total failure: `daemon stop --all` or an `--instance all` broadcast where ≥ 1 target's per-entry `rc` ≠ 0 |
| 4 | Recording-only soft failure: process stopped cleanly, raw `.avi` kept, but ffmpeg `.avi`→`.mp4` transcode failed (envelope stays `ok:true` + `daemon_stop_warning`) |
| 64 | Usage error (argparse, preflight reject, bad runtime argument, script path / `run(bridge)` missing, multi-instance targeting error) — always client code `-1003` |

Shell-`if` works:

```bash
if godot-cli-control exists /root/Main/Boss; then
  godot-cli-control click /root/Main/Boss
fi
```

## Command catalogue

One line per command; run `<cmd> -h` for flags, read `references/commands.md` for full semantics.

**Read:**
- `get <path> <prop> [<prop2> ...]` — read properties (multi = same-frame atomic). Compound Variants return `{"value": [..], "type": "Vector2"}` — the array round-trips into `set`. Sub-path reads a leaf: `get <path> position:x`.
- `text <path>` — read Label / Button text
- `exists <path>` / `visible <path>` — boolean checks (exit-code-as-result)
- `children <path> [type-filter]` — direct children
- `tree [path] [depth] [--max-nodes N]` — scene tree as JSON (default cap 200 nodes; `truncated: true` signals overflow). `tree /root` reaches autoloads.
- `find [--type <Class>] [--exact <text>|--contains <substr>] [--name-pattern <glob>] [--from <path>] [--limit N]` — **server-side node search in one RPC**; the way to locate programmatically-built UI with anonymous paths (`@Button@12`). Exit 0 = matched, 1 = zero matches. Never walk the tree client-side with repeated `children`/`text` calls.
- `pressed` — currently held simulated inputs
- `actions [--all]` — InputMap actions (default hides `ui_*` builtins)

**Write / call:**
- `set <path> <prop> <json-value>` — write a property (JSON-first value parsing; see footgun in pitfalls)
- `call <path> <method> [json-args...]` — call a method (typed-signature coercion, fails loud on mismatch)
- `click <path>` — UI click on a Control/Button node
- `emit-signal <path> <signal> [args...]` — fire a signal (test seam); needs daemon started with `--allow-emit-signal`, else error `1015`

**Input simulation** (InputMap actions; both polling and `_input` callbacks see them):
- `press <action>` / `release <action>` — sticky press (persists across CLI invocations!)
- `tap <action> [duration] [--wait]` — press → wait → release
- `hold <action> <duration> [--wait]` — auto-release after N seconds
- `combo --steps-json '[...]' [--wait]` — serial sequence of `{"action","duration"}` / `{"wait"}` steps
- `combo-cancel` / `release-all` — abort / release everything
- `click-at <x> <y> | --node <path>` `[--button] [--double]` — coordinate-level mouse click (viewport physical pixels)
- `mouse-move <x> <y> | --node <path>` — one mouse-motion event
- `drag <x1> <y1> <x2> <y2>` (or `--from-node`/`--to-node`) `[--duration] [--steps]` — press → interpolated move → release

**Wait** (replace magic sleeps with these):
- `wait-node <path> [timeout]` — until node appears
- `wait-prop <path> <prop> <json-value> [--op eq|ne|gt|lt|ge|le] [--timeout N] [--tolerance N]` — until property satisfies condition
- `wait-signal <path> <signal> [timeout] [--trigger '<subcommand>']` — until signal fires; `--trigger` arms first, then fires your action on the same connection (kills the race)
- `wait-frames <N> [--physics]` — exactly N frames
- `wait-time <seconds>` — N in-game seconds (scaled by `time_scale`)

**Scene / time:**
- `scene-reload [--timeout N]` / `scene-change <res://path.tscn> [--timeout N]` — blocks until the new scene is ready. **All cached node paths go stale.**
- `time-scale [value]` — read/set `Engine.time_scale` (range `(0, 100]`)
- `pause` / `unpause` — freeze / resume the tree
- `step-frames <n> [--physics]` — while paused, advance exactly N frames (error `1009` if not paused)

**Render / diagnostics:**
- `screenshot <path> [--node <node-path>]` — daemon writes the PNG to disk (any size); `--node` crops to that node's screen rect. Needs a real renderer — headless → `1006`.
- `sprite-info <node-path>` — what a Sprite2D / AnimatedSprite2D / TextureRect actually renders (`effective_region`, `frame_texture`); pure property read, **works headless**
- `errors [--since MARKER] [--limit N]` — structured `push_error`/`push_warning` query (Godot 4.5+). Cursor pattern: `errors --limit 0` → act → `errors --since <marker>`.
- `daemon logs [--tail N]` — tail `godot.log` client-side; works after the daemon died

**Daemon:** `daemon start [--headless|--gui] [--name X] [--port N] [--time-scale N] [--record --movie-path out.avi] [--allow-emit-signal]` / `status` / `stop [--all]` / `ls` / `logs`. Details: `references/daemon-multi-instance.md`.

## Error codes in one minute

**Most errors carry an `error.hint` field — the concrete next step. Follow it first**; the tables exist for the codes that don't (or when you need retry semantics). Three non-overlapping ranges in `error.code` — the range tells you who is wrong:

- **`1xxx`** (server, business): the request was understood but the world refused — `1001` node not found (retry after `wait-node`, or locate with `find`), `1002` property not found, `1003` method/action not found, `1004` combo in progress (`combo-cancel` then retry), `1005` tree too large (`--max-nodes` / subtree), `1006` transient resource (retry once), `1009` not paused (call `pause` first), `1015` emit-signal not enabled.
- **`-32xxx`** (JSON-RPC): request shape is wrong — `-32602` invalid params (blocked by blacklist, type mismatch, wrong arg count). Don't retry; fix the request.
- **`-1xxx`** (client CLI): `-1001` daemon not running (`daemon status`), `-1002` timeout, `-1003` usage error (always exit 64), `-1005` your `run` script raised, `-1006` infra failure (always exit 2).

Full table with retry semantics per code: `references/error-codes.md`.

## Top pitfalls (the ones that actually burn sessions)

1. **`tap` / `hold` / `combo` return *before* the motion finishes.** They arm the input and return immediately; `get position` right after reads a mid-motion value. Add `--wait` to block until the duration elapses, or insert `wait-time <duration>`.
2. **`wait-signal` must be armed before the trigger.** Each CLI call is a fresh process — fire-then-wait always times out. Use `--trigger`: `wait-signal /root/Area door_opened 3 --trigger 'tap interact'` (arms first, fires second, same connection).
3. **`set`/`call` values parse as JSON first.** Bare `null`/`true`/`42` become JSON literals, not strings. Force strings with `--text-value` (best for generated commands) or explicit quotes: `'"42"'`.
4. **Locate buttons with `find`, never a client-side tree walk.** `find --type BaseButton --contains OK` is one RPC; a `children`+`text` recursion is one RPC *per node* (once burned 57 s in a recorded demo). Note: exact-text flag is `--exact` (`--text` is the global output toggle).
5. **After `scene-change` / `scene-reload`, every cached node path is stale** — re-locate via `wait-node` / `find` before touching them.
6. **A sticky `press` outlives the CLI call** — by design (clean disconnects don't release). Character stuck moving? `release-all`.
7. **`pause` / `time-scale` are engine-global and outlive your script** — restore them in `try/finally`; only the pytest `bridge` fixture auto-restores.
8. **≥ 2 instances running → every command needs `--instance <name>`** (else `-1003`, exit 64, message lists the names). Transient `-1003` "port file not readable yet" right after `daemon start` = mid-startup race → just re-run.
9. **`screenshot` needs a real renderer** — headless daemon → `1006`. For "what does this sprite show" assertions prefer `sprite-info` (headless-safe); screenshot only for true pixel checks.
10. **"Tests green but the game logged errors" is invisible unless you assert it** — use the `errors --since` cursor (shell) or the `no_push_errors` pytest fixture. Needs Godot 4.5+.
11. **Replace magic `wait-time` sleeps** with `wait-prop` (state) or `wait-frames` (frames) — faster and not flaky.
12. **Coordinate-level mouse (`click-at`/`drag`) updates events, not the `Input` singleton** — `get_global_mouse_position()` polling won't see it; position-dependent widgets need these commands, `InputEventAction` (`press`/`tap`) carries no coordinates.

More (recording gotchas, hiDPI, value coercion edge cases, legacy migration notes): `references/pitfalls.md`.

---

Generated from godot-cli-control v{{version}}. Re-run `godot-cli-control init --skills-only` to refresh.
