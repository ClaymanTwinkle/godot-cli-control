# Python API, `run(bridge)` scripts, and the pytest plugin

## Python `GameClient` API (async)

`from godot_cli_control.client import GameClient` — async WebSocket client; use as `async with GameClient() as client:`. With no `port` argument it **auto-discovers** from `.cli_control/instances/<name>/port` (the file the daemon writes), falling back to `9877` — a no-arg `GameClient()` connects to a running daemon out of the box. `GameClient(port=N)` overrides; `GameClient(instance="server")` targets a named instance (explicit `port` wins). `GameBridge(instance="server")` works identically in `run` scripts.

Errors raise `RpcError(code, message)` (a `RuntimeError` subclass) preserving the server's error code — useful for retrying `1004 "combo in progress"`.

**Every method has a 1-line CLI equivalent; reach for Python only to keep one connection across many steps.**

| Method | CLI equivalent |
|---|---|
| `await client.click(path)` | `click <path>` |
| `await client.click_at(x, y, node=None, button="left", double=False)` | `click-at <x> <y> \| --node <path> [--button] [--double]` |
| `await client.mouse_move(x, y, node=None)` | `mouse-move <x> <y> \| --node <path>` |
| `await client.drag(x1, y1, x2, y2, from_node=None, to_node=None, button="left", duration=0.3, steps=10)` | `drag <x1> <y1> <x2> <y2> \| --from-node/--to-node <path> [--button] [--duration] [--steps]` |
| `await client.get_property(path, prop)` | `get <path> <prop>` — returns bare value only (no `type`); use `client.request("get_property", ...)` for the full shape |
| `await client.get_properties(path, props)` | `get <path> <prop1> <prop2> ...` — returns `{prop: bare_value}`; `client.request("get_properties", ...)` for full shape |
| `await client.set_property(path, prop, value)` | `set <path> <prop> <json-value>` |
| `await client.call_method(path, method, args)` | `call <path> <method> [json-args...]` |
| `await client.get_text(path)` | `text <path>` |
| `await client.node_exists(path)` | `exists <path>` |
| `await client.is_visible(path)` | `visible <path>` |
| `await client.get_children(path)` | `children <path>` |
| `await client.find_nodes(node_type=None, text=None, text_contains=None, name_pattern=None, from_path=None, limit=20)` | `find [--type] [--exact] [--contains] [--name-pattern] [--from] [--limit]` — `text`=exact, `text_contains`=substring (mutually exclusive) |
| `await client.screenshot(node=None)` | `screenshot <path> [--node]` — returns PNG bytes over the socket; watch the 10 MB outbound buffer (`1016`) |
| `await client.screenshot_raw(node=None, path=None)` | raw response incl. `region`; pass an **absolute** `path` to have the daemon write the PNG itself (`{path, bytes}` metadata only; parent dir must exist, `1013` otherwise) |
| `await client.sprite_info(path)` | `sprite-info <node-path>` |
| `await client.errors(since=0, limit=100)` | `errors [--since] [--limit]` — `limit=0` is a marker-only baseline |
| `await client.get_scene_tree(depth, max_nodes=None, path=None)` | `tree [path] [depth] [--max-nodes N]` |
| `await client.wait_for_node(path, timeout)` | `wait-node <path> [timeout]` |
| `await client.wait_game_time(seconds)` | `wait-time <seconds>` |
| `await client.wait_property(path, prop, value, op, timeout, tolerance)` | `wait-prop ...` |
| `await client.wait_signal(path, signal, timeout, on_armed=...)` | `wait-signal <path> <signal> [timeout] [--trigger '<subcommand>']` |
| `await client.wait_frames(frames, physics)` | `wait-frames <N> [--physics]` |
| `await client.action_press(action)` / `action_release(action)` | `press` / `release <action>` |
| `await client.action_tap(action, duration)` | `tap <action> [duration]` |
| `await client.hold(action, duration)` | `hold <action> <duration>` |
| `await client.combo(steps)` | `combo --steps-json '...'` |
| `await client.combo_cancel()` / `release_all()` / `get_pressed()` | `combo-cancel` / `release-all` / `pressed` |
| `await client.list_input_actions(include_builtin)` | `actions [--all]` |
| `await client.time_scale(value=None)` | `time-scale [value]` — `None` reads |
| `await client.pause()` / `unpause()` | `pause` / `unpause` |
| `await client.step_frames(frames, physics=False)` | `step-frames <n> [--physics]` — requires paused tree (`1009`) |

Note: `bridge.get_property` / `bridge.get_properties` (and the client convenience methods) strip the `type` field. When you need it (e.g. to distinguish `Vector2` from a plain 2-element array), call `client.request("get_property", {...})` directly.

## `def run(bridge)` script mode

For multi-step scenarios where keeping one client connection matters, write a script with a `run(bridge)` entry point and invoke `godot-cli-control run my_script.py`. The runner auto-starts the daemon (if not running) and tears down what it started.

