class_name GameBridge
extends Node
## WebSocket 服务器 + JSON-RPC 消息路由

const DEFAULT_PORT: int = 9877
const SETTING_AUTO_ENABLE: String = "godot_cli_control/auto_enable_in_debug"
const SETTING_OUTBOUND_BUFFER_MB: String = "godot_cli_control/outbound_buffer_mb"
const DEFAULT_OUTBOUND_BUFFER_MB: int = 10

var _tcp_server: TCPServer = TCPServer.new()
var _active_peer: WebSocketPeer = null
var _active_stream: StreamPeerTCP = null
var _port: int = DEFAULT_PORT
var _idle_timeout_secs: int = 0
var _last_activity_ms: int = 0
var _outbound_buffer_size: int = DEFAULT_OUTBOUND_BUFFER_MB * 1024 * 1024
var _low_level_api: LowLevelApi = null
var _input_sim_api: InputSimulationApi = null
# 方法注册表：{method_name: {"callable": Callable, "kind": "sync"|"async"|"async_with_id"}}
# - sync: handler(params) -> Dictionary，dispatcher 立即 send response
# - async: handler(params) -> await Dictionary，dispatcher await 后 send response
# - async_with_id: handler(params, request_id) -> void，handler 自行通过 callback 回响
var _methods: Dictionary = {}


func _ready() -> void:
	if not _should_activate():
		print(
			(
				"[Godot CLI Control] inactive — pass --cli-control, set GODOT_CLI_CONTROL=1, or enable %s in Project Settings (debug build only)"
				% SETTING_AUTO_ENABLE
			)
		)
		queue_free()
		return
	# 即使 SceneTree 暂停也要继续运行
	process_mode = Node.PROCESS_MODE_ALWAYS
	# 创建子 API 节点
	_low_level_api = LowLevelApi.new()
	_low_level_api.name = "LowLevelApi"
	add_child(_low_level_api)
	_input_sim_api = InputSimulationApi.new()
	_input_sim_api.name = "InputSimulationApi"
	add_child(_input_sim_api)
	_input_sim_api.setup(_on_async_response)
	# 构建统一方法注册表
	_register_methods()
	# 缓存 outbound buffer 大小（ProjectSettings 可覆盖默认 10MB，至少 1MB）
	var mb: int = int(
		ProjectSettings.get_setting(SETTING_OUTBOUND_BUFFER_MB, DEFAULT_OUTBOUND_BUFFER_MB)
	)
	_outbound_buffer_size = max(1, mb) * 1024 * 1024
	# 启动 TCP 服务器
	_port = _parse_port_from_args()
	# 安全：显式绑 127.0.0.1，避免 Godot TCPServer 默认 "*" 暴露到 LAN
	var err: Error = _tcp_server.listen(_port, "127.0.0.1")
	if err != OK:
		# push_error 在 headless 下只进 Godot 内部 log，不上 stderr。daemon
		# subprocess 看不到 root cause，超时 30s 后只能报 "GameBridge not ready"。
		# printerr 直接写 stderr，subprocess 默认透传 → 用户立刻看到端口冲突。
		var msg: String = "GameBridge: Failed to listen on port %d: %s" % [_port, error_string(err)]
		push_error(msg)
		printerr(msg)
		return
	print("GameBridge: Listening on ws://127.0.0.1:%d" % _port)
	_idle_timeout_secs = _parse_idle_timeout_from_args()
	_last_activity_ms = Time.get_ticks_msec()
	if _idle_timeout_secs > 0:
		var t: Timer = Timer.new()
		t.wait_time = 1.0
		t.autostart = true
		t.process_mode = Node.PROCESS_MODE_ALWAYS
		t.timeout.connect(_check_idle)
		add_child(t)
		print("GameBridge: idle-timeout %ds enabled" % _idle_timeout_secs)


func _process(_delta: float) -> void:
	if not _tcp_server.is_listening():
		return
	_accept_new_connections()
	_poll_active_peer()


func _accept_new_connections() -> void:
	if not _tcp_server.is_connection_available():
		return
	if _active_peer != null and _active_peer.get_ready_state() != WebSocketPeer.STATE_CLOSED:
		# 拒绝新连接（单客户端模式）
		var rejected: StreamPeerTCP = _tcp_server.take_connection()
		rejected.disconnect_from_host()
		return
	_active_stream = _tcp_server.take_connection()
	_active_peer = WebSocketPeer.new()
	_active_peer.outbound_buffer_size = _outbound_buffer_size
	var err: Error = _active_peer.accept_stream(_active_stream)
	if err != OK:
		push_error("GameBridge: WebSocket handshake failed: %s" % error_string(err))
		_active_peer = null
		_active_stream = null
		return
	print("GameBridge: Client connected")


