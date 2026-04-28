class_name GameBridge
extends Node
## WebSocket 服务器 + JSON-RPC 消息路由

const DEFAULT_PORT: int = 9877
const SETTING_AUTO_ENABLE: String = "godot_cli_control/auto_enable_in_debug"

var _tcp_server: TCPServer = TCPServer.new()
var _active_peer: WebSocketPeer = null
var _active_stream: StreamPeerTCP = null
var _port: int = DEFAULT_PORT
var _low_level_api: LowLevelApi = null
var _input_sim_api: InputSimulationApi = null
var _low_level_methods: Dictionary = {}
var _input_methods: Dictionary = {}


func _ready() -> void:
	if not _should_activate():
		print("[Godot CLI Control] inactive — pass --cli-control, set GODOT_CLI_CONTROL=1, or enable %s in Project Settings (debug build only)" % SETTING_AUTO_ENABLE)
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
	# 构建方法路由表
	_low_level_methods = {
		"click": _low_level_api.handle_click,
		"get_property": _low_level_api.handle_get_property,
		"set_property": _low_level_api.handle_set_property,
		"call_method": _low_level_api.handle_call_method,
		"get_text": _low_level_api.handle_get_text,
		"node_exists": _low_level_api.handle_node_exists,
		"is_visible": _low_level_api.handle_is_visible,
		"get_children": _low_level_api.handle_get_children,
		"get_scene_tree": _low_level_api.handle_get_scene_tree,
	}
	_input_methods = {
		"input_action_press": _input_sim_api.handle_action_press,
		"input_action_release": _input_sim_api.handle_action_release,
		"input_action_tap": _input_sim_api.handle_action_tap,
		"input_get_pressed": _input_sim_api.handle_get_pressed,
		"input_hold": _input_sim_api.handle_hold,
		"input_move": _input_sim_api.handle_move,
		"input_release_all": _input_sim_api.handle_release_all,
		"input_combo_cancel": _input_sim_api.handle_combo_cancel,
	}
	# 启动 TCP 服务器
	_port = _parse_port_from_args()
	# 安全：显式绑 127.0.0.1，避免 Godot TCPServer 默认 "*" 暴露到 LAN
	var err: Error = _tcp_server.listen(_port, "127.0.0.1")
	if err != OK:
		push_error("GameBridge: Failed to listen on port %d: %s" % [_port, error_string(err)])
		return
	print("GameBridge: Listening on ws://localhost:%d" % _port)


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
	_active_peer.outbound_buffer_size = 10 * 1024 * 1024  # 10MB
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


func _handle_message(raw: String) -> void:
	var parsed: Variant = JSON.parse_string(raw)
	if parsed == null or not parsed is Dictionary:
		_send_error("", -32600, "Invalid JSON")
		return
	var msg: Dictionary = parsed as Dictionary
	var id: String = msg.get("id", "") as String
	var method: String = msg.get("method", "") as String
	var params: Dictionary = msg.get("params", {}) as Dictionary
	if method.is_empty():
		_send_error(id, -32600, "Missing method")
		return
	# wait_for_node（异步，不阻塞）
	if method == "wait_for_node":
		_handle_wait_for_node_async(id, params)
		return
	# wait_game_time（异步，按 game delta 等待）
	if method == "wait_game_time":
		_handle_wait_game_time_async(id, params)
		return
	# 低层 API（同步）
	if _low_level_methods.has(method):
		var handler: Callable = _low_level_methods[method] as Callable
		var result: Dictionary = handler.call(params)
		if result.has("error"):
			var code: int = result.get("code", -1) as int
			_send_error(id, code, result["error"] as String)
		else:
			_send_response(id, result)
		return
	# screenshot（异步，需要等渲染）
	if method == "screenshot":
		_handle_screenshot_async(id)
		return
	# 输入模拟（同步，除了 combo）
	if method == "input_combo":
		_input_sim_api.handle_combo(params, id)
		return
	if _input_methods.has(method):
		var handler: Callable = _input_methods[method] as Callable
		var result: Dictionary = handler.call(params)
		if result.has("code"):
			_send_error(id, result["code"] as int, result.get("error", "") as String)
		elif result.has("error"):
			_send_error(id, -1, result["error"] as String)
		else:
			_send_response(id, result)
		return
	# 未知方法
	_send_error(id, -32601, "Unknown method: %s" % method)


func _handle_wait_for_node_async(id: String, params: Dictionary) -> void:
	var result: Dictionary = await _low_level_api.wait_for_node_async(params)
	_send_response(id, result)


func _handle_screenshot_async(id: String) -> void:
	var result: Dictionary = await _low_level_api.take_screenshot_async()
	_send_response(id, result)


func _handle_wait_game_time_async(id: String, params: Dictionary) -> void:
	var result: Dictionary = await _low_level_api.wait_game_time_async(params)
	if result.has("error"):
		_send_error(id, result.get("code", -1) as int, result["error"] as String)
	else:
		_send_response(id, result)


func _on_async_response(id: String, result: Dictionary) -> void:
	if result.has("error"):
		_send_error(id, result.get("code", -1) as int, result["error"] as String)
	else:
		_send_response(id, result)


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
	if _has_cli_flag("--cli-control"):
		return true
	if OS.get_environment("GODOT_CLI_CONTROL") == "1":
		return true
	if OS.is_debug_build() and ProjectSettings.get_setting(SETTING_AUTO_ENABLE, false):
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
			if parts.size() == 2 and parts[1].is_valid_int():
				return parts[1].to_int()
	return DEFAULT_PORT
