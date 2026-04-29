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


# #22：NodePath 子属性（"script:source_code"）不能绕过 top-level 黑名单
func test_set_property_nested_script_blocked() -> void:
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "script:source_code",
		"value": "extends Node\nfunc _init():\n  pass",
	})
	assert_has(result, "error", "script:xxx 必须被 top-level 黑名单挡住")
	assert_eq(int(result.error.code), -32602)


func test_set_property_nested_resource_blocked() -> void:
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "texture:resource_path",
		"value": "res://anything.png",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_set_property_non_blacklisted_nested_still_works() -> void:
	# 控制组：name:length 这类无害嵌套（不存在的子路径）应正常往下走
	# 不在黑名单里。Godot 自身可能拒绝赋值，但黑名单不应提前误挡。
	# 验证不会被 -32602 Blocked 挡，至少能通过校验阶段。
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "name",  # name 本身不在黑名单
		"value": "RenamedTarget",
	})
	# name 是合法可写属性
	assert_does_not_have(result, "error")


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


# ── _build_tree 节点总数上限（防 outbound buffer 超限） ───────────────

func test_build_tree_short_circuits_above_node_limit() -> void:
	# 不打满 5000+ 节点（慢且与场景耦合）：预填 counter 到上限，
	# 验证 _build_tree 递归一层后短路、返回的 entry 不含 children。
	var leaf: Node = Node.new()
	leaf.name = "Leaf"
	add_child_autofree(leaf)
	var counter: Array[int] = [LowLevelApiScript._BUILD_TREE_NODE_LIMIT]
	var entry: Dictionary = _api._build_tree(leaf, 5, 0, counter)
	# leaf 自身被计入 → counter 变成 LIMIT+1
	assert_eq(int(counter[0]), LowLevelApiScript._BUILD_TREE_NODE_LIMIT + 1)
	# 超 limit 后立刻 return，不下递归 children
	assert_does_not_have(entry, "children")


func test_build_tree_under_limit_includes_children() -> void:
	# 控制组：counter 远未到上限时正常带 children
	var parent: Node = Node.new()
	parent.name = "Parent"
	add_child_autofree(parent)
	var c1: Node = Node.new()
	c1.name = "C1"
	parent.add_child(c1)
	var counter: Array[int] = [0]
	var entry: Dictionary = _api._build_tree(parent, 5, 0, counter)
	assert_has(entry, "children")
	assert_eq((entry.children as Array).size(), 1)


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
