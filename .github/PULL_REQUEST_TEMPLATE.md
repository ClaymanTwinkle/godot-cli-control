## What & why

<!-- What does this change do, and why? -->

Closes #

## Checklist

- [ ] Tests added/updated, and `coverage run -m pytest python/tests/` passes (coverage ≥ 80%)
- [ ] `ruff check` is clean
- [ ] GUT tests pass if GDScript changed (`GODOT_BIN=... python addons/godot_cli_control/tests/run_gut.py`)
- [ ] **Changed a CLI subcommand, an error code, or a default behavior?** Then I also updated:
  - [ ] `python/godot_cli_control/templates/skill/SKILL.md`
  - [ ] the error-code / exit-code / command tables in `addons/godot_cli_control/README.md`
- [ ] **New RPC?** I added the full chain: `GameClient` → `GameBridge` → `cli.py` (`RpcSpec` + handler + text formatter) → GDScript handler
- [ ] No `--no-verify`, no skipped/`xfail`'d tests, no commented-out failing code
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)

## Notes for the reviewer

<!-- Anything tricky, follow-ups deferred to issues, screenshots, etc. -->
