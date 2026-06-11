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

# ── issue #155：async_with_id spy 基础设施 ──
# 收集服务端发出的帧：armed 中间帧走 _armed_ids，终帧走 _final_by_id
var _armed_ids: Array = []
var _final_by_id: Dictionary = {}


func _spy_send_armed(id: String) -> void:
	_armed_ids.append(id)


func _spy_send_response(id: String, result: Dictionary) -> void:
	_final_by_id[id] = result


func _wire_spies() -> void:
	_armed_ids = []
	_final_by_id = {}
	_api.setup(_low._read_property, _spy_send_response, _spy_send_armed)


func before_each() -> void:
	_low = LowLevelApiScript.new()
	_low.name = "LowLevelApi"
	add_child_autofree(_low)
	_api = WaitApiScript.new()
	_api.name = "WaitApi"
	add_child_autofree(_api)
	_wire_spies()


# ── issue #96：wait_frames ──

func test_wait_frames_advances_n_process_frames() -> void:
	# 帧边界防御：前驱 async 测试可能结束在 process_frame 发射周期内，
	# 此时下一个 await 同帧立即解析，会让帧差测量少算 1（await-during-emission 语义）
	await get_tree().process_frame
	var start: int = Engine.get_process_frames()
	var result: Dictionary = await _api.wait_frames_async({"frames": 3})
	assert_eq(result, {"success": true, "frames": 3})
	assert_gte(Engine.get_process_frames() - start, 3)


func test_wait_frames_physics_mode() -> void:
	# 帧边界防御，同上——本用例测物理帧差，跨 physics_frame 边界才对齐
	await get_tree().physics_frame
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
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "e2e_sig", "timeout": 2.0}, "WS1")
	await get_tree().create_timer(0.5).timeout
	var result: Dictionary = _final_by_id["WS1"]
	assert_true(result["emitted"])
	assert_eq(result["args"], [{"value": 42}])


func test_wait_signal_zero_arg_signal() -> void:
	var emitter := Node.new()
	add_child_autofree(emitter)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: emitter.emit_signal("renamed"))
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "renamed", "timeout": 2.0}, "WS2")
	await get_tree().create_timer(0.5).timeout
	var result: Dictionary = _final_by_id["WS2"]
	assert_true(result["emitted"])
	assert_eq(result["args"], [])


func test_wait_signal_timeout_cleans_up_connection() -> void:
	var emitter := Node.new()
	emitter.add_user_signal("never_fires")
	add_child_autofree(emitter)
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "never_fires", "timeout": 0.1}, "WS3")
	await get_tree().create_timer(0.5).timeout
	var result: Dictionary = _final_by_id["WS3"]
	assert_false(result["emitted"])
	assert_eq(emitter.get_signal_connection_list("never_fires").size(), 0, "超时后连接必须清理")


func test_wait_signal_node_missing_is_1001() -> void:
	_api.wait_signal_async({"path": "/root/__nope__", "signal": "x", "timeout": 0.1}, "WS4")
	await get_tree().process_frame
	var result: Dictionary = _final_by_id["WS4"]
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)


func test_wait_signal_unknown_signal_is_1007() -> void:
	var emitter := Node.new()
	add_child_autofree(emitter)
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "__nope__", "timeout": 0.1}, "WS5")
	await get_tree().process_frame
	var result: Dictionary = _final_by_id["WS5"]
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
	_api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "test_sig_for_timeout_check",
		"timeout": "abc",
	}, "WS6")
	await get_tree().process_frame
	var result: Dictionary = _final_by_id["WS6"]
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
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "never_fires_2", "timeout": 0.1}, "WS7")
	await get_tree().create_timer(0.5).timeout
	var result: Dictionary = _final_by_id["WS7"]
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
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "fleeting_sig", "timeout": 2.0}, "WS8")
	await get_tree().create_timer(0.5).timeout
	var result: Dictionary = _final_by_id["WS8"]
	assert_false(result["emitted"])
	assert_has(result, "reason", "节点释放应有 reason 字段")
	assert_eq(result["reason"], "node_freed")