func _poll_active_peer() -> void:
	if _active_peer == null:
		return
	_active_peer.poll()
	var state: WebSocketPeer.State = _active_peer.get_ready_state()
	if state == WebSocketPeer.STATE_CLOSED:
		print("GameBridge: Client disconnected")
		_input_sim_api.release_all()
		_active_peer = null
		_active_stream = null
		return
	if state != WebSocketPeer.STATE_OPEN:
		return
	while _active_peer.get_available_packet_count() > 0:
		var packet: PackedByteArray = _active_peer.get_packet()
		var message: String = packet.get_string_from_utf8()
		_handle_message(message)


func _register_methods() -> void:
	# 低层 API（同步）
	_methods["click"] = {"callable": _low_level_api.handle_click, "kind": "sync"}
	_methods["get_property"] = {"callable": _low_level_api.handle_get_property, "kind": "sync"}
	_methods["set_property"] = {"callable": _low_level_api.handle_set_property, "kind": "sync"}
	_methods["call_method"] = {"callable": _low_level_api.handle_call_method, "kind": "sync"}
	_methods["get_text"] = {"callable": _low_level_api.handle_get_text, "kind": "sync"}
	_methods["node_exists"] = {"callable": _low_level_api.handle_node_exists, "kind": "sync"}
	_methods["is_visible"] = {"callable": _low_level_api.handle_is_visible, "kind": "sync"}
	_methods["get_children"] = {"callable": _low_level_api.handle_get_children, "kind": "sync"}
	_methods["get_scene_tree"] = {"callable": _low_level_api.handle_get_scene_tree, "kind": "sync"}
	# 低层 API（异步）
	_methods["wait_for_node"] = {"callable": _low_level_api.wait_for_node_async, "kind": "async"}
	_methods["wait_game_time"] = {"callable": _low_level_api.wait_game_time_async, "kind": "async"}
	_methods["screenshot"] = {"callable": _wrap_screenshot, "kind": "async"}
	# 输入模拟（同步）
	_methods["input_action_press"] = {
		"callable": _input_sim_api.handle_action_press, "kind": "sync"
	}
	_methods["input_action_release"] = {
		"callable": _input_sim_api.handle_action_release, "kind": "sync"
	}
	_methods["input_action_tap"] = {"callable": _input_sim_api.handle_action_tap, "kind": "sync"}
	_methods["input_get_pressed"] = {"callable": _input_sim_api.handle_get_pressed, "kind": "sync"}
	_methods["list_input_actions"] = {
		"callable": _input_sim_api.handle_list_input_actions, "kind": "sync"
	}
	_methods["input_hold"] = {"callable": _input_sim_api.handle_hold, "kind": "sync"}
	_methods["input_release_all"] = {"callable": _input_sim_api.handle_release_all, "kind": "sync"}
	_methods["input_combo_cancel"] = {
		"callable": _input_sim_api.handle_combo_cancel, "kind": "sync"
	}
	# 输入模拟（async_with_id：handler 自行通过 _on_async_response 回响）
	_methods["input_combo"] = {"callable": _input_sim_api.handle_combo, "kind": "async_with_id"}


# screenshot wrapper：take_screenshot_async 不接 params，统一签名为 (params) -> Dictionary
func _wrap_screenshot(_params: Dictionary) -> Dictionary:
	return await _low_level_api.take_screenshot_async()


func _handle_message(raw: String) -> void:
	_last_activity_ms = Time.get_ticks_msec()
	var parsed: Variant = JSON.parse_string(raw)
	if parsed == null or not parsed is Dictionary:
		_send_error("", -32600, "Invalid JSON")
		return
	var msg: Dictionary = parsed as Dictionary
	# `id` 缺失 → 空串（client 用空串当 fire-and-forget）；非 String 视为协议错。
	# 不能直接 `as String` 强转，整数/null 失败赋空串后客户端拿不到自己的 id。
	var id_raw: Variant = msg.get("id", "")
	if not id_raw is String:
		_send_error("", -32600, "id must be string")
		return
	var id: String = id_raw as String
	var method_raw: Variant = msg.get("method", "")
	if not method_raw is String:
		_send_error(id, -32600, "method must be string")
		return
	var method: String = method_raw as String
	if method.is_empty():
		_send_error(id, -32600, "Missing method")
		return
	# params 缺失 → 空 dict（合法）；存在但非 Dictionary → 协议错。
	# 不在这里挡，handler 内 `params.get` 会在 null 上崩，连接进入「不发响应」死锁，
	# 客户端 await 挂死到 30s timeout（参见 client.request）。
	var params_raw: Variant = msg.get("params", {})
	if not params_raw is Dictionary:
		_send_error(id, -32600, "params must be object")
		return
	var params: Dictionary = params_raw as Dictionary
	if not _methods.has(method):
		_send_error(id, -32601, "Unknown method: %s" % method)
		return
	var entry: Dictionary = _methods[method] as Dictionary
	var handler: Callable = entry["callable"] as Callable
	var kind: String = entry["kind"] as String
	match kind:
		"sync":
			var result: Dictionary = handler.call(params)
			_dispatch_result(id, result)
		"async":
			_run_async(id, handler, params)
		"async_with_id":
			handler.call(params, id)


