class_name DiagnosticsApi
extends Node
## push_error / push_warning 结构化捕获 + errors RPC（issue #103）
##
## 动机：「游戏静默吞错」类 bug 在 e2e 里无法断言 —— e2e 全绿因为没有任何
## 手段表达「本用例期间应零 push_error」。这里给出那条防线的服务端：
## Logger（Godot 4.5+）→ ring buffer → errors RPC 按 seq 游标增量查询。
##
## - 线程安全：_log_error 可能从任意线程进来，ring 操作全程持 Mutex；
## - 上下文友好（契约 6）：消息截断 + 默认 limit + truncated/dropped 信号；
## - 老引擎（< 4.5 无 Logger）：errors RPC 返回 1012，capture_logger.gd
##   靠 load() 动态加载隔离，不拖垮整个 addon 的编译。

const _CAPTURE_LOGGER_PATH := "res://addons/godot_cli_control/bridge/capture_logger.gd"
const _DEFAULT_LIMIT: int = 100
const _MAX_LIMIT: int = 1000
const _MAX_MESSAGE_LEN: int = 1024

# ring 容量。非 const：GUT 测试调小验证 overflow/dropped 路径。
var _ring_cap: int = 1000

var _logger: Object = null  # capture_logger 实例；老引擎恒 null
var _mutex := Mutex.new()
var _entries: Array[Dictionary] = []
var _next_seq: int = 1


# 用 _enter_tree / _exit_tree 对称管理（_ready 只在首次进树触发，摘除重挂
# 后 logger 会悬空）。ring 内容跨摘挂保留 —— 只有捕获通道随树挂载开关。
func _enter_tree() -> void:
	if not ClassDB.class_exists("Logger"):
		return  # 老引擎：handle_errors 走 1012
	var script: GDScript = load(_CAPTURE_LOGGER_PATH)
	_logger = script.new()
	_logger.sink = _record
	OS.add_logger(_logger)


func _exit_tree() -> void:
	if _logger != null:
		OS.remove_logger(_logger)
		_logger = null


## capture_logger 的 sink。可能在任意线程被调，除 Time 读数外全程持锁。
func _record(entry: Dictionary) -> void:
	var msg: String = entry.get("message", "") as String
	if msg.length() > _MAX_MESSAGE_LEN:
		entry["message"] = msg.substr(0, _MAX_MESSAGE_LEN) + "...[truncated]"
	entry["unix_time"] = Time.get_unix_time_from_system()
	entry["ticks_msec"] = Time.get_ticks_msec()
	_mutex.lock()
	entry["seq"] = _next_seq
	_next_seq += 1
	_entries.append(entry)
	if _entries.size() > _ring_cap:
		_entries.pop_front()
	_mutex.unlock()


## errors RPC：{"since": int>=0, "limit": int 0.._MAX_LIMIT} 都可选。
## 返回 {"errors": [...], "marker": int, "dropped": int, "truncated": bool}：
## - marker：游标。无截断时 = 全局最新 seq；截断时 = 本批最后一条的 seq
##   （下次 since=marker 继续翻页）。limit=0 是纯基线查询（拿 marker 不拿数据）。
## - dropped：since 之后、ring 已挤掉的条数（>0 说明错误风暴超过容量）。
func handle_errors(params: Dictionary) -> Dictionary:
	if _logger == null:
		return _err(
			CliControlErrorCodes.FEATURE_UNAVAILABLE,
			"errors capture requires Godot 4.5+ (Logger API); running %s"
				% Engine.get_version_info().get("string", "unknown"),
		)
	var since_or_err: Variant = _parse_non_negative_int(params, "since", 0)
	if since_or_err is Dictionary:
		return since_or_err as Dictionary
	var since: int = since_or_err as int
	var limit_or_err: Variant = _parse_non_negative_int(params, "limit", _DEFAULT_LIMIT)
	if limit_or_err is Dictionary:
		return limit_or_err as Dictionary
	var limit: int = limit_or_err as int
	if limit > _MAX_LIMIT:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"'limit' must be 0..%d, got %d" % [_MAX_LIMIT, limit],
		)

	_mutex.lock()
	var latest: int = _next_seq - 1
	var oldest_available: int = _next_seq - _entries.size()
	var matched: Array[Dictionary] = []
	for e: Dictionary in _entries:
		if int(e["seq"]) > since:
			matched.append(e.duplicate())
	_mutex.unlock()

	var dropped: int = maxi(0, oldest_available - since - 1)
	var truncated: bool = limit > 0 and matched.size() > limit
	var out: Array[Dictionary] = matched.slice(0, limit)
	var marker: int = latest
	if truncated:
		marker = int(out[-1]["seq"])
	return {"errors": out, "marker": marker, "dropped": dropped, "truncated": truncated}


func _parse_non_negative_int(params: Dictionary, key: String, default_value: int) -> Variant:
	var raw: Variant = params.get(key, default_value)
	# JSON 数字经解析可能是 float；接受「数值上是整数」的 float
	if raw is float and (raw as float) == floorf(raw as float):
		raw = int(raw as float)
	if not (raw is int):
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"'%s' must be a non-negative integer, got %s" % [key, str(raw)],
		)
	if (raw as int) < 0:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"'%s' must be a non-negative integer, got %d" % [key, raw],
		)
	return raw as int


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
