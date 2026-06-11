class_name WaitApi
extends Node
## Wait 原语 API：等节点出现、等游戏时间、等属性满足条件、等帧数、等信号
##
## 从 low_level_api.gd 纯搬移（#108），镜像 input_simulation_api.gd 拆分先例。
## 错误码常量来自 res://addons/godot_cli_control/bridge/error_codes.gd
## （class_name CliControlErrorCodes）。靠 Godot 全局 class 注册解析；若 GUT
## 测试跑前遇到 "Class 'CliControlErrorCodes' not found"，先 import 一次。

# wait_game_time_async 防呆上限：防止误传 1e9 之类的数值挂死 session
const _MAX_WAIT_SECONDS: float = 3600.0
# wait_frames 防呆上限：60fps 下 1 分钟。要等更久用 wait_game_time / wait_property。
const _MAX_WAIT_FRAMES: int = 3600
# wait_property 支持的比较操作符列表
const _WAIT_PROP_OPS: PackedStringArray = ["eq", "ne", "gt", "lt", "ge", "le"]
# encode_value 会递归重建的容器类型（#109：这些走免物化的结构化比较；
# 其余类型 encode 都是 O(1)，直接编码走旧路径）
const _CONTAINER_TYPES: PackedInt32Array = [
	TYPE_ARRAY, TYPE_DICTIONARY,
	TYPE_PACKED_BYTE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY,
	TYPE_PACKED_FLOAT32_ARRAY, TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_STRING_ARRAY,
	TYPE_PACKED_VECTOR2_ARRAY, TYPE_PACKED_VECTOR3_ARRAY, TYPE_PACKED_COLOR_ARRAY,
	TYPE_PACKED_VECTOR4_ARRAY,
]

# 注入的 _read_property Callable（由 setup() 传入）
var _read_property_fn: Callable = Callable()
# 注入：发终帧 Callable（async_with_id 用，issue #155）
var _send_response_callback: Callable = Callable()
# 注入：发 armed 中间帧 Callable（async_with_id 用，issue #155）
var _send_armed_callback: Callable = Callable()
# arm_ack 在途计时状态（#172 item1）：req_id -> _ArmState。client 在 trigger
# （on_armed）完成后发 wait_signal_start_timer，game_bridge 调 notify_start_timer
# 把对应 _ArmState.started 置真——arming 阶段据此结束、等信号 timeout 才开始计。
var _arm_states: Dictionary = {}


## 必须在任何 wait_property_async / wait_signal_async 调用之前执行。
## callbacks 是回调字典（issue #172 item3：从定长 setup(a, b, c) 改字典注入，
## 未来新增中间帧回调——如 wait_property / wait_node 也要 ack——只加 key、不改签名，
## 第三方 addon 升级无需同步改所有 setup 调用）。约定的 key：
##   "read_property" -> Callable(node, prop) -> Dictionary（wait_property 读属性）
##   "send_response" -> Callable(id, result) -> void（async_with_id 终帧，#155）
##   "send_armed"    -> Callable(id) -> void（armed 中间帧，#155）
## 缺失的 key 留空 Callable；用到时各自的 is_valid() 守卫兜底。
func setup(callbacks: Dictionary) -> void:
	_read_property_fn = callbacks.get("read_property", Callable())
	_send_response_callback = callbacks.get("send_response", Callable())
	_send_armed_callback = callbacks.get("send_armed", Callable())


## #172 item1：client 在 trigger（on_armed）完成后通过 wait_signal_start_timer 控制
## 消息调到这里，标记对应在途 wait_signal「trigger 已完成，可以开始计 timeout」。
## req_id 未知（已超时清理 / 非 arm_ack 路径）= no-op，不报错。
func notify_start_timer(req_id: String) -> void:
	if _arm_states.has(req_id):
		(_arm_states[req_id] as _ArmState).started = true


## #172 item1：arm_ack 在途计时状态。arming 阶段 started=false（不计 timeout）；
## wait_signal_start_timer 到达后由 notify_start_timer 置真，wait_signal_async 据此
## 结束 arming、开始计等信号 timeout。
class _ArmState:
	extends RefCounted

	var started: bool = false


