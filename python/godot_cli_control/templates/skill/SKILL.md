---
name: godot-cli-control
description: Use when driving a Godot 4 game from a script or terminal — clicking buttons, simulating input actions, taking screenshots, dumping the scene tree, writing pytest end-to-end tests against a live Godot scene (via the bundled pytest plugin / `godot_daemon` + `bridge` fixtures), or recording video / screen capture / demo replays (Godot Movie Maker, `--write-movie`, auto-transcoded to mp4 via ffmpeg). Trigger when the user mentions godot-cli-control, the godot-cli-control CLI/daemon, the `bridge` / `godot_daemon` pytest fixtures, or asks to automate / scrape / black-box-test / record / capture / film / e2e-test a Godot scene.
---

# godot-cli-control

WebSocket bridge for headless / scripted control of Godot 4 scenes. A daemon process owns a running Godot instance; clients (the CLI or the Python `GameClient`) send RPC over `ws://127.0.0.1:<port>` to click nodes, simulate input actions, read/write properties, dump the scene tree, take screenshots, and record movies.

## When to use

**Use this when:**
- Black-box testing a Godot UI from outside the engine
- Recording a demo / regression video by scripting input
- Building a bot or smoke test that exercises the running game
- Scraping the live scene tree or a node's text/property

**Don't use this when:**
- You can write a normal GDScript unit test (use GUT inside the project instead)
- You need to reason about source logic — read the `.gd` files directly
- The change is to the Godot project's data layer with no UI involvement

## Quick start

```bash
# Once per Godot project (already done if you can read this file):
godot-cli-control init

# Per session:
godot-cli-control daemon start          # boots Godot in the background
godot-cli-control daemon status         # exit 0 = running, 1 = stopped
godot-cli-control tree 3                # confirm RPC works
# ... your work ...
godot-cli-control daemon stop
```

## Shell vs. Python: which surface to use

- **Shell CLI** covers a deliberately small set: `click`, `screenshot`, `tree`, `press`, `release`, `tap`, `hold`, `combo`, `release-all`, plus `daemon {start,stop,status}` / `run` / `init`. Good for one-off probes from a terminal or shell pipeline.
- **Python `GameClient` / `bridge`** covers everything the shell does **plus** read/write helpers (`get_property`, `set_property`, `get_text`, `node_exists`, `is_visible`, `get_children`, `wait_for_node`, `wait_game_time`, `combo_cancel`, …). These are **not** exposed as CLI subcommands — for multi-step assertions or reads, write a `def run(bridge):` script and invoke it via `godot-cli-control run my_script.py`.

Rule of thumb: if you need to *read* anything beyond `tree`, you want the Python side.

### Recording a video / demo / screen capture

The daemon can drive Godot's [Movie Maker](https://docs.godotengine.org/en/stable/tutorials/animation/creating_movies.html) (`--write-movie`) so input you script through this skill is captured to disk as a video. Pipeline: Godot writes raw `.avi` (or PNG sequence), the daemon's `stop` step shells out to `ffmpeg` to transcode that into `.mp4` next to the original.

```bash
# Headless recording with scripted input (best for CI / non-interactive runs):
godot-cli-control daemon start --record --movie-path out.avi --headless --fps 60

godot-cli-control click /root/Main/StartButton
godot-cli-control tap jump 0.3
# ... whatever input drives the demo ...

godot-cli-control daemon stop
# → produces out.mp4 in cwd; original out.avi is removed on success
```

Or in one shot via `run`:

```bash
godot-cli-control run record_demo.py --record --movie-path out.avi --headless --fps 60
```

Key constraints:

- `--record` **requires** `--movie-path` (daemon refuses to start otherwise).
- The `.mp4` is produced **only when `daemon stop` runs**; killing the daemon with `kill -9` leaves the raw `.avi` behind.
- `ffmpeg` must be on `PATH` for transcoding. If it's missing or transcoding fails, the raw `.avi` is kept and `daemon stop` exits with code `2` (transcode log at `.cli_control/ffmpeg.log`).
- `--fps` controls the **fixed simulation framerate** Godot runs at while recording — set it to your target video framerate, not your screen's.
- `--headless` is recommended for CI and when no display is available. For visual recordings of a windowed run, drop `--headless`.
- Output path is relative to the current working directory; pass an absolute path if your script changes cwd.

### `combo` JSON schema

`combo` reads a JSON file shaped as either `[step, step, …]` or `{"steps": [step, step, …]}`. Each `step` is one of:

- `{"action": "<InputMap action>", "duration": <seconds, default 0.1>}` — press the action, hold for `duration` seconds, then auto-release.
- `{"wait": <seconds>}` — pause without touching input.

Minimal `combo.json`:

```json
[
  {"action": "jump", "duration": 0.2},
  {"wait": 0.5},
  {"action": "attack"}
]
```

Steps run strictly serially; while a combo is in flight any `press`/`release`/new `combo` is rejected with error `1004 "combo in progress"`. Use `release-all` to abort.

## CLI reference

<!-- BEGIN cli_help (auto-generated by godot-cli-control init) -->
```
{{cli_help}}
```
<!-- END cli_help -->

## Python `GameClient` API

`from godot_cli_control.client import GameClient` — async WebSocket client; use as `async with GameClient(port=...) as client:`.

