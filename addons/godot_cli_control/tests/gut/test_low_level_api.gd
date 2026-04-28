## GUT 单元测试：LowLevelApi handler 边界
##
## 跑法：在 GUT 已装到 res://addons/gut/ 的项目里执行
##   godot --headless -d -s res://addons/gut/gut_cmdln.gd \
##       -gdir=res://addons/godot_cli_control/tests/gut -gexit
extends GutTest

const LowLevelApiScript := preload("res://addons/godot_cli_control/bridge/low_level_api.gd")

var _api: Node
var _target: Node


func before_each() -> void:
	_api = LowLevelApiScript.new()
	_api.name = "LowLevelApi"
	add_child_autofree(_api)
	_target = Node.new()
	_target.name = "GutTestTarget"
	add_child_autofree(_target)


# ── handle_get_property ───────────────────────────────────────────

func test_get_property_returns_value() -> void:
	var result: Dictionary = _api.handle_get_property({
		"path": str(_target.get_path()),
		"property": "name",
	})
	assert_does_not_have(result, "error")
	assert_eq(str(result.get("value")), "GutTestTarget")


func test_get_property_nonexistent_returns_1002() -> void:
	var result: Dictionary = _api.handle_get_property({
		"path": str(_target.get_path()),
		"property": "no_such_property_xyz",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1002)


func test_get_property_missing_param_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_get_property({
		"path": str(_target.get_path()),
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_get_property_node_not_found_returns_1001() -> void:
	var result: Dictionary = _api.handle_get_property({
		"path": "/root/__definitely_does_not_exist__",
		"property": "name",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)


# ── handle_set_property blacklist ─────────────────────────────────

func test_set_property_script_blocked() -> void:
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "script",
		"value": null,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Blocked property")


func test_set_property_resource_blocked() -> void:
	# Resource 注入向量（绕过 script ban 的次级路径）也必须挡住
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "texture",
		"value": null,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


# ── handle_call_method blacklist ──────────────────────────────────

func test_call_method_queue_free_blocked() -> void:
	var result: Dictionary = _api.handle_call_method({
		"path": str(_target.get_path()),
		"method": "queue_free",
		"args": [],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_call_method_reflection_set_blocked() -> void:
	# `set` 是反射类入口，可绕过 PROPERTY_BLACKLIST，必须 ban
	var result: Dictionary = _api.handle_call_method({
		"path": str(_target.get_path()),
		"method": "set",
		"args": ["script", null],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_call_method_nonexistent_returns_1003() -> void:
	var result: Dictionary = _api.handle_call_method({
		"path": str(_target.get_path()),
		"method": "no_such_method_xyz",
		"args": [],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1003)


# ── handle_node_exists / handle_get_children ──────────────────────

func test_node_exists_true_for_real_path() -> void:
	var result: Dictionary = _api.handle_node_exists({
		"path": str(_target.get_path()),
	})
	assert_true(result.get("exists"))


func test_node_exists_false_for_missing_path() -> void:
	var result: Dictionary = _api.handle_node_exists({
		"path": "/root/__missing__",
	})
	assert_false(result.get("exists"))
