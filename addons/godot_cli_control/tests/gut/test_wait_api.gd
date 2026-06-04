## GUT 单元测试：WaitApi handler 边界
##
## 从 test_low_level_api.gd 纯搬移（#108），对应 wait_api.gd 从 low_level_api.gd 的拆分。
## 跑法：在 GUT 已装到 res://addons/gut/ 的项目里执行
##   godot --headless -d -s res://addons/gut/gut_cmdln.gd \
##       -gdir=res://addons/godot_cli_control/tests/gut -gexit
extends GutTest

const WaitApiScript := preload("res://addons/godot_cli_control/bridge/wait_api.gd")
const LowLevelApiScript := preload("res://addons/godot_cli_control/bridge/low_level_api.gd")

var _api: Node
var _low: Node


func before_each() -> void:
	_low = LowLevelApiScript.new()
	_low.name = "LowLevelApi"
	add_child_autofree(_low)
	_api = WaitApiScript.new()
	_api.name = "WaitApi"
	add_child_autofree(_api)
	_api.setup(_low._read_property)


# ── issue #96：wait_frames ──

func test_wait_frames_advances_n_process_frames() -> void:
	var start: int = Engine.get_process_frames()
	var result: Dictionary = await _api.wait_frames_async({"frames": 3})
	assert_eq(result, {"success": true, "frames": 3})
	assert_gte(Engine.get_process_frames() - start, 3)


func test_wait_frames_physics_mode() -> void:
	var start: int = Engine.get_physics_frames()
	var result: Dictionary = await _api.wait_frames_async({"frames": 2, "physics": true})
	assert_eq(result, {"success": true, "frames": 2})
	assert_gte(Engine.get_physics_frames() - start, 2)


func test_wait_frames_rejects_bad_input() -> void:
	for bad: Variant in [null, 0, -1, 3601, "x"]:
		var result: Dictionary = await _api.wait_frames_async({"frames": bad})
		assert_has(result, "error", "frames=%s 应报错" % [bad])
		assert_eq(int(result.error.code), -32602)


# ── issue #96：wait_property ──

func test_wait_property_matches_immediately() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(5.0, 0.0)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "position", "value": [5.0, 0.0], "timeout": 1.0,
	})
	assert_true(result["matched"])
	assert_eq(result["value"], [5.0, 0.0])


func test_wait_property_catches_later_change() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.visible = false
	get_tree().create_timer(0.05).timeout.connect(func() -> void: node.visible = true)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "visible", "value": true, "timeout": 2.0,
	})
	assert_true(result["matched"])


func test_wait_property_gt_on_sub_path() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(0.0, 0.0)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: node.position = Vector2(600.0, 0.0))
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "position:x", "value": 500, "op": "gt", "timeout": 2.0,
	})
	assert_true(result["matched"])


func test_wait_property_timeout_reports_reason_and_last_value() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.visible = false
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "visible", "value": true, "timeout": 0.1,
	})
	assert_false(result["matched"])
	assert_eq(result["reason"], "timeout")
	assert_eq(result["value"], false)


func test_wait_property_node_not_found_reason() -> void:
	var result: Dictionary = await _api.wait_property_async({
		"path": "/root/__nope__", "property": "visible", "value": true, "timeout": 0.1,
	})
	assert_false(result["matched"])
	assert_eq(result["reason"], "node_not_found")


func test_wait_property_tolerance_eq() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(1.0001, 0.0)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "position:x", "value": 1.0,
		"tolerance": 0.001, "timeout": 0.5,
	})
	assert_true(result["matched"])


func test_wait_property_rejects_ordering_op_with_non_numeric_value() -> void:
	var result: Dictionary = await _api.wait_property_async({
		"path": "/root", "property": "name", "value": [1, 2], "op": "gt", "timeout": 0.1,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


# ── issue #96：wait_signal ──

func test_wait_signal_captures_emission_and_args() -> void:
	var emitter := Node.new()
	emitter.add_user_signal("e2e_sig", [{"name": "v", "type": TYPE_INT}])
	add_child_autofree(emitter)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: emitter.emit_signal("e2e_sig", 42))
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "e2e_sig", "timeout": 2.0,
	})
	assert_true(result["emitted"])
	assert_eq(result["args"], [{"value": 42}])


func test_wait_signal_zero_arg_signal() -> void:
	var emitter := Node.new()
	add_child_autofree(emitter)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: emitter.emit_signal("renamed"))
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "renamed", "timeout": 2.0,
	})
	assert_true(result["emitted"])
	assert_eq(result["args"], [])


func test_wait_signal_timeout_cleans_up_connection() -> void:
	var emitter := Node.new()
	emitter.add_user_signal("never_fires")
	add_child_autofree(emitter)
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "never_fires", "timeout": 0.1,
	})
	assert_false(result["emitted"])
	assert_eq(emitter.get_signal_connection_list("never_fires").size(), 0, "超时后连接必须清理")