| Method | Purpose | Also as CLI? |
|---|---|---|
| `await client.click(path)` | Click a Control/Button node by absolute scene path | `click` |
| `await client.get_property(path, prop)` | Read a node property | — |
| `await client.set_property(path, prop, value)` | Write a node property | — |
| `await client.call_method(path, method, *args)` | Invoke an arbitrary node method | — |
| `await client.get_text(path)` | Shortcut for label/button text | — |
| `await client.node_exists(path)` | Boolean existence check | — |
| `await client.is_visible(path)` | Visibility check | — |
| `await client.get_children(path)` | Direct children (one level) | — |
| `await client.screenshot()` | Returns PNG bytes | `screenshot` |
| `await client.get_scene_tree(depth=5)` | Tree dump as nested dict | `tree` |
| `await client.wait_for_node(path, timeout=5.0)` | Poll until node appears | — |
| `await client.wait_game_time(seconds)` | Wait N in-game seconds | — |
| `await client.action_press(action)` | Press an InputMap action (sticky) | `press` |
| `await client.action_release(action)` | Release a sticky action | `release` |
| `await client.action_tap(action, duration=0.1)` | Press → wait → release | `tap` |
| `await client.hold(action, duration)` | Hold for N seconds, auto-release | `hold` |
| `await client.combo(steps)` | Run a `[{action/wait}, …]` sequence (see schema above) | `combo` |
| `await client.combo_cancel()` | Abort running combo | — |
| `await client.release_all()` | Release every held action | `release-all` |

## `def run(bridge)` script mode

For multi-step scenarios that don't fit a single CLI call, write a Python script with a `run(bridge)` entry point and invoke it via `godot-cli-control run my_script.py`. The runner auto-starts the daemon (if not already running) and tears it down on exit.

```python
# my_script.py
def run(bridge):
    bridge.click("/root/Main/StartButton")
    bridge.wait_game_time(0.5)
    assert bridge.get_text("/root/Main/Score") == "0"
```

`bridge` is a synchronous wrapper around `GameClient` — same method names, no `await`. Sibling-imports work (the script's directory is on `sys.path`).

## pytest plugin (preferred for end-to-end test suites)

`pip install godot-cli-control[pytest]` registers a `pytest11` entry-point that exposes two fixtures, so a Godot e2e test is a one-liner:

```python
def test_jump(godot_daemon, bridge):
    bridge.click("/root/Game/Start")
    bridge.tap("jump")
    assert bridge.get_property("/root/Player", "on_floor") is False
```

- **`godot_daemon`** *(session-scoped)*: starts the daemon for the whole test session and stops it at teardown. If a daemon was **already running** when the session started (e.g. you launched it from your IDE for dev), the fixture leaves it alone — neither restarts nor kills it. This makes the same test file work in CI and during interactive development.
- **`bridge`** *(function-scoped)*: a fresh `GameBridge` per test; on teardown it calls `release_all()` and closes the socket so a `hold`/`press` left dangling by one test can't bleed into the next.

Pytest CLI options the plugin adds:

| Option | Default | Purpose |
|---|---|---|
| `--godot-cli-port` | `9877` | GameBridge port the fixture connects to / starts the daemon on |
| `--godot-cli-no-headless` | off (i.e. headless) | Drop `--headless`, open a real Godot window — useful for debugging a flaky test |
| `--godot-cli-project-root` | `pytest rootdir` | Override the Godot project root (point at a different `project.godot`) |

If the entry-point isn't picking up automatically (rare — usually means an editable install glitch), fall back to listing it in `conftest.py`:

```python
pytest_plugins = ["godot_cli_control.pytest_plugin"]
```

When to choose this over `godot-cli-control run my_script.py`: any time you have **more than one** scenario, want assertion failure messages from pytest, parametrize, or run as part of an existing CI suite. The `run`-script mode is for ad-hoc one-shots.

## Module entry-point

`godot-cli-control` and `python -m godot_cli_control` are equivalent — same `main()`. Use `python -m` form when:

- The console script isn't on `PATH` (e.g. a venv that wasn't activated).
- You're shelling out from another Python process that imports the package and wants to be sure it hits the same install.

## Common pitfalls

- **`ConnectionRefused` on every RPC** — daemon isn't running. Run `godot-cli-control daemon status` to confirm, then `daemon start`.
- **Node paths must be absolute** — start with `/root/...`. Relative paths return "node not found".
- **`InvalidMessage: did not receive a valid HTTP response`** — `all_proxy` / `http_proxy` env var is hijacking localhost. The client sets `proxy=None` to defend, but if you see weird handshake errors, `unset all_proxy` first.
- **Daemon won't start** — check `.cli_control/godot_bin` exists and points at a real Godot 4 binary, or `export GODOT_BIN=/path/to/godot`. See `godot-cli-control init -h` for the full lookup chain.
- **Top-level `--port` doesn't change daemon port** — it only routes RPC subcommands. To make `daemon start` / `run` listen on a non-default port, pass `--port` *after* the subcommand: `godot-cli-control daemon start --port 9888`.
- **`combo` rejects everything with `1004`** — a combo is already running. Wait for it to finish or call `release-all` to cancel.
- **`screenshot` without an output path floods stdout** — output is base64 of a multi-megabyte PNG. Always pass an explicit `out.png` unless you really want it in the terminal.

---

Generated from godot-cli-control v{{version}}. Re-run `godot-cli-control init --skills-only` to refresh.
