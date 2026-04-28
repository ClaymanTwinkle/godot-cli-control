# godot-cli-control

WebSocket bridge for headless / scripted control of Godot 4 scenes — Python client.

## Install

```bash
pip install godot-cli-control
```

Requires Python ≥ 3.10. Companion Godot plugin must be installed and enabled in your Godot project (see [the plugin README](https://github.com/ClaymanTwinkle/godot-cli-control/blob/main/addons/godot_cli_control/README.md) for setup).

## Usage

```python
import asyncio
from godot_cli_control import GameClient

async def main():
    async with GameClient(port=9877) as client:
        # Inspect scene
        tree = await client.get_scene_tree(depth=3)
        print(tree)

        # Interact
        await client.click("/root/MyScene/Button")
        await client.action_press("jump")
        await client.wait_game_time(0.5)
        await client.action_release("jump")

        # Capture
        png_bytes = await client.screenshot()
        open("frame.png", "wb").write(png_bytes)

asyncio.run(main())
```

## CLI

```bash
python -m godot_cli_control tree 3
python -m godot_cli_control click /root/MyScene/Button
python -m godot_cli_control screenshot /tmp/frame.png
```

## Documentation

See the [Godot plugin README](https://github.com/ClaymanTwinkle/godot-cli-control/blob/main/addons/godot_cli_control/README.md) for the full RPC reference, activation modes, security model, and known limitations.
