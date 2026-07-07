# Common pitfalls — the full list

Every entry below was hit in real agent sessions. Organized by theme; the highest-frequency ones are duplicated in condensed form in `../SKILL.md`.

## Contents

- [Connection & targeting](#connection--targeting)
- [Input simulation](#input-simulation)
- [Waiting & timing](#waiting--timing)
- [Values: get / set / call](#values-get--set--call)
- [Scene & engine-global state](#scene--engine-global-state)
- [Render, screenshot & recording](#render-screenshot--recording)
- [Diagnostics](#diagnostics)
- [Legacy notes (obsolete workarounds to remove)](#legacy-notes)

## Connection & targeting

- **`{"ok": false, "error": {"code": -1001, ...}}` on every RPC** — daemon isn't running. `daemon status` to confirm, then `daemon start`.
- **Node paths must be absolute** — start with `/root/...`. Relative paths return `node not found`.
- **Multiple instances running and you forgot `--instance`** — exit 64 / `-1003`, message lists the running names. Pick one and re-run with `--instance <name>` (`--name` for `daemon` subcommands).
- **`-1003` "port file is not readable yet … retry in a moment"** — the instance is alive but mid-startup (pid file exists, port file doesn't). Transient race, not a config problem: re-run. Mostly seen firing RPCs immediately after `daemon start` in a parallel script.
- **`InvalidMessage` / `did not receive a valid HTTP response`** — `all_proxy` / `http_proxy` is hijacking localhost. The client sets `proxy=None` to defend, but on weird handshake errors `unset all_proxy` first.
- **Daemon won't start** — check `.cli_control/godot_bin` points at a real Godot 4 binary, or `export GODOT_BIN=...`. Then `daemon logs --tail 50` (works even though the daemon is dead) and `daemon status` (`last_log` / `last_exit_code`).
- **Two independent `--port` flags** — top-level `--port` selects which daemon an RPC talks to; `daemon start --port` sets the listen port. See `daemon-multi-instance.md`.
- **Output flags work in any position** — `--json` / `--text` / `--no-json` are accepted before and after subcommands.

## Input simulation

- **`hold` / `tap` / `combo` return *before* the motion finishes — use `--wait` (or `wait-time`) before reading state.** They arm the input and return (~0.4 s) while the character keeps moving for the full duration; an immediate `get position` reads a mid-motion value. `--wait` blocks until the duration elapses in one command/connection (`tap` default 0.1 s; `combo` waits the summed step durations). Also account for physics/animation that settles over extra frames after the input ends.
- **`hold` / `press` persist after the command returns** — by design: each CLI command is a short-lived connection that closes *cleanly*, and a clean close does not release inputs. `hold` auto-releases after its duration (the timer lives in the daemon); a sticky `press` stays until `release` / `release-all` / daemon idle-timeout. Character stuck moving → `release-all`. (An *abnormal* drop — client crash/kill — does trigger a safety `release-all`.)
- **`combo` rejects everything with `1004`** — a combo is already running. `combo-cancel` (or `release-all`) first.
- **Coordinate-level mouse (`click-at` / `mouse-move` / `drag`) vs node/action-level input.** `press`/`tap`/`hold`/`combo` inject `InputEventAction` (no screen position); `click <path>` emits straight to a known node. Position-dependent controls (`TextureButton` with custom shape, `TouchScreenButton`, world-space `Area2D` picking, slider thumbs, drag-and-drop) need real positioned events → `click-at` / `mouse-move` / `drag` (viewport physical pixels; `--node`/`--from-node`/`--to-node` for node centers). They go through `Viewport.push_input`, so `_input` / `_gui_input` / physics picking see correct `position`/`relative`/`button_mask` — **but the `Input` singleton is NOT updated** (`get_global_mouse_position`, `is_mouse_button_pressed` won't see it). `Area2D` picking also needs the project's physics object picking enabled. `drag` runs on game-time and only one may be in flight (`1014`); `release-all` cancels it and emits the pending mouse-up.
- **Want to fire a signal to drive UI (e.g. `ItemList.select()` doesn't emit `item_selected`)** — use `emit-signal` with a daemon started with `--allow-emit-signal`. `call <node> emit_signal` is permanently blocked by the blacklist.

## Waiting & timing

- **`wait-signal` must be armed before the action that fires it.** Every CLI invocation is a fresh process — fire-then-wait always times out. Preferred: `--trigger` arms and fires on the same connection (`wait-signal /root/Area door_opened 3 --trigger 'tap interact'`); the server connects the handler first, then runs the trigger, and the `timeout` covers only the signal wait (slow triggers like `combo`/`drag` don't eat the budget). The miss envelope's `trigger_result` tells "trigger failed" from "game never emitted". Fallback without `--trigger`: background the wait first (`wait-signal ... & ... tap jump; wait`).
- **Replace magic `wait-time` sleeps with `wait-prop` or `wait-frames`.** Fixed guesses are too long when the game is fast and too short under load; condition waits are more reliable and often 2-10× faster.
- **`time-scale` also shortens the wall-clock duration of `wait-time` — don't compensate.** `wait-time 1.0` always waits 1 in-game second; at `time_scale=5` that's 0.2 s wall-clock. The values are already correct.

## Values: get / set / call

- **`set` with a value that *looks* like JSON** — the parser tries JSON first: bare `null`/`true`/`42` become JSON literals, not strings. Force a literal string with `'"42"'`, or pass `--text-value` to disable JSON parsing for all values (best for generated commands).
- **`get` on a compound Variant returns `{"value": [...], "type": ...}` — the array round-trips straight into `set`.** No conversion needed.
- **`call` into a typed method just works — and fails loud when it can't.** JSON arrays are coerced from the method signature; mismatches (length, element type, arity, inconvertible scalar) return `-32602`, never a fake `ok: true / result: null`. Trust the success envelope.
- **Arrays/Dicts nested inside compounds encode as arrays without `type`** — read a typed leaf with a sub-path (`get <node> mydict:somekey`).
- **Sub-path leaf typos on closed compound types fail loud on both `get` and `set`** (`1002` listing valid leaves; nested paths validate per level). Open/dynamic types (Dictionary, Array, Object) still return `{"value": null}` / pass a `set` through for unknown leaves.
- **`tree` returns `1005 "scene tree too large"`** — more than 5000 visible nodes. `--max-nodes 200`, or `tree <path>` / `children <path>` for a subtree.
- **Locating a button by its label — use `find`, never a client-side `children`/`text` walk.** Programmatic UI gets anonymous unstable paths (`@Button@12`); a client-side walk costs one RPC per node (50–150 ms each under `--record`; once burned 57 s of recorded dead time). `find --type Button --contains 开始` → feed `matches[0].path` into `click`. Exact-text flag is `--exact`, not `--text`. Re-run `find` after `scene-change`/`scene-reload`.

## Scene & engine-global state

- **After `scene-change` / `scene-reload`, all node paths from the previous scene are stale** — the new scene is a brand-new tree; cached paths return `1001` or, worse, point at different nodes. Re-locate with `wait-node` / `find`.
- **`pause` / `time-scale` are engine-global and outlive your command/script — restore in `try/finally`.** If your script dies between `pause` and `unpause` (or after `time-scale 5`), every later command runs against a frozen / fast-forwarded tree and the symptom doesn't point back at time control. Only the pytest `bridge` fixture auto-restores (best-effort `unpause` + snapshot-reset of `time_scale`).
- **`step-frames` requires `pause` first (error `1009`)** — the intended pattern is `pause` → `step-frames` → assert → `unpause`.
- **`fresh_scene` errors with `1008` before the test body runs** — the project has no current scene (autoload-only / no-main-scene startup). Intended fail-loud. Drive into a scene first (`scene-change res://...`) or don't request the fixture.
- **`daemon start` opens a window when you expected headless** — stdout is a TTY. Pass `--headless`, or shell out with stdout piped.
- **`run <script>` opens a window even though stdout is piped** — by design: `run` greps the script (plus same-directory imports, one level deep) for `screenshot` and force-flips to GUI so `bridge.screenshot(...)` doesn't `1006`-fail. `--no-gui-auto` disables; explicit `--headless` wins.

## Render, screenshot & recording

- **`screenshot` needs a real renderer** — headless daemon → `1006`, permanent. Restart with `--gui` (or let `run`'s auto-detection handle it).
- **Asserting "what does this sprite show" — prefer `sprite-info` (headless-safe) over pixels.** `effective_region` / `frame_texture` answer "facing left / frame N / mirrored" without a renderer. `screenshot --node` is for true pixel comparison; `1010` usually means you aimed at a container — aim at the child sprite; `1011` means off-screen right now.
- **`screenshot` returns `stale_suspect: true`** — bytes identical to the previous shot. Risk hint, not proof (static scenes trigger it too; root cause is macOS occlusion throttling — mitigated server-side by `RenderingServer.force_draw()`, and by `always_on_top` under `--record`). For certainty force a state change (`wait-frames 2`) and re-shoot.
- **Recording**: `--movie-path` must be `.avi`/`.png`; `--record` can't be `--headless`; the `.mp4` only appears on `daemon stop`; `time_scale` fast-forwards the footage. Full constraints + warm-then-record recipe: `recording.md`.

## Diagnostics

- **"Tests green but the game logged errors" is undetectable unless you assert it** — business assertions can't see a swallowed `push_error` (classic: sprite fails to load, NPC silently doesn't render, every position check passes). Use `no_push_errors` (pytest) or the cursor pattern (`errors --limit 0` → act → `errors --since <marker>`). Needs Godot 4.5+ (`1012` otherwise). The ring keeps the last 1000 entries (`dropped > 0` = overflow) and only sees errors pushed after the bridge booted.
- **`daemon logs --tail N` works after the daemon died — use it first when the daemon won't start or vanished.** It reads the log client-side (no RPC). Don't `daemon status` → copy path → shell `tail`; `logs` is that in one envelope.

## Legacy notes

Behaviors that older scripts may still work around — the workarounds are obsolete, remove them:

- `bridge.wait(1.5)` before the first screenshot: unnecessary — GameBridge waits for the viewport's first frame before opening the port.
- Shrinking the window to 1280×720 before capturing (old `-1001 "Connection closed by server"` on hiDPI frames): obsolete — the daemon writes PNGs to disk, no image bytes cross the socket.
- Sacrificial `wait()` pads before `daemon stop` in recording scripts: obsolete — graceful quit flushes Movie Maker's buffer.
