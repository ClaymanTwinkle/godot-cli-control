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

# 注入的 _read_property Callable（由 setup() 传入）
var _read_property_fn: Callable = Callable()


## 必须在任何 wait_property_async 调用之前执行；不调用则 _read_property_fn 无效。
func setup(read_property: Callable) -> void:
	_read_property_fn = read_property


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
	var last_value: Variant = null
	while true:
		var node: Node = get_tree().root.get_node_or_null(path)
		if node == null:
			reason = "node_not_found"
			last_value = null
		else:
			var read: Dictionary = _read_property_fn.call(node, property)
			if read.has("error"):
				reason = "property_not_found"
				last_value = null
			else:
				reason = "timeout"
				last_value = CliControlVariantCodec.encode_value(read["value"])
				if _wait_compare(last_value, expected, op, tolerance):
					return {
						"matched": true,
						"value": last_value,
						"waited": float(Time.get_ticks_msec() - start_ms) / 1000.0,
					}
		if float(Time.get_ticks_msec() - start_ms) / 1000.0 >= timeout:
			break
		await get_tree().process_frame
	return {"matched": false, "reason": reason, "value": last_value}


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
	return a == b  # String / bool / null / Dictionary（引擎深比较）


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


## issue #96：等信号发射（带超时）。竞态注意（SKILL.md pitfall）：必须先挂
## 等待再触发动作——信号在 connect 之前发射不会被捕获。
func wait_signal_async(params: Dictionary) -> Dictionary:
	var path: String = params.get("path", "") as String
	var node: Node = get_tree().root.get_node_or_null(path)
	if node == null:
		return _node_not_found(path)
	var signal_name: String = params.get("signal", "") as String
	if signal_name.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'signal' parameter")
	if not node.has_signal(signal_name):
		return _err(CliControlErrorCodes.SIGNAL_NOT_FOUND, "Signal not found: %s" % signal_name)
	var timeout_raw2: Variant = params.get("timeout", 5.0)
	if not (timeout_raw2 is int or timeout_raw2 is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'timeout' must be a number")
	var timeout: float = float(timeout_raw2)
	if timeout < 0.0 or timeout > _MAX_WAIT_SECONDS:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "timeout must be 0..%s" % _MAX_WAIT_SECONDS)
	var argc: int = 0
	for sig: Dictionary in node.get_signal_list():
		if sig["name"] == signal_name:
			argc = (sig["args"] as Array).size()
			break
	if argc > _SignalCapture.MAX_ARGS:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"signal '%s' has %d args (max %d supported)" % [signal_name, argc, _SignalCapture.MAX_ARGS],
		)
	var capture: _SignalCapture = _SignalCapture.new()
	var cb: Callable = capture.callable_for(argc)
	node.connect(signal_name, cb, CONNECT_ONE_SHOT)
	var start_ms: int = Time.get_ticks_msec()
	var reason: String = "timeout"
	while not capture.fired:
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
		return {"emitted": false, "reason": reason}
	var encoded_args: Array = []
	for arg: Variant in capture.args:
		encoded_args.append(CliControlVariantCodec.encode(arg))
	return {"emitted": true, "args": encoded_args}


func _node_not_found(path: String) -> Dictionary:
	return _err(CliControlErrorCodes.NODE_NOT_FOUND, "Node not found: %s" % path)


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