func test_wait_signal_emitted_has_no_reason() -> void:
	## 命中信号 → {"emitted": true, "args": [...]} 不含 reason 字段（对齐 wait_property matched 时无 reason）
	var emitter := Node.new()
	emitter.add_user_signal("fires_soon", [{"name": "n", "type": TYPE_INT}])
	add_child_autofree(emitter)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: emitter.emit_signal("fires_soon", 7))
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "fires_soon", "timeout": 2.0}, "WS9")
	await get_tree().create_timer(0.5).timeout
	var result: Dictionary = _final_by_id["WS9"]
	assert_true(result["emitted"])
	assert_does_not_have(result, "reason", "命中信号时不应有 reason 字段")


# ── issue #109：raw 直比免物化路径 ──

func test_wait_compare_raw_parity_matrix() -> void:
	## 核心等价性守卫：_wait_compare_raw(raw, …) 必须与
	## _wait_compare(encode_value(raw), …) 逐位一致。全类型 corpus ×
	## corpus 编码形 + 殊形 × 6 op × 3 tolerance 笛卡尔积，分歧数必须为 0。
	## 改 _wait_compare_raw / _encoded_eq_raw / encode_value / _deep_equal
	## 任意一处，这个矩阵就是回归网。
	var corpus: Array = [
		null, true, false, 42, 3.5, -0.0, "s", "", NAN, INF, -INF,
		&"sn", NodePath("/a/b"),
		Vector2(1.5, 2.0), Vector2i(1, 2), Vector3(1.0, 2.0, 3.0), Vector3i(1, 2, 3),
		Color(0.1, 0.2, 0.3, 1.0), Rect2(1.0, 2.0, 3.0, 4.0),
		Quaternion(0.0, 0.0, 0.0, 1.0), Transform2D.IDENTITY,
		[], [1, 2.0, "x"], [Vector2(1.0, 2.0), [3, [4]]], [NAN, INF],
		{}, {"hp": 10.0, "pos": Vector2(1.0, 2.0)}, {"tags": ["a", "b"], "nest": {"k": 1}},
		{1: "int-key", "s": 2}, {&"sn_key": 7},
		{"pf": PackedFloat32Array([1.5]), "pi": PackedInt32Array([1])},
		PackedByteArray([1, 2]), PackedInt32Array([1, 2, 3]), PackedInt64Array([9]),
		PackedFloat32Array([1.5, 2.5]), PackedFloat64Array([1.5, INF]),
		PackedStringArray(["a", "b"]),
		PackedVector2Array([Vector2(1.0, 2.0)]), PackedVector3Array([Vector3(1.0, 2.0, 3.0)]),
		PackedColorArray([Color(1.0, 0.0, 0.0, 1.0)]),
	]
	# >_MAX_ENCODE_DEPTH 嵌套：覆盖哨兵降级路径的镜像一致性
	corpus.append(_build_deep_array(CliControlVariantCodec._MAX_ENCODE_DEPTH + 6))
	var expecteds: Array = []
	for raw: Variant in corpus:
		expecteds.append(CliControlVariantCodec.encode_value(raw))
	# 数值近差（tolerance 边界两侧）+ int/float 互通边界（数组吃、字典不吃）
	# + 类型错配殊形
	expecteds.append_array([
		42.0005, 3.5005, [1, 2.0005, "x"], {"hp": 10.0005, "pos": [1.0, 2.0]},
		{"hp": 10.0}, [1.0, 2.0], "nan", "inf", 5, 42.0,
		[1.0, 2.0, "x"], {"hp": 10, "pos": [1.0, 2.0]},
		{"tags": ["a", "b"], "nest": {"k": 1.0}},
		{"pf": [1.5], "pi": [1.0]},
	])
	var checked: int = 0
	var mismatches: Array = []
	for raw: Variant in corpus:
		var encoded: Variant = CliControlVariantCodec.encode_value(raw)
		for expected: Variant in expecteds:
			for op: String in ["eq", "ne", "gt", "lt", "ge", "le"]:
				for tol: float in [0.0, 0.001, 0.5]:
					checked += 1
					var fast: bool = _api._wait_compare_raw(raw, expected, op, tol)
					var slow: bool = _api._wait_compare(encoded, expected, op, tol)
					if fast != slow:
						mismatches.append("raw=%s expected=%s op=%s tol=%s fast=%s slow=%s" % [
							raw, expected, op, tol, fast, slow,
						])
	assert_gt(checked, 30000, "矩阵规模异常缩水")
	assert_eq(mismatches, [], "raw 直比与编码后比较出现分歧")


