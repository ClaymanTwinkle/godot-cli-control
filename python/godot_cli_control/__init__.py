"""Godot CLI Control — WebSocket bridge for headless / scripted control of Godot scenes."""

from godot_cli_control.client import DEFAULT_PORT, GameClient

__all__ = ["GameClient", "DEFAULT_PORT"]
__version__ = "0.1.0"