## wait_signal 的参数捕获器（issue #96）。GDScript 无变参 Callable，
## 按信号声明 argc（get_signal_list）从 0..MAX_ARGS 选对应 cap 函数。
class _SignalCapture:
	extends RefCounted

	const MAX_ARGS: int = 8

	var fired: bool = false
	var args: Array = []

	func cap0() -> void: _hit([])
	func cap1(a1: Variant) -> void: _hit([a1])
	func cap2(a1: Variant, a2: Variant) -> void: _hit([a1, a2])
	func cap3(a1: Variant, a2: Variant, a3: Variant) -> void: _hit([a1, a2, a3])
	func cap4(a1: Variant, a2: Variant, a3: Variant, a4: Variant) -> void: _hit([a1, a2, a3, a4])
	func cap5(a1: Variant, a2: Variant, a3: Variant, a4: Variant, a5: Variant) -> void: _hit([a1, a2, a3, a4, a5])
	func cap6(a1: Variant, a2: Variant, a3: Variant, a4: Variant, a5: Variant, a6: Variant) -> void: _hit([a1, a2, a3, a4, a5, a6])
	func cap7(a1: Variant, a2: Variant, a3: Variant, a4: Variant, a5: Variant, a6: Variant, a7: Variant) -> void: _hit([a1, a2, a3, a4, a5, a6, a7])
	func cap8(a1: Variant, a2: Variant, a3: Variant, a4: Variant, a5: Variant, a6: Variant, a7: Variant, a8: Variant) -> void: _hit([a1, a2, a3, a4, a5, a6, a7, a8])

	func callable_for(argc: int) -> Callable:
		match argc:
			0: return cap0
			1: return cap1
			2: return cap2
			3: return cap3
			4: return cap4
			5: return cap5
			6: return cap6
			7: return cap7
			8: return cap8
		return Callable()

	func _hit(captured: Array) -> void:
		if fired:
			return
		fired = true
		args = captured


func wait_for_node_async(params: Dictionary) -> Dictionary:
	var path: String = params.get("path", "") as String
	var timeout: float = params.get("timeout", 5.0) as float
	var elapsed: float = 0.0
	var poll_interval: float = 0.1
	while elapsed < timeout:
		var node: Node = get_tree().root.get_node_or_null(path)
		if node != null:
			return {"found": true}
		await get_tree().create_timer(poll_interval).timeout
		elapsed += poll_interval
	return {"found": false}


func wait_game_time_async(params: Dictionary) -> Dictionary:
	var seconds: float = params.get("seconds", 0.0) as float
	if seconds < 0.0:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "seconds must be >= 0")
	if seconds > _MAX_WAIT_SECONDS:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "seconds must be <= %s" % _MAX_WAIT_SECONDS)
	if seconds == 0.0:
		return {"success": true}
	# create_timer 的 process_always 默认 true：tree paused 时计时器照常走
	# （e2e 依赖此语义在 paused 下用 wait-time 验证冻结）；它同时跟随
	# Engine.time_scale（time-scale 调大 → wait 语义不变、墙钟变快，#102）。
	# 别"优化"成显式 process_always=false——paused 下 wait-time 会永久挂死。
	await get_tree().create_timer(seconds).timeout
	return {"success": true}


