@tool
extends EditorPlugin

const GameBridgeScript := preload("res://addons/godot_cli_control/bridge/game_bridge.gd")
const LowLevelApiScript := preload("res://addons/godot_cli_control/bridge/low_level_api.gd")

const AUTOLOAD_NAME := "GameBridgeNode"
const AUTOLOAD_PATH := "res://addons/godot_cli_control/bridge/game_bridge.gd"


func _enter_tree() -> void:
	_ensure_project_settings()
	if not ProjectSettings.has_setting("autoload/" + AUTOLOAD_NAME):
		add_autoload_singleton(AUTOLOAD_NAME, AUTOLOAD_PATH)


func _exit_tree() -> void:
	if ProjectSettings.has_setting("autoload/" + AUTOLOAD_NAME):
		remove_autoload_singleton(AUTOLOAD_NAME)


func _ensure_project_settings() -> void:
	_register_setting(
		GameBridgeScript.SETTING_AUTO_ENABLE,
		true,
		TYPE_BOOL,
		"Auto-start CLI control server in debug builds (release builds always disabled)",
	)
	_register_setting(
		GameBridgeScript.SETTING_OUTBOUND_BUFFER_MB,
		GameBridgeScript.DEFAULT_OUTBOUND_BUFFER_MB,
		TYPE_INT,
		"WebSocket outbound buffer in MB (raise if pushing multi-frame screenshots/video). Min 1.",
	)
	_register_setting(
		LowLevelApiScript.SETTING_PROPERTY_BLACKLIST_EXTRA,
		PackedStringArray(),
		TYPE_PACKED_STRING_ARRAY,
		"Extra property names blocked by set_property (merged with built-in safety blacklist).",
	)
	_register_setting(
		LowLevelApiScript.SETTING_METHOD_BLACKLIST_EXTRA,
		PackedStringArray(),
		TYPE_PACKED_STRING_ARRAY,
		"Extra method names blocked by call_method (merged with built-in safety blacklist).",
	)


func _register_setting(key: String, default_value: Variant, type: int, hint: String) -> void:
	if ProjectSettings.has_setting(key):
		return
	ProjectSettings.set_setting(key, default_value)
	ProjectSettings.set_initial_value(key, default_value)
	ProjectSettings.add_property_info({
		"name": key,
		"type": type,
		"hint_string": hint,
	})