func test_wait_signal_node_missing_is_1001() -> void:
	var result: Dictionary = await _api.wait_signal_async({
		"path": "/root/__nope__", "signal": "x", "timeout": 0.1,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)


func test_wait_signal_unknown_signal_is_1007() -> void:
	var emitter := Node.new()
	add_child_autofree(emitter)
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "__nope__", "timeout": 0.1,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1007)


# ── issue #96 review fix：timeout/tolerance 非数字服务端拒绝 ──

func test_wait_property_timeout_string_is_minus_32602() -> void:
	## wait_property 传 timeout="abc" 必须被拒绝（-32602），不能静默转 0.0。
	var result: Dictionary = await _api.wait_property_async({
		"path": "/root", "property": "name", "value": "root", "timeout": "abc",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602,
		"timeout='abc' 应报 -32602 INVALID_PARAMS，实际 code=%d" % [int(result.error.code)])


func test_wait_signal_timeout_string_is_minus_32602() -> void:
	## wait_signal 传 timeout="abc" 必须被拒绝（-32602），节点和信号均合法。
	var emitter := Node.new()
	emitter.add_user_signal("test_sig_for_timeout_check")
	add_child_autofree(emitter)
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "test_sig_for_timeout_check",
		"timeout": "abc",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602,
		"timeout='abc' 应报 -32602 INVALID_PARAMS，实际 code=%d" % [int(result.error.code)])


func test_wait_property_tolerance_string_is_minus_32602() -> void:
	## wait_property 传 tolerance="abc" 必须被拒绝（-32602），对齐 timeout 同款拒绝。
	## tolerance 是 float 参数；字符串不应被静默当 0.0 使用。
	var node := Node2D.new()
	add_child_autofree(node)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "visible", "value": true,
		"tolerance": "abc", "timeout": 0.5,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602,
		"tolerance='abc' 应报 -32602 INVALID_PARAMS，实际 code=%d" % [int(result.error.code)])


# ── I1 review fix：_read_property_fn 未 setup 时的守卫 ──

func test_wait_property_without_setup_returns_internal_error() -> void:
	## WaitApi 不调 setup() 直接调 wait_property_async 必须返回 -32603 error 信封，
	## 而不是抛 SCRIPT ERROR（裸 Callable().call() 崩溃），对齐 InputSimulationApi
	## 注入 Callable 的 is_valid() 防御约定。
	var unwired: WaitApi = WaitApiScript.new()
	add_child_autofree(unwired)
	var result: Dictionary = await unwired.wait_property_async({
		"path": "/root", "property": "name", "value": "root", "timeout": 0.1,
	})
	assert_has(result, "error", "未 setup 的 WaitApi 应返回 error 信封")
	assert_eq(int(result.error.code), -32603,
		"未 setup 应报 -32603 internal error，实际 code=%d" % [int(result.error.code)])


# ── issue #110：wait_signal reason 字段 ──

func test_wait_signal_timeout_has_reason_timeout() -> void:
	## 真超时 → {"emitted": false, "reason": "timeout"}
	var emitter := Node.new()
	emitter.add_user_signal("never_fires_2")
	add_child_autofree(emitter)
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "never_fires_2", "timeout": 0.1,
	})
	assert_false(result["emitted"])
	assert_has(result, "reason", "超时应有 reason 字段")
	assert_eq(result["reason"], "timeout")


func test_wait_signal_node_freed_has_reason_node_freed() -> void:
	## 等待中把目标节点 free() → {"emitted": false, "reason": "node_freed"}
	var emitter := Node.new()
	emitter.add_user_signal("fleeting_sig")
	add_child_autofree(emitter)
	# 50ms 后释放节点，timeout 留足余量确保先 free 后 timeout
	get_tree().create_timer(0.05).timeout.connect(func() -> void: emitter.free())
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "fleeting_sig", "timeout": 2.0,
	})
	assert_false(result["emitted"])
	assert_has(result, "reason", "节点释放应有 reason 字段")
	assert_eq(result["reason"], "node_freed")


func test_wait_signal_emitted_has_no_reason() -> void:
	## 命中信号 → {"emitted": true, "args": [...]} 不含 reason 字段（对齐 wait_property matched 时无 reason）
	var emitter := Node.new()
	emitter.add_user_signal("fires_soon", [{"name": "n", "type": TYPE_INT}])
	add_child_autofree(emitter)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: emitter.emit_signal("fires_soon", 7))
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "fires_soon", "timeout": 2.0,
	})
	assert_true(result["emitted"])
	assert_does_not_have(result, "reason", "命中信号时不应有 reason 字段")