## issue #96：逐帧轮询等属性满足条件。超时不报错——返回 matched=false +
## reason（timeout / node_not_found / property_not_found，按最后一次轮询状态）
## + value（最后一次读到的编码值），容忍节点/属性中途出现，typo 靠 reason 诊断。
func wait_property_async(params: Dictionary) -> Dictionary:
	var path: String = params.get("path", "") as String
	var property: String = params.get("property", "") as String
	if property.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'property' parameter")
	var op: String = params.get("op", "eq") as String
	if not op in _WAIT_PROP_OPS:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "op must be one of: %s" % ", ".join(_WAIT_PROP_OPS))
	var timeout_raw: Variant = params.get("timeout", 5.0)
	if not (timeout_raw is int or timeout_raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'timeout' must be a number")
	var timeout: float = float(timeout_raw)
	if timeout < 0.0 or timeout > _MAX_WAIT_SECONDS:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "timeout must be 0..%s" % _MAX_WAIT_SECONDS)
	var tolerance_raw: Variant = params.get("tolerance", 0.0)
	if not (tolerance_raw is int or tolerance_raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'tolerance' must be a number")
	var tolerance: float = float(tolerance_raw)
	if tolerance < 0.0:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "tolerance must be >= 0")
	var expected: Variant = params.get("value", null)
	if not (expected is int or expected is float) and not op in ["eq", "ne"]:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "op '%s' requires a numeric value; compound/string values only support eq/ne" % op)
	if not _read_property_fn.is_valid():
		return _err(-32603, "WaitApi not wired: read_property unavailable")
	var start_ms: int = Time.get_ticks_msec()
	var reason: String = "timeout"
	var last_raw: Variant = null
	var has_value: bool = false
	# #109 memo：已确认「该属性存在」的节点实例 id。同实例后续帧走裸读，
	# 跳过 _read_property 内 get_property_list 的逐帧整表分配 + 线性扫。
	# 实例 id 不复用——节点中途被释放/替换时 memo 自动失效、回全检路径。
	var verified_iid: int = 0
	var is_sub_path: bool = ":" in property
	var prop_np: NodePath = NodePath(property)  # 裸读预解析，避免逐帧构造
	while true:
		var node: Node = get_tree().root.get_node_or_null(path)
		if node == null:
			reason = "node_not_found"
			has_value = false
		else:
			var read: Dictionary
			if node.get_instance_id() == verified_iid:
				# 裸读与 low_level_api._read_property 的取值路径对齐（sub-path 走
				# get_indexed）。读到 null 时降级全检：null 无法区分「真 null 值」
				# 与「属性中途消失（set_script 等）」，降级保住 reason 诊断精度。
				var fast: Variant = node.get_indexed(prop_np) if is_sub_path else node.get(property)
				read = {"value": fast} if fast != null else _read_property_fn.call(node, property)
			else:
				read = _read_property_fn.call(node, property)
			if read.has("error"):
				reason = "property_not_found"
				has_value = false
				verified_iid = 0
			else:
				verified_iid = node.get_instance_id()
				reason = "timeout"
				last_raw = read["value"]
				has_value = true
				# #109：原始 Variant 直比（容器 miss 帧零分配、首差短路），
				# 命中才编码做返回 payload
				if _wait_compare_raw(last_raw, expected, op, tolerance):
					return {
						"matched": true,
						"value": CliControlVariantCodec.encode_value(last_raw),
						"waited": float(Time.get_ticks_msec() - start_ms) / 1000.0,
					}
		if float(Time.get_ticks_msec() - start_ms) / 1000.0 >= timeout:
			break
		await get_tree().process_frame
	# break 与最后一次读取同帧（中间无 await），此刻编码与旧版逐帧编码结果一致
	return {
		"matched": false,
		"reason": reason,
		"value": CliControlVariantCodec.encode_value(last_raw) if has_value else null,
	}


## #109 性能：与 `_wait_compare(CliControlVariantCodec.encode_value(actual_raw),
## expected, op, tolerance)` 结果恒等，但容器不物化整棵编码树——miss 帧零分配、
## 首差短路。等价性由 test_wait_api.gd 的 parity 矩阵测试钉死；改这里 / 改
## variant_codec.encode_value / 改 _deep_equal 任意一处都必须重跑矩阵。
func _wait_compare_raw(actual_raw: Variant, expected: Variant, op: String, tolerance: float) -> bool:
	if not typeof(actual_raw) in _CONTAINER_TYPES:
		# 非容器叶子 encode_value 是 O(1)（标量恒等 / 复合类型 → 定长小数组），原路走
		return _wait_compare(CliControlVariantCodec.encode_value(actual_raw), expected, op, tolerance)
	# 容器编码后仍是 Array/Dictionary（非数值）：旧路径下 ordering op 恒 false，
	# eq/ne 走 _deep_equal——此处免物化镜像
	var equal: bool = _encoded_eq_raw(actual_raw, expected, tolerance, false, 0)
	if op == "eq":
		return equal
	if op == "ne":
		return not equal
	return false


