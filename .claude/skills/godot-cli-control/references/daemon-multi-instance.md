# Daemon management & multi-instance reference

## Daemon commands

```bash
godot-cli-control daemon start                           # boot daemon for cwd project (instance "default")
godot-cli-control daemon start --name server             # boot a named instance
godot-cli-control daemon start --name client1 --port 0   # second instance on an OS-assigned port
godot-cli-control daemon start --time-scale 5            # start at 5× game speed
godot-cli-control daemon status                          # exit 0 = running, 1 = stopped
godot-cli-control daemon status --name server            # status for a specific instance
godot-cli-control daemon stop                            # stop cwd-project daemon (auto-selects if 1 running)
godot-cli-control daemon stop --name server              # stop a named instance
godot-cli-control daemon stop --project /path/to/other/godot/project
godot-cli-control daemon stop --all                      # stop every registered daemon; exit 3 if any failed
godot-cli-control daemon stop --all --project /path/to/project  # stop all instances of one project
godot-cli-control daemon ls                              # list all running daemons (cross-project)
godot-cli-control daemon logs --tail 50                  # last N lines of godot.log (works after death, too)
godot-cli-control daemon logs --name server --tail 50    # logs for a specific instance
```

### Payloads

- `daemon status` running: `{"state": "running", "pid": N, "port": M, "instance": "<name>"}`; stopped: `{"state": "stopped"}` plus, when available, `"last_log": "<path>"` and/or `"last_exit_code": <int>` — use these to diagnose why the daemon died without grepping `.cli_control/`.
- `daemon ls`: `{"daemons": [{"project_root", "pid", "port", "instance", "started_at", "godot_bin", "log_path"}, ...]}`. Dead records (PID gone) auto-prune on each call — the canonical machine-wide list of actually-alive daemons.
- `daemon stop --all`: `{"stopped": [{"project_root","pid","port","instance","rc"[, "error"]}, ...], "rc": 0|3}`. A per-instance transcode-only failure shows as that entry's `rc: 4` but does not bump the aggregate.
- `daemon logs [--tail N]`: `{"path", "lines", "returned", "instance"}` (default 50, max 1000). Read client-side — **no RPC**, works post-mortem. No log file yet → `-1006`, exit 2.

### Start flags worth knowing

- `--time-scale N` — sets `Engine.time_scale = N` (range `(0, 100]`) from the very first frame; `run <script>` accepts it too (passed to the auto-started daemon).
- `--allow-emit-signal` (also on `run`) — unlocks the `emit-signal` subcommand for this daemon (three-gate model: debug build + localhost + explicit opt-in). Without it `emit-signal` → `1015`. It does **not** unblock `call <node> emit_signal` (permanently blacklisted).
- `--record --movie-path out.avi [--fps N] [--no-always-on-top]` — Movie Maker recording; see `recording.md`.
- Headless autodetect: non-TTY stdout (pipes, CI, agent shells) → headless; interactive terminal → window. Override with `--headless` / `--gui`.

### Project-level defaults

`.cli_control/config.json` (optional): `{"idle_timeout": "30m"}` makes `daemon start` / `run` auto-quit after idle without re-typing `--idle-timeout`. Read only when the flag isn't passed; explicit flag wins. Bad JSON / malformed duration → `-1003`, exit 64.

### Long-operation wall-time fail-safe

Long `wait-time` / `combo` / recording ops are bounded client-side by a fixed **600 s** wall-time fail-safe (game-time vs wall-time can't be predicted, so it's not per-call). A genuinely longer operation (e.g. a > 10-minute recording) can raise it via `GODOT_CLI_LONG_OP_TIMEOUT=<seconds>`; non-positive / non-numeric values fall back to 600.

## Multi-instance (multiple daemons for one project)

Run more than one Godot instance per project — e.g. a "server" and a "client" of the same game.

```bash
godot-cli-control daemon start --name server
godot-cli-control daemon start --name client1

godot-cli-control --instance server click /root/Game/StartButton
godot-cli-control --instance client1 get /root/Player position

godot-cli-control daemon stop --name server
godot-cli-control daemon stop --all --project .    # or both in one shot
```

`--instance` (top-level, for RPC subcommands) and `--name` (inside `daemon` subcommands) are equivalent; passing both with different values → `-1003` conflict. `--instance` and `--port` are mutually exclusive (both select the target daemon).

### Target-selection semantics (all subcommands)

| Running instances | No `--instance` / `--name` | Explicit `--instance nope` |
|---|---|---|
| 0 | connects to "default" (legacy fallback) | error `-1003`, exit 64 |
| 1 | auto-selects the single instance | error `-1003`, exit 64 |
| ≥ 2 | **error `-1003`, exit 64** — message lists the running names | error `-1003`, exit 64 |

- "Forgot `--instance`" (≥ 2 running) and "named instance not running" are distinct errors with distinct messages; the latter lists the currently-running names so you can pick without a separate `daemon ls`:

```json
{"ok": false, "error": {"code": -1003, "message": "multiple instances running: client1, server — pass --instance <name>"}}
```

- **Transient case**: instance alive but its port file not readable yet (mid-startup, millisecond window) → `-1003` with *"port file is not readable yet … retry in a moment"* instead of a silent 30 s hang. Just re-run.
- **Upgrade note**: a legacy daemon started by an older CLI (pid/port directly under `.cli_control/` instead of `.cli_control/instances/<name>/`) may make `daemon ls` show two lines for one project. Stop the legacy daemon; no manual migration needed. Legacy flat-layout daemons are not broadcast targets.

### Broadcasting: `--instance all`

```bash
godot-cli-control --instance all exists /root/Main          # assert on every instance
godot-cli-control --instance all screenshot /tmp/shot-{instance}.png
```

- Targets every live instance of the cwd project **concurrently**; result array sorted by instance name.
- Envelope (top-level `ok` stays `true`; per-instance failures live in the entries, mirroring `daemon stop --all`):

```json
{"ok": true, "result": {"instances": [
  {"instance": "client1", "ok": true, "result": true, "rc": 0},
  {"instance": "server", "ok": false, "error": {"code": 1002, "message": "..."}, "rc": 1}
], "rc": 3}}
```

- Exit 0 iff every per-instance `rc` is 0, else 3 — so shell-`if` means "true on *all* instances".
- Every string argument gets `{instance}` substituted per target. **Required for `screenshot`** (a path without `{instance}` is rejected pre-flight — all instances would overwrite one file). No escape hatch; applies to all string args including `set`/`call` values.
- `all` is a reserved instance name (`daemon start --name all` rejected).
- RPC subcommands only: `--instance all` with `run` / `daemon` → `-1003` (use `daemon stop --all` to stop everything). 0 live instances → `-1006`, exit 2.
- Output size multiplies by instance count — for `tree`/`children` prefer per-instance calls with tight limits.

### Two independent `--port` flags — don't confuse them

- Top-level `godot-cli-control --port N <subcommand>`: which GameBridge port an RPC connects to (normally auto-discovered from `.cli_control/instances/<name>/port`; legacy `.cli_control/port` as fallback). Accepted both before and after the subcommand.
- `daemon start --port N`: the port the daemon itself listens on.

In pytest suites, don't hand-roll this lifecycle — the `godot_instances` fixture starts named instances, hands you connected `GameBridge` objects, and stops what it started (see `python-and-pytest.md`).
