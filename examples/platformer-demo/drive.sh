#!/usr/bin/env bash
# godot-cli-control · platformer demo — canonical drive script.
#
# This is the full minimal loop of "an external process driving a running Godot
# scene": install the bridge, start the daemon, click a button, simulate input,
# read state back, screenshot, tear down. The shell IS the canonical surface —
# every line below is one `godot-cli-control` subcommand emitting a single-line
# JSON envelope, exactly what an AI agent or a CI job would run.
#
# Prereq: `pipx install godot-cli-control` (or, from this repo, `pip install -e python`).
set -euo pipefail
cd "$(dirname "$0")"

# 0) One-time: install the control surface into this project (copies the addon,
#    patches project.godot, detects the Godot binary). addons/, .godot/ and
#    .cli_control/ are all gitignored, so this never dirties the repo. Idempotent.
godot-cli-control init

# 1) Start the daemon with a window (GUI mode — screenshots need a real renderer).
godot-cli-control daemon start --gui

# 2) Wait for the scene, then click Start to bring the character on stage.
godot-cli-control wait-node /root/Main/UI/StartButton
godot-cli-control click     /root/Main/UI/StartButton

# 3) Let the character land, then drive it: one jump, then run right for 1s.
godot-cli-control wait-node /root/Main/World/Player
godot-cli-control wait-time 1.0
godot-cli-control tap  jump
godot-cli-control hold move_right 1.0

# 4) Read state back, black-box: jump_count should be 1 — the same property the
#    CI regression test (python/tests/test_e2e_example.py) asserts on.
godot-cli-control get /root/Main/World/Player jump_count

# 5) Screenshot for the record (README hero shot / debugging).
godot-cli-control screenshot frame.png

# 6) Tear down.
godot-cli-control daemon stop
echo "✓ demo finished — screenshot at ./frame.png"