## `_deep_equal(encode_value(raw, depth), expected, tolerance)`（strict=false）或
## `encode_value(raw, depth) == expected`（strict=true，引擎深比较）的免物化镜像。
## strict 的来源：_deep_equal 对 Dictionary 对落引擎 `==`（精确、无 tolerance），
## 所以进入 Dictionary 后整棵子树切 strict——与「编码后真比较」行为逐位一致。
func _encoded_eq_raw(raw: Variant, expected: Variant, tolerance: float, strict: bool, depth: int) -> bool:
	if depth > CliControlVariantCodec._MAX_ENCODE_DEPTH:
		# 镜像 encode_value 超深降级哨兵；哨兵是 String，对非 String expected 恒 false
		# （typeof 守卫：GDScript 跨类型 == 是运行期脚本错误，见 _safe_eq docstring）
		return expected is String and (expected as String) == CliControlVariantCodec._DEPTH_SENTINEL
	match typeof(raw):
		TYPE_ARRAY:
			if not expected is Array:
				return false  # 编码后是 Array，与非 Array 比较两种模式下都为 false
			var raw_arr: Array = raw as Array
			var exp_arr: Array = expected as Array
			if raw_arr.size() != exp_arr.size():
				return false
			for i in raw_arr.size():
				if not _encoded_eq_raw(raw_arr[i], exp_arr[i], tolerance, strict, depth + 1):
					return false
			return true
		TYPE_DICTIONARY:
			if not expected is Dictionary:
				return false
			var raw_dict: Dictionary = raw as Dictionary
			var exp_dict: Dictionary = expected as Dictionary
			for key: Variant in raw_dict:
				if not (key is String or key is StringName):
					# 异类键经 str() 可能撞键折叠（后写覆盖），结构化镜像不划算：
					# 兜底物化这棵子树走真编码（罕见路径，正确性优先）。
					# String/StringName 键在 Dictionary 里本就同键（引擎语义），无碰撞。
					return CliControlVariantCodec.encode_value(raw, depth) == expected
			if raw_dict.size() != exp_dict.size():
				return false
			for key: Variant in raw_dict:
				var skey: String = str(key)
				if not exp_dict.has(skey):
					return false
				# Dictionary 内部恒 strict（见 docstring）
				if not _encoded_eq_raw(raw_dict[key], exp_dict[skey], tolerance, true, depth + 1):
					return false
			return true
		TYPE_PACKED_BYTE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY, TYPE_PACKED_STRING_ARRAY:
			# encode_value 对这 4 种是 Array(v) 直转：元素（int/String）不经编码、
			# 不耗 depth 层级——元素按 depth=0 比较（恒等、永不触哨兵），镜像直转语义
			return _packed_elems_eq(raw, expected, tolerance, strict, 0)
		TYPE_PACKED_FLOAT32_ARRAY, TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_VECTOR2_ARRAY, TYPE_PACKED_VECTOR3_ARRAY, TYPE_PACKED_COLOR_ARRAY, TYPE_PACKED_VECTOR4_ARRAY:
			# encode_value 对这 6 种逐元素 encode_value(item, depth + 1)，镜像之
			return _packed_elems_eq(raw, expected, tolerance, strict, depth + 1)
		_:
			# 非容器叶子：O(1) 编码后按模式比较（含 float 的 nan/inf → 字符串降级）
			var enc: Variant = CliControlVariantCodec.encode_value(raw, depth)
			if strict:
				# 引擎内部深比较是「类型严格」的：int 与 float 不互通
				# （实证：{"a": 1} == {"a": 1.0} → false）。typeof 守卫同时挡掉
				# GDScript 跨类型 == 的运行期脚本错误。
				return typeof(enc) == typeof(expected) and enc == expected
			return _deep_equal(enc, expected, tolerance)


