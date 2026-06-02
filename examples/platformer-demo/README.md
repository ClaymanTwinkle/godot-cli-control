# Platformer demo

A tiny Godot 4 project that exists for one purpose: to be driven from outside by
[`godot-cli-control`](../../). A **Start** button brings a character on stage;
the character can jump and run, and deliberately exposes `jump_count` /
`moved_right` so a black-box test can assert on them.

It does double duty as:

- the **hero-GIF source** for the project README, and
- a **clone-and-run example** — what you see in the GIF, you can reproduce here.

## Run it

```bash
pipx install godot-cli-control      # or, from this repo: pip install -e python
cd examples/platformer-demo
./drive.sh
```

[`drive.sh`](drive.sh) is the canonical surface — a sequence of
`godot-cli-control` subcommands. It will:

1. `init` — copy the addon in and patch `project.godot` (gitignored, idempotent);
2. start a windowed daemon;
3. click **Start**, then `tap jump` and `hold move_right`;
4. read `jump_count` back (→ `1`);
5. screenshot to `frame.png`;
6. stop the daemon.

You can also open the project in the Godot editor and play it by hand
(**Space** = jump, **→** = move right) — `player.gd` binds those keys at runtime.

## What's committed vs. generated

This is a **pristine player project** — only the game itself is committed:

```
project.godot   # scene config + two InputMap actions (jump, move_right)
main.tscn       # Main ▸ Background, World ▸ {Ground, Player}, UI ▸ StartButton
main.gd         # Start button → reveal + activate the player
player.gd       # CharacterBody2D: gravity + jump + move; exposes jump_count / moved_right
drive.sh        # the canonical drive script above
```

The control surface is **not** committed here. `godot-cli-control init` appends
the `[autoload]` + `[editor_plugins]` sections and copies
`addons/godot_cli_control` in on first run — all gitignored. That keeps the
addon in a single source of truth (repo-root `addons/`) and lets the demo
exercise `init` for real.

## Regression-tested

This demo is not allowed to rot. [`python/tests/test_e2e_example.py`](../../python/tests/test_e2e_example.py)
drives it under a real (headless) Godot and asserts `jump_count == 1` and
`moved_right == true`. If the scene layout or the CLI surface breaks, CI goes red.