func _build_deep_array(levels: int) -> Array:
	var root: Array = []
	var cur: Array = root
	for _i in levels:
		var nxt: Array = []
		cur.append(nxt)
		cur = nxt
	cur.append(1)
	return root


func test_wait_compare_raw_tolerance_in_array_not_in_dictionary() -> void:
	## 契约 pin（现状语义，免物化路径必须保持）：数组元素吃 tolerance 且
	## int/float 数值互通；Dictionary 值不吃 tolerance（_deep_equal 对
	## Dictionary 落引擎 ==，类型严格——int 与 float 在引擎深比较里**不**互通，
	## 实证 {"a": 1} == {"a": 1.0} → false）。
	assert_true(_api._wait_compare_raw([10.0005], [10.0], "eq", 0.001))
	assert_true(_api._wait_compare_raw([10], [10.0], "eq", 0.0))
	assert_false(_api._wait_compare_raw({"hp": 10.0005}, {"hp": 10.0}, "eq", 0.001))
	assert_false(_api._wait_compare_raw({"hp": 10}, {"hp": 10.0}, "eq", 0.0))
	assert_true(_api._wait_compare_raw({"hp": 10.0}, {"hp": 10.0}, "eq", 0.0))


func test_wait_compare_cross_type_is_false_not_abort() -> void:
	## #109 顺手修存量雷 pin：跨类型比较必须「干净地」返回 false——
	## 旧版裸 `a == b` 是运行期脚本错误（中止函数、抛 Nil、每帧刷错误日志、
	## 污染 #103 errors 缓冲）。assert_eq 严格比对 false（中止时会拿到 Nil，
	## 能与 false 区分开）。
	assert_eq(_api._wait_compare("str", 5, "eq", 0.0), false)
	assert_eq(_api._wait_compare(true, 1, "eq", 0.0), false)
	assert_eq(_api._wait_compare([1, 2], "x", "eq", 0.0), false)
	assert_eq(_api._wait_compare(["a", 1], [1, "a"], "eq", 0.0), false)
	assert_eq(_api._wait_compare_raw({"k": [1]}, {"k": "x"}, "eq", 0.0), false)
	assert_eq(_api._wait_compare_raw(PackedStringArray(["a"]), [1], "eq", 0.0), false)


func test_wait_property_dictionary_eq_end_to_end() -> void:
	## 容器属性走免物化路径后，端到端 matched=true 且返回 payload 仍是编码形
	## （Vector2 → [x, y]）。
	var s := GDScript.new()
	s.source_code = "extends Node2D\nvar stats: Dictionary = {\"hp\": 10.0, \"pos\": Vector2(1.0, 2.0)}\n"
	s.reload()
	var node: Node2D = s.new()
	add_child_autofree(node)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "stats",
		"value": {"hp": 10.0, "pos": [1.0, 2.0]}, "timeout": 1.0,
	})
	assert_true(result["matched"])
	assert_eq(result["value"], {"hp": 10.0, "pos": [1.0, 2.0]})


func test_wait_property_memo_survives_node_swap() -> void:
	## #109 memo 按实例 id：等待中把节点换成同名新实例（新 iid），
	## memo 必须自动失效、回全检路径并照常命中。
	var old := Node2D.new()
	old.name = "SwapTarget"
	old.position = Vector2(0.0, 0.0)
	add_child(old)  # 不 autofree：50ms 后手动 free 换新
	var parent: Node = old.get_parent()
	var path: String = str(old.get_path())
	get_tree().create_timer(0.05).timeout.connect(func() -> void:
		old.free()
		var fresh := Node2D.new()
		fresh.name = "SwapTarget"
		fresh.position = Vector2(7.0, 0.0)
		parent.add_child(fresh)
		autofree(fresh)
	)
	var result: Dictionary = await _api.wait_property_async({
		"path": path, "property": "position:x", "value": 7.0, "timeout": 2.0,
	})
	assert_true(result["matched"])


