# Godot CLI Control

WebSocket bridge for headless / scripted control of Godot 4 scenes — click nodes,
read/write properties, simulate input, take screenshots, record movies — from a
Python or shell client.

> **Status**: alpha. On PyPI (`pipx install godot-cli-control`); Godot AssetLib
> submission pending ([#18](https://github.com/ClaymanTwinkle/godot-cli-control/issues/18)).
> Dogfooded in [`godot-2d-skeleton`](https://github.com/ClaymanTwinkle/godot-2d-skeleton).
> Current version tracked in [`CHANGELOG`](addons/godot_cli_control/CHANGELOG.md).

## Repository layout

```
godot-cli-control/
├── addons/godot_cli_control/   # Godot 4 plugin (drop into your project's addons/)
├── python/                     # Python client + CLI (pip-installable)
└── .github/workflows/          # release packaging
```

Each subdirectory has its own README:

- [`addons/godot_cli_control/README.md`](addons/godot_cli_control/README.md) — plugin install / RPC reference
- [`python/README.md`](python/README.md) — Python `GameClient` API + CLI usage

## One-shot install (recommended)

```bash
# 1. Install the Python CLI (it bundles the Godot plugin source)
pipx install godot-cli-control

# 2. From your Godot project root: copy plugin, patch project.godot, detect Godot binary
cd path/to/your_godot_project
godot-cli-control init

# 3. Start the daemon and try it
godot-cli-control daemon start
godot-cli-control tree 3
godot-cli-control screenshot /tmp/x.png
godot-cli-control daemon stop
```

`init` does the manual steps for you:
- copies `addons/godot_cli_control/` into your project,
- patches `project.godot` (`[autoload]` + `[editor_plugins]`) so you don't have to click through the Godot Editor,
- detects the Godot binary and writes `.cli_control/godot_bin` for the daemon,
- writes `.claude/skills/godot-cli-control/SKILL.md` and `.codex/skills/godot-cli-control/SKILL.md` so any AI agent (Claude Code / Codex) working in your Godot project can immediately learn this CLI. Pass `--no-skills` to skip; `--skills-only` to refresh only the skill files (e.g., after a CLI upgrade).

Want unreleased main? `pipx install "git+https://github.com/ClaymanTwinkle/godot-cli-control.git#subdirectory=python"`.

## Agent integration

When `godot-cli-control init` runs, two `SKILL.md` files are dropped under your Godot project root:

- `.claude/skills/godot-cli-control/SKILL.md` (Claude Code)
- `.codex/skills/godot-cli-control/SKILL.md` (Codex)

Both are rendered from the same template and pin the current CLI version + `--help` output, so an agent loaded into your project can immediately see the full command surface, the `GameClient` API, and the `def run(bridge)` script convention. After upgrading the CLI (`pipx upgrade godot-cli-control`), refresh both with:

```bash
godot-cli-control init --skills-only
```

If you have hand-edited a `SKILL.md` and want to keep your version, you have two options:

- `godot-cli-control init --no-skills` — skip skill writes entirely going forward
- `godot-cli-control init --skills-no-clobber` — keep existing files, only fill in missing ones (e.g., refresh just the half you accidentally deleted)

The two `--no-*` flags above are mutually exclusive with each other; `--skills-no-clobber` is orthogonal and may be combined with `--skills-only`.

> Optional: add `.claude/` and `.codex/` to your project's `.gitignore` if you don't want the SKILL.md files committed to source control. They are reproducible from the CLI any time via `godot-cli-control init --skills-only`.

## Manual install (advanced)

If you don't want to use `init`, copy the plugin manually and enable it from the editor — see the [plugin README](addons/godot_cli_control/README.md) for the long-form walkthrough. The legacy wrappers (`bin/run_cli_control.sh` / `.ps1`) are kept as compatibility shims but **deprecated since 0.1.6 and scheduled for removal in 0.3.0** — new code should call `godot-cli-control <subcommand>` directly.

## Roadmap

Tracked in [issues](https://github.com/ClaymanTwinkle/godot-cli-control/issues).
Remaining open items:

- AssetLib first submission ([#18](https://github.com/ClaymanTwinkle/godot-cli-control/issues/18))

## License

MIT — see [`LICENSE`](LICENSE).
