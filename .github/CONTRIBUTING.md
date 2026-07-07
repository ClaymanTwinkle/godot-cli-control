# Contributing to godot-cli-control

Thanks for your interest! Issues and pull requests are welcome.

This project has one north star: it is an **AI-friendly CLI** for driving a running Godot 4
scene from the outside (shell, pytest, Python, or an AI agent). Before you change anything,
read the [contract section below](#the-contracts-you-must-not-silently-break) — it is what
makes the tool usable by an agent that can only run one shell command and parse one line of
JSON at a time. Breaking it quietly is the one thing we cannot merge.

## Ways to contribute

- **Report a bug** — use the [bug report template](ISSUE_TEMPLATE/bug_report.yml). Include the
  CLI version, Godot version, OS, the exact command, and the single-line JSON envelope it
  printed. That envelope is usually enough to localize the failure.
- **Request a feature** — use the [feature request template](ISSUE_TEMPLATE/feature_request.yml).
  Say which surface it touches (CLI / `GameClient` / pytest fixture / addon).
- **Send a PR** — see the workflow below.

## Development setup

```bash
git clone https://github.com/ClaymanTwinkle/godot-cli-control.git
cd godot-cli-control

# Python client + CLI, editable, with the full test/lint toolchain
pip install -e ".[test]"
```

The repository has two halves that are tested independently:

```
addons/godot_cli_control/   # Godot 4 GDScript plugin + GUT tests
python/godot_cli_control/   # Python CLI / GameClient (async) / GameBridge (sync) / pytest plugin
python/tests/               # pytest suite
```

## Running the checks

All three must be green before a PR can merge; CI runs them on Linux, macOS, and Windows.

```bash
# 1. Python tests + coverage gate.
#    Use `coverage run -m pytest`, NOT `pytest --cov`: the pytest11 entry-point imports the
#    package at startup, before pytest-cov attaches, so --cov misses import-time statements.
coverage run -m pytest python/tests/
coverage report          # fails under 80% line/branch coverage

# 2. Lint gate (ruff). Rule set is intentionally the 0-error baseline: pyflakes (F) +
#    pycodestyle E4/E7/E9. Widening it is a separate PR with the autofix included.
ruff check

# 3. GUT unit tests for the GDScript plugin (needs a Godot 4 binary).
#    Cross-platform runner — this is what CI uses:
GODOT_BIN=/path/to/godot python addons/godot_cli_control/tests/run_gut.py
```

> Per project policy we don't use `pytest --cov`; the reasoning lives in the comment above
> `[tool.coverage.run]` in `pyproject.toml`.

## The contracts you must NOT silently break

These are the invariants that make the tool AI-friendly. If your change touches them, it must
keep them whole **and** update the docs that advertise them (see [Docs that travel with code](#docs-that-travel-with-code)).

1. **JSON envelope is the default.** Every command prints a single line of JSON on stdout:
   `{"ok": true, "result": ...}` or `{"ok": false, "error": {"code": <int>, "message": "..."}}`.
   No traceback ever reaches stdout (tracebacks go to stderr, for humans). `--text` / `--no-json`
   is a legacy bypass — it must never become the default.
2. **Error codes are three non-overlapping bands.** Server-side GDScript: positive `1xxx`
   (business) + `-32xxx` (JSON-RPC standard). Python client: `-1xxx`. A single `code` field is
   unambiguous. Before adding a code, check the error-code table in the addon `README.md` and
   the constants in `addons/godot_cli_control/bridge/error_codes.gd`.
3. **Exit codes are semantic.** `0` = success / boolean true / node exists / wait hit; `1` = RPC
   error (including `exists`/`visible` = false, `wait-node` timeout); `2` = connection / IO error
   or `run`/`daemon` preflight failure; `64` = usage error; `3` = `daemon stop --all` partial
   failure. `if godot-cli-control exists /root/Foo; then ...` must keep working.
4. **Shell is the canonical surface.** Every RPC has a CLI subcommand with positional args and
   JSON literals. The `def run(bridge):` script form is the fallback, only for "must hold one
   connection across many steps."
5. **Preflight beats a network round-trip.** A usage error (e.g. `combo` with no steps) must fail
   *before* connecting to the daemon, so an agent doesn't wait out a 30s connection retry to learn
   it mistyped an argument. See `RpcSpec.preflight`.
6. **Don't blow up the agent's context window.** `screenshot` writes to a file path — never base64
   on stdout. `tree` / `children` have a depth cap and a truncation signal. New RPCs that return
   large data need a trim strategy up front.
7. **localhost-only / blacklist safety net stays.** The bridge always binds `127.0.0.1`; release
   builds are unconditionally disabled; the method/property blacklist is the last line against RCE.
   Third-party projects extend it additively via the `godot_cli_control/method_blacklist_extra`
   ProjectSetting — never by loosening the built-in list.

## Adding a new RPC

Follow this exact order so the shell surface, the Python API, and the agent docs stay in lockstep:

1. Add an `async` method to `python/godot_cli_control/client.py` (`GameClient`).
2. Add the sync wrapper to `python/godot_cli_control/bridge.py` (`GameBridge`).
3. In `python/godot_cli_control/cli.py`: add the `RpcSpec` + handler + text formatter, plus a
   `preflight` and/or `exit_code_from` if the command needs them.
4. Add the RPC handler to `addons/godot_cli_control/.../low_level_api.gd` or
   `input_simulation_api.gd`.
5. Update the docs — see below.

## Docs that travel with code

`SKILL.md` is the only ground truth an AI agent sees. **If you change a CLI subcommand, an error
code, or a default, you must update the same change in:**

- `python/godot_cli_control/templates/skill/` (the multi-file skill — `SKILL.md` core +
  `references/*.md` — that `init` renders into a project's `.claude/` and `.codex/` skill dirs;
  put the detail in the matching reference file, keep the core lean)
- `addons/godot_cli_control/README.md` (error-code table, exit-code table, command table)

After editing the SKILL template, sanity-check that it still renders and stays in sync:

```bash
python -m pytest python/tests/test_skills_install.py -q
```

## Pull request workflow

- Branch from `main`. Keep PRs focused.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, …) — matching the existing history.
- Fill in the PR template checklist, especially the "SKILL.md updated?" box if you touched the
  CLI surface, error codes, or defaults.
- All CI checks (tests on 3 platforms, coverage ≥ 80%, ruff, GUT) must pass.
- Don't `--no-verify`, don't `xfail`/skip a test to make CI green, don't comment out failing code.
  If something is genuinely blocked, say so in the PR and open a tracking issue.

## Be respectful

Be kind and constructive in issues, PRs, and discussions. We follow the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/) —
harassment or abuse of any kind isn't welcome here.
