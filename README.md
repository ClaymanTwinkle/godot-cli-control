# Godot CLI Control

WebSocket bridge for headless / scripted control of Godot 4 scenes — click nodes,
read/write properties, simulate input, take screenshots, record movies — from a
Python or shell client.

> **Status**: alpha (`0.1.0`). Dogfooded in
> [`godot-2d-skeleton`](https://github.com/ClaymanTwinkle/godot-2d-skeleton).
> Not yet on PyPI / AssetLib.

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

## Install (until published)

**Plugin** (Godot side):

```bash
git clone https://github.com/ClaymanTwinkle/godot-cli-control.git
cp -r godot-cli-control/addons/godot_cli_control your_project/addons/
# then enable in Godot Editor → Project Settings → Plugins
```

**Python client**:

```bash
pip install "git+https://github.com/ClaymanTwinkle/godot-cli-control.git#subdirectory=python"
```

Requires Python ≥ 3.10.

## Quick smoke test

```bash
# in your project root, after enabling the plugin:
./run_cli_control.sh start
./run_cli_control.sh tree 1
./run_cli_control.sh screenshot /tmp/x.png
./run_cli_control.sh stop
```

See [`addons/godot_cli_control/README.md`](addons/godot_cli_control/README.md) for the
full 5-minute walkthrough.

## Roadmap

Tracked in [issues](https://github.com/ClaymanTwinkle/godot-cli-control/issues).
First-pass items:

- GitHub Actions CI (Python matrix + Godot binary)
- Windows PowerShell wrapper
- GUT unit tests for `LowLevelApi` / `InputSimulationApi`
- `ProjectSettings`-driven blacklist extension
- `register_method()` extension API
- PyPI + AssetLib publish

## License

MIT — see [`LICENSE`](LICENSE).