```python
# my_script.py
def run(bridge):
    bridge.click("/root/Main/StartButton")
    bridge.wait(0.5)
    assert bridge.get_text("/root/Main/Score") == "0"
```

`bridge` is a synchronous wrapper around `GameClient` — same method names, no `await`. Sibling imports work (the script's directory is on `sys.path`).

Exit codes (when `run` started the daemon itself): `0` success; `1` script raised (`-1005`, traceback on stderr); `2` script-path / daemon-start failure (`-1006`); `4` script succeeded but the auto-stop hit an ffmpeg transcode failure (raw `.avi` preserved); `64` usage error.

`run` accepts `--time-scale N` and `--allow-emit-signal` (passed through to the auto-started daemon), plus `--no-gui-auto` to disable the screenshot→GUI detection.

## Module entry-point

`godot-cli-control` and `python -m godot_cli_control` are equivalent. Use `python -m` when the console script isn't on `PATH` (unactivated venv) or you must hit the same install as an importing process.

## pytest plugin (preferred for e2e test suites)

`pip install godot-cli-control[pytest]` registers a `pytest11` entry-point exposing five fixtures:

```python
def test_jump(godot_daemon, bridge):
    bridge.click("/root/Game/Start")
    bridge.tap("jump")
    assert bridge.get_property("/root/Player", "on_floor") is False
```

- **`godot_daemon`** *(session-scoped)*: starts the daemon for the session, stops it at teardown. If a daemon was already running, leaves it alone — same test file works in CI and interactive dev.
- **`bridge`** *(function-scoped)*: fresh `GameBridge` per test; teardown calls `release_all()` and closes the socket (no dangling `hold`/`press` bleeding between tests), plus restores engine-global time state: best-effort `unpause` + reset `Engine.time_scale` to the setup-time snapshot — a test that crashed after `pause`/`time-scale` can't freeze every later test.
- **`fresh_scene`** *(function-scoped)*: reloads the current scene before the test; yields the same `bridge`. Cheap per-test isolation. Requires an actual current scene: autoload-only / no-main-scene startup → setup fails loudly with `1008` (drive into a scene first with `scene-change`, or skip this fixture).
- **`no_push_errors`** *(function-scoped, opt-in)*: fails the test if the game emitted any new `push_error` during it — the e2e defense against silently-swallowed failures. Snapshots the `errors` marker at setup, checks the increment at teardown; yields the `bridge`. Warnings don't fail it. Caveats: the failure surfaces as a pytest **ERROR** (teardown-phase), and it needs Godot 4.5+ (`RpcError 1012` otherwise — fail-loud beats fake-green).
- **`godot_instances`** *(scope configurable, default function)*: multi-instance factory for multiplayer e2e:

  ```python
  def test_join(godot_instances):
      server = godot_instances.start("server")
      client = godot_instances.start("client1")
      # connected GameBridge objects; teardown stops every instance
      # this fixture started (and only those)
  ```

  `start(name)` is idempotent get-or-start; `headless` / `time_scale` default to the global CLI options, overridable per call; `port` always defaults to 0 (OS-assigned). An instance already running before the test is connected to but not restarted/stopped. `stop(name)` stops one mid-test (disconnect scenarios; can be `start`ed again); `daemon(name)` exposes the underlying `Daemon`; both raise `KeyError` for never-started names. `--godot-cli-instances-scope session` shares one set across the suite (faster, no state isolation).

On a call-phase test failure using `bridge` with a non-headless daemon, the plugin saves a screenshot to `<project_root>/.cli_control/failures/<nodeid>.png` and notes the path in the report — CI debugging becomes "look at the picture". Headless runs skip this; screenshot failures never mask the original failure.

```python
def test_score_resets_on_reload(godot_daemon, fresh_scene):
    # fresh_scene is the bridge — scene was already reloaded at setup
    assert fresh_scene.get_property("/root/Game", "score") == 0
    fresh_scene.call_method("/root/Game", "add_score", [10])
    assert fresh_scene.get_property("/root/Game", "score") == 10
```

### Pytest CLI options the plugin adds

| Option | Default | Purpose |
|---|---|---|
| `--godot-cli-port` | auto | GameBridge port (read from `.cli_control/instances/<name>/port`; legacy `.cli_control/port` fallback) |
| `--godot-cli-no-headless` | off (headless) | Open a real Godot window |
| `--godot-cli-project-root` | pytest rootdir | Override the Godot project root |
| `--godot-cli-time-scale` | engine default 1.0 | `Engine.time_scale` at daemon startup (range `(0, 100]`) — e.g. `5` runs the whole suite at 5× |
| `--godot-cli-instances-scope` | `function` | `godot_instances` scope: `function` (best isolation) or `session` (faster) |

If the entry-point doesn't pick up (rare editable-install glitch), list it in `conftest.py`:

```python
pytest_plugins = ["godot_cli_control.pytest_plugin"]
```