func _run_async(id: String, handler: Callable, params: Dictionary) -> void:
	# 不能直接 `as Dictionary` —— handler 返回 null 或非 dict 会强转成 null，
	# 后续 `result.has("error")` 在 null 上引擎错误，**响应永远不会发出**，
	# 客户端 await 挂死到 timeout。先收原 Variant 自己 type-check。
	var raw: Variant = await handler.call(params)
	if not raw is Dictionary:
		_send_error(id, -32603, "internal: async handler returned non-dict")
		return
	_dispatch_result(id, raw as Dictionary)


func _dispatch_result(id: String, result: Dictionary) -> void:
	if result.has("error"):
		var err: Dictionary = result["error"] as Dictionary
		_send_error(id, err["code"] as int, err["message"] as String)
	else:
		_send_response(id, result)


func _on_async_response(id: String, result: Dictionary) -> void:
	_dispatch_result(id, result)


func _send_response(id: String, result: Dictionary) -> void:
	var response: Dictionary = {"id": id, "result": result}
	_send_json(response)


func _send_error(id: String, code: int, message: String) -> void:
	var response: Dictionary = {"id": id, "error": {"code": code, "message": message}}
	_send_json(response)


func _send_json(data: Dictionary) -> void:
	if _active_peer == null or _active_peer.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	var json_str: String = JSON.stringify(data)
	_active_peer.send_text(json_str)


func _should_activate() -> bool:
	# 显式禁用：GODOT_CLI_CONTROL=0 是最高优先 escape hatch，
	# 即使存在 --cli-control flag 或 debug auto-enable 也强制关闭。
	if OS.get_environment("GODOT_CLI_CONTROL") == "0":
		return false
	if _has_cli_flag("--cli-control"):
		return true
	if OS.get_environment("GODOT_CLI_CONTROL") == "1":
		return true
	if OS.is_debug_build() and ProjectSettings.get_setting(SETTING_AUTO_ENABLE, true):
		return true
	return false


func _has_cli_flag(flag: String) -> bool:
	for arg: String in OS.get_cmdline_args():
		if arg == flag:
			return true
	return false


func _parse_port_from_args() -> int:
	for arg: String in OS.get_cmdline_args():
		if arg.begins_with("--game-bridge-port="):
			var parts: PackedStringArray = arg.split("=", false, 1)
			if parts.size() != 2 or not parts[1].is_valid_int():
				push_warning(
					"GameBridge: Invalid port value %s, falling back to %d" % [arg, DEFAULT_PORT]
				)
				return DEFAULT_PORT
			var port: int = parts[1].to_int()
			if port < 1 or port > 65535:
				push_warning(
					(
						"GameBridge: Port %d out of range [1, 65535], falling back to %d"
						% [port, DEFAULT_PORT]
					)
				)
				return DEFAULT_PORT
			return port
	return DEFAULT_PORT


func _parse_idle_timeout_from_args() -> int:
	for arg: String in OS.get_cmdline_args():
		if arg.begins_with("--game-bridge-idle-timeout="):
			var parts: PackedStringArray = arg.split("=", false, 1)
			if parts.size() != 2 or not parts[1].is_valid_int():
				push_warning("GameBridge: Invalid idle-timeout %s, disabling" % arg)
				return 0
			var secs: int = parts[1].to_int()
			return secs if secs > 0 else 0
	return 0


func _check_idle() -> void:
	var idle_ms: int = Time.get_ticks_msec() - _last_activity_ms
	if idle_ms / 1000 >= _idle_timeout_secs:
		print("GameBridge: idle for %ds, shutting down" % (idle_ms / 1000))
		get_tree().quit()
