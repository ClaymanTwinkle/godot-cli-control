@tool
extends EditorPlugin

const GameBridgeScript := preload("res://addons/godot_cli_control/bridge/game_bridge.gd")

const AUTOLOAD_NAME := "GameBridgeNode"
const AUTOLOAD_PATH := "res://addons/godot_cli_control/bridge/game_bridge.gd"


func _enter_tree() -> void:
	_ensure_project_setting()
	if not ProjectSettings.has_setting("autoload/" + AUTOLOAD_NAME):
		add_autoload_singleton(AUTOLOAD_NAME, AUTOLOAD_PATH)


func _exit_tree() -> void:
	if ProjectSettings.has_setting("autoload/" + AUTOLOAD_NAME):
		remove_autoload_singleton(AUTOLOAD_NAME)


func _ensure_project_setting() -> void:
	var key := GameBridgeScript.SETTING_AUTO_ENABLE
	if ProjectSettings.has_setting(key):
		return
	ProjectSettings.set_setting(key, false)
	ProjectSettings.set_initial_value(key, false)
	ProjectSettings.add_property_info({
		"name": key,
		"type": TYPE_BOOL,
		"hint_string": "Auto-start CLI control server in debug builds (release builds always disabled)",
	})