func test_wait_property_null_then_value_transition() -> void:
	## 裸读读到 null 时降级全检：属性真值为 null 期间不误判 property_not_found，
	## 变值后正常命中（覆盖 fast-read-null → 全检 fallback 分支）。
	var s := GDScript.new()
	s.source_code = "extends Node2D\nvar payload = null\n"
	s.reload()
	var node: Node2D = s.new()
	add_child_autofree(node)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: node.set("payload", 5))
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "payload", "value": 5, "timeout": 2.0,
	})
	assert_true(result["matched"])
	assert_eq(int(result["value"]), 5)


func test_wait_property_missing_property_reason() -> void:
	## 属性整段缺失：_has_property 的 `in` 快拒路径下 reason 仍是
	## property_not_found（#109 不改诊断语义）。
	var node := Node2D.new()
	add_child_autofree(node)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "no_such_prop", "value": 1, "timeout": 0.1,
	})
	assert_false(result["matched"])
	assert_eq(result["reason"], "property_not_found")
	assert_null(result["value"])


# ── issue #155：wait_signal arm_ack armed 帧 ──

func test_wait_signal_arm_ack_emits_armed_frame_then_final() -> void:
	## arm_ack=true：connect 后先发 armed 中间帧，emit 后发终帧
	var emitter := Node.new()
	emitter.name = "PingEmitter155"
	emitter.add_user_signal("ping")
	add_child_autofree(emitter)
	# async_with_id：不返回，回调发帧
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "ping",
		"timeout": 2.0, "arm_ack": true}, "REQ1")
	await get_tree().process_frame  # 让 connect + armed 帧发出
	assert_eq(_armed_ids, ["REQ1"], "arm_ack 应先发一条 armed 帧")
	assert_false(_final_by_id.has("REQ1"), "未 emit 时不应有终帧")
	emitter.emit_signal("ping")
	await get_tree().process_frame
	assert_true(_final_by_id.has("REQ1"), "emit 后应发终帧")
	assert_true(_final_by_id["REQ1"]["emitted"], "终帧 emitted=true")


func test_wait_signal_without_arm_ack_emits_no_armed_frame() -> void:
	## 不传 arm_ack：不发 armed 帧（零回归），信号命中时正常发终帧
	var emitter := Node.new()
	emitter.name = "PingEmitter155B"
	emitter.add_user_signal("ping")
	add_child_autofree(emitter)
	_api.wait_signal_async({"path": str(emitter.get_path()), "signal": "ping",
		"timeout": 2.0}, "REQ2")
	await get_tree().process_frame
	assert_eq(_armed_ids, [], "不传 arm_ack 不发 armed 帧（零回归）")
	emitter.emit_signal("ping")
	await get_tree().process_frame
	assert_true(_final_by_id.has("REQ2"), "emit 后应发终帧")
	assert_true(_final_by_id["REQ2"]["emitted"], "仍正常发终帧")


func test_wait_signal_arm_ack_node_missing_no_armed_frame() -> void:
	## arm_ack=true 但节点不存在：先发 error 终帧，不发 armed 帧
	_api.wait_signal_async({"path": "/root/NoSuch155", "signal": "x",
		"timeout": 1.0, "arm_ack": true}, "REQ3")
	await get_tree().process_frame
	assert_eq(_armed_ids, [], "节点不存在：armed 帧前就发 error，不发 armed")
	if not _final_by_id.has("REQ3"):
		fail_test("no final frame for REQ3")
		return
	assert_true(_final_by_id["REQ3"].has("error"), "终帧是 error")
	assert_eq(_final_by_id["REQ3"]["error"]["code"], CliControlErrorCodes.NODE_NOT_FOUND)