func _packed_elems_eq(raw: Variant, expected: Variant, tolerance: float, strict: bool, elem_depth: int) -> bool:
	if not expected is Array:
		return false
	var exp_arr: Array = expected as Array
	if raw.size() != exp_arr.size():
		return false
	for i in exp_arr.size():
		if not _encoded_eq_raw(raw[i], exp_arr[i], tolerance, strict, elem_depth):
			return false
	return true


## 编码后值比较。数值走 6 op（eq/ne 带 tolerance）；其余类型仅 eq/ne 深比较
## （数组逐元素，数值元素同样吃 tolerance）。ordering op + 非数值 actual →
## false（继续轮询直到 timeout，reason 诊断）。
func _wait_compare(actual: Variant, expected: Variant, op: String, tolerance: float) -> bool:
	if (actual is int or actual is float) and (expected is int or expected is float):
		var a: float = float(actual)
		var b: float = float(expected)
		match op:
			"eq": return absf(a - b) <= tolerance
			"ne": return absf(a - b) > tolerance
			"gt": return a > b
			"lt": return a < b
			"ge": return a >= b
			"le": return a <= b
		return false
	var equal: bool = _deep_equal(actual, expected, tolerance)
	if op == "eq":
		return equal
	if op == "ne":
		return not equal
	return false


func _deep_equal(a: Variant, b: Variant, tolerance: float) -> bool:
	if (a is int or a is float) and (b is int or b is float):
		return absf(float(a) - float(b)) <= tolerance
	if a is Array and b is Array:
		var aa: Array = a as Array
		var ba: Array = b as Array
		if aa.size() != ba.size():
			return false
		for i in aa.size():
			if not _deep_equal(aa[i], ba[i], tolerance):
				return false
		return true
	return _safe_eq(a, b)  # String / bool / null / Dictionary（引擎深比较）


## #109 顺手修存量雷：GDScript 层跨类型 `==`（如 Array == String、bool == int）
## 是**运行期脚本错误**——打印错误、中止所在函数、向上抛 Nil，不是静默 false。
## 旧版 _deep_equal 末行裸 `a == b` 在 expected 与属性类型不符时（agent typo
## 常态）每帧刷一条脚本错误、还会污染 #103 errors 环形缓冲，结果只是靠
## Nil→false 的二次错误侥幸正确。合法比较只有三类：同 typeof / int-float
## 数值对 / 任一侧 null（恒 false）——其余直接判 false，结果不变、零错误输出。
func _safe_eq(a: Variant, b: Variant) -> bool:
	var ta: int = typeof(a)
	var tb: int = typeof(b)
	if ta == tb or ((ta == TYPE_INT or ta == TYPE_FLOAT) and (tb == TYPE_INT or tb == TYPE_FLOAT)):
		return a == b
	return false  # 含 null vs 非 null：引擎语义本就恒 false，这里短路掉


## issue #96：等 N 帧（确定性帧推进）。physics=true 等 physics_frame。
func wait_frames_async(params: Dictionary) -> Dictionary:
	var frames_raw: Variant = params.get("frames", null)
	if not (frames_raw is int or frames_raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'frames' must be an integer")
	var frames: int = int(frames_raw)
	if frames < 1 or frames > _MAX_WAIT_FRAMES:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "frames must be 1..%d (got %d)" % [_MAX_WAIT_FRAMES, frames])
	var physics: bool = bool(params.get("physics", false))
	for _i in frames:
		if physics:
			await get_tree().physics_frame
		else:
			await get_tree().process_frame
	return {"success": true, "frames": frames}


