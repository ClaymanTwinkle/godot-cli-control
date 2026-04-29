"""Godot CLI Control — WebSocket bridge for headless / scripted control of Godot scenes."""

from godot_cli_control.client import DEFAULT_PORT, GameClient

try:
    from godot_cli_control._version import __version__
except ImportError:
    __version__ = "0.0.0+unknown"

__all__ = ["GameClient", "DEFAULT_PORT", "__version__"]
