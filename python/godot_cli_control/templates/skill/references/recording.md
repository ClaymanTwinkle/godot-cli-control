# Recording video / demo / screen capture

The daemon drives Godot's [Movie Maker](https://docs.godotengine.org/en/stable/tutorials/animation/creating_movies.html) (`--write-movie`) so input you script through this skill is captured to disk. Pipeline: Godot writes raw `.avi`; `daemon stop` shells out to `ffmpeg` to transcode into `.mp4` next to the original.

```bash
godot-cli-control daemon start --record --movie-path out.avi --fps 60
godot-cli-control click /root/Main/StartButton
godot-cli-control tap jump 0.3
godot-cli-control daemon stop          # → produces out.mp4
```

## Key constraints

- `--record` **requires** `--movie-path` (daemon refuses to start otherwise).
- `--movie-path` must end in **`.avi` or `.png`** (case-insensitive) — all Godot Movie Maker can write. Other extensions (e.g. `.mp4`) are rejected up front with `-1003` / exit 64 (older Godot behavior was a silent no-record false-success). Want an `.mp4`? Pass `.avi` — the transcode at `daemon stop` produces it.
- `--record` needs a **real renderer** — it cannot combine with `--headless` (Movie Maker's `add_frame()` reads the viewport texture; the headless dummy renderer leaves it null → SIGSEGV). The daemon rejects `--record --headless` with exit 2 before launching. You don't need `--gui`: `--record` auto-opens a window even in non-TTY shells.
- **macOS occlusion**: `--record` sets the window `always_on_top` by default to prevent stale duplicate frames from macOS render throttling of occluded windows. `--no-always-on-top` disables.
- The `.mp4` is produced **only when `daemon stop` runs**; `kill -9` leaves just the raw `.avi`.
- `daemon stop` exits the game gracefully (internal `quit` RPC before SIGTERM), so Movie Maker flushes its buffer and **no tail frames are lost** — no sacrificial `wait()` pad needed. Falls back to SIGTERM if the RPC fails.
- `ffmpeg` must be on `PATH`. If transcoding fails, the raw `.avi` is kept and `daemon stop` exits `4` (log at `.cli_control/ffmpeg.log`; envelope stays `ok:true` with `daemon_stop_warning`).
- `--fps` sets the **fixed simulation framerate** Godot runs at while recording — set it to your target video framerate.
- Output path is relative to cwd; use absolute paths if your script changes cwd.
- A recording longer than the client's 600 s wall-time fail-safe needs `GODOT_CLI_LONG_OP_TIMEOUT=<seconds>` (see `daemon-multi-instance.md`).
- **`time_scale` still applies under `--record`** — Movie Maker renders a fixed `--fps × time_scale` frames per game-second, so a high `time_scale` produces fast-forwarded footage (same frame count, more game-time per frame). Don't combine unless you want that.

## Recipe: warm-then-record (filming a pre-evolved world)

To record a demo that begins from a *long-evolved* world (e.g. many in-game years of simulation), you **cannot** fast-forward inside the recording — raising `Engine.time_scale` under `--record` only fast-forwards the captured footage. Use a two-daemon **split-record** flow: evolve + save in a non-recording daemon, then load + perform in a recording one.

1. **Warm up (non-recording daemon).** Start a normal daemon (`--headless` is fine for pure simulation) and push the world to the target state with `time-scale N` or, better, the game's own sim-speed control. Poll a game-side progress value to know when you've arrived — e.g. `wait-prop /root/GameState day <target>` (substitute your game's node/field).
2. **Persist + stop.** Call the game's save entry point to flush the evolved world to a (sandbox) save slot — `call /root/WorldService save_session` (substitute your own) — then `daemon stop` the warm-up daemon.
3. **Load + record (recording daemon).** Start a `--record` daemon and `run` a script that loads the saved world, waits for it to settle, then drives the camera/player at `time_scale = 1`:

   ```python
   def run(bridge):
       bridge.call("/root/SceneRouter", "continue_game")            # load saved world (game-side API)
       bridge.wait_node("/root/World")                              # settles after `await world.ready`
       bridge.set("/root/World/Player", "global_position", [x, y])  # reset the start AFTER the load settles
       # ... scripted walk / clicks, captured to the movie ...
   ```

Known gotchas (all hit in real downstream demo work):

- **Headless `time_scale` overshoots.** Headless runs uncapped, so a high `time_scale` blows past the target in a single tick. Prefer a GUI daemon (frame-rate capped) and poll a game-side counter, or warm up in small `time-scale` steps.
- **SIGTERM close-save can clobber your save.** If the game saves on quit, the graceful exit `daemon stop` performs may overwrite the slot with the warm-up state. Confirm the save landed before stopping, disable on-quit save during warm-up, or treat the recording-side load as the source of truth and reset the start there.
- **The load settles asynchronously.** A `continue_game`-style call places entities only after `await world.ready`. In the recording script, `wait-node` / `wait-prop` for readiness **before** overriding the start point — a position set too early is overwritten when the load resolves.

Boundary: to fast-forward *within* a recording, drive the game's **own** sim-speed (leaves Movie Maker's frame budget untouched), not engine `time_scale` (fast-forwards the footage).