## issue #96 / #155：等信号发射（带超时）。async_with_id 路径：不返回值，
## 通过 _send_response_callback 回终帧，通过 _send_armed_callback 回 armed 中间帧。
## arm_ack=true（issue #155）：connect 完成后先发 {id, armed:true} 进度帧，
## client 据此在同连接发 trigger 子命令，再等终帧——竞态从协议层根除。
func wait_signal_async(params: Dictionary, id: String) -> void:
	var path: String = params.get("path", "") as String
	var node: Node = get_tree().root.get_node_or_null(path)
	if node == null:
		_send_response_callback.call(id, _node_not_found(path))
		return
	var signal_name: String = params.get("signal", "") as String
	if signal_name.is_empty():
		_send_response_callback.call(id, _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'signal' parameter"))
		return
	if not node.has_signal(signal_name):
		_send_response_callback.call(id, _err(CliControlErrorCodes.SIGNAL_NOT_FOUND, "Signal not found: %s" % signal_name))
		return
	var timeout_raw2: Variant = params.get("timeout", 5.0)
	if not (timeout_raw2 is int or timeout_raw2 is float):
		_send_response_callback.call(id, _err(CliControlErrorCodes.INVALID_PARAMS, "'timeout' must be a number"))
		return
	var timeout: float = float(timeout_raw2)
	if timeout < 0.0 or timeout > _MAX_WAIT_SECONDS:
		_send_response_callback.call(id, _err(CliControlErrorCodes.INVALID_PARAMS, "timeout must be 0..%s" % _MAX_WAIT_SECONDS))
		return
	var argc: int = 0
	for sig: Dictionary in node.get_signal_list():
		if sig["name"] == signal_name:
			argc = (sig["args"] as Array).size()
			break
	if argc > _SignalCapture.MAX_ARGS:
		_send_response_callback.call(id, _err(CliControlErrorCodes.INVALID_PARAMS,
			"signal '%s' has %d args (max %d supported)" % [signal_name, argc, _SignalCapture.MAX_ARGS]))
		return
	var capture: _SignalCapture = _SignalCapture.new()
	var cb: Callable = capture.callable_for(argc)
	node.connect(signal_name, cb, CONNECT_ONE_SHOT)
	var reason: String = "timeout"
	# arm 完成同步点（issue #155）：connect 后才发 armed 帧，client 收到后再触发动作，
	# 保证「先挂再触发」——竞态从协议层根除。
	# #172 item1：发 armed 后进入 arming 阶段——在 client 发回 wait_signal_start_timer
	# （trigger 完成）之前不计 timeout，避免 trigger 执行时间（往返 + 游戏内 duration）
	# 吃掉等信号预算。arming 期间仍响应 capture.fired（trigger 中途已发信号）与节点释放。
	# _MAX_WAIT_SECONDS 仅作防泄漏兜底——同版本 client 必在 trigger 后发 start_timer，
	# arming 实际只持续 trigger 时长（秒级），兜底永不触及。
	if bool(params.get("arm_ack", false)) and _send_armed_callback.is_valid():
		var arm_state: _ArmState = _ArmState.new()
		_arm_states[id] = arm_state
		_send_armed_callback.call(id)
		var arm_started_ms: int = Time.get_ticks_msec()
		while not arm_state.started and not capture.fired:
			if not is_instance_valid(node):
				reason = "node_freed"
				break
			if float(Time.get_ticks_msec() - arm_started_ms) / 1000.0 >= _MAX_WAIT_SECONDS:
				break
			await get_tree().process_frame
		_arm_states.erase(id)
	# arming 结束后才开始计等信号 timeout（非 arm_ack 路径直接从这里起算，零回归）
	var start_ms: int = Time.get_ticks_msec()
	while not capture.fired and reason != "node_freed":
		if float(Time.get_ticks_msec() - start_ms) / 1000.0 >= timeout:
			break
		await get_tree().process_frame
		if not is_instance_valid(node):
			reason = "node_freed"
			break  # 节点被释放：连接随之失效，按未命中处理
	if not capture.fired:
		# one-shot 未触发时连接仍挂着，必须显式清理（防悬挂 Callable 泄漏）
		if is_instance_valid(node) and node.is_connected(signal_name, cb):
			node.disconnect(signal_name, cb)
		_send_response_callback.call(id, {"emitted": false, "reason": reason})
		return
	var encoded_args: Array = []
	for arg: Variant in capture.args:
		encoded_args.append(CliControlVariantCodec.encode(arg))
	_send_response_callback.call(id, {"emitted": true, "args": encoded_args})


func _node_not_found(path: String) -> Dictionary:
	return _err(CliControlErrorCodes.NODE_NOT_FOUND, "Node not found: %s" % path)


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
