class_name GameBridge
extends Node
## WebSocket 服务器 + JSON-RPC 消息路由

const DEFAULT_PORT: int = 9877
const SETTING_AUTO_ENABLE: String = "godot_cli_control/auto_enable_in_debug"
const SETTING_OUTBOUND_BUFFER_MB: String = "godot_cli_control/outbound_buffer_mb"
const DEFAULT_OUTBOUND_BUFFER_MB: int = 10
# 启动 gate：listen 前等 viewport 首帧 ready 的上限帧数。
# 60fps 下 ~2s；shader 编译 / 大资源加载超过这个就走 fallback —— 仍开端口，
# 把后续 screenshot 的 transient 兜底交给 take_screenshot_async 的循环。
# 设计意图见 issue #61：根因消除（H）+ handler 内动态兜底（D）。
const FIRST_FRAME_READY_MAX_FRAMES: int = 120

var _tcp_server: TCPServer = TCPServer.new()
var _active_peer: WebSocketPeer = null
var _active_stream: StreamPeerTCP = null
var _port: int = DEFAULT_PORT
var _idle_timeout_secs: int = 0
var _last_activity_ms: int = 0
# 正在处理中的请求数。> 0 表示 daemon 没闲着，idle 检查必须放过 —— 否则
# 一条 wait_game_time(3600) 会被 30m idle-timeout 半路打断，客户端拿不到响应。
# 入：消息通过参数校验、即将派发到 handler 时 +1。
# 出：_dispatch_result（sync/async/async_with_id 三条路径的最终响应点）-1。
# 校验失败的 _send_error 不计数（没进过 handler）。
var _in_flight: int = 0
var _outbound_buffer_size: int = DEFAULT_OUTBOUND_BUFFER_MB * 1024 * 1024
var _low_level_api: LowLevelApi = null
var _input_sim_api: InputSimulationApi = null
var _wait_api: WaitApi = null
var _scene_api: SceneApi = null
var _time_api: TimeApi = null
var _render_api: RenderApi = null
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
	_wait_api = WaitApi.new()
	_wait_api.name = "WaitApi"
	add_child(_wait_api)
	_wait_api.setup(_low_level_api._read_property)
	_scene_api = SceneApi.new()
	_scene_api.name = "SceneApi"
	add_child(_scene_api)
	_time_api = TimeApi.new()
	_time_api.name = "TimeApi"
	add_child(_time_api)
	_render_api = RenderApi.new()
	_render_api.name = "RenderApi"
	add_child(_render_api)
	# 启动倍速（issue #102）：非法值 parse 内已 printerr + 忽略，不挡启动
	# 必须在 await _wait_first_frame_ready() 之前，保证「第 0 帧即倍速」
	var startup_scale: float = TimeApi.parse_cmdline_time_scale(OS.get_cmdline_args())
	if startup_scale > 0.0:
		Engine.time_scale = startup_scale
		print("GameBridge: Engine.time_scale = %s (from --cli-time-scale)" % startup_scale)
	# 构建统一方法注册表
	_register_methods()
	# 缓存 outbound buffer 大小（ProjectSettings 可覆盖默认 10MB，至少 1MB）
	var mb: int = int(
		ProjectSettings.get_setting(SETTING_OUTBOUND_BUFFER_MB, DEFAULT_OUTBOUND_BUFFER_MB)
	)
	_outbound_buffer_size = max(1, mb) * 1024 * 1024
	# 启动 gate：等 viewport 首帧 ready 后再开 listen。
	# 根因：issue #61。screenshot 在「viewport 从未画过一次」时拿到 null
	# texture → 报 1006 transient；让 client connect 上来这件事本身就承诺
	# 「至少画过一帧」，把根因消除而不是各 handler 各自打补丁。
	# 超时不阻塞 listen —— 端口冲突 / 启动诊断信号要保留（_tcp_server.listen
	# 失败的 printerr 在端口冲突时是用户唯一的 root cause 提示）。
	await _wait_first_frame_ready()
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


func _wait_first_frame_ready() -> void:
	# dummy renderer (--headless) 下 RenderingServer.frame_post_draw 永不发射；
	# 与 take_screenshot_async 用同一套检测，dummy 路径走 process_frame ×2。
	# 上限 FIRST_FRAME_READY_MAX_FRAMES 后无论 viewport 是否 ready 都返回，
	# 让 listen 不被 GPU 卡死 / 大场景首帧拖住，避免 daemon 启动绑架到项目复杂度。
	var dummy: bool = RenderingServer.get_rendering_device() == null
	if dummy:
		await get_tree().process_frame
		await get_tree().process_frame
		return
	for _i in FIRST_FRAME_READY_MAX_FRAMES:
		await RenderingServer.frame_post_draw
		var image: Image = get_viewport().get_texture().get_image()
		if image != null:
			return


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
		_handle_disconnect(_active_peer.get_close_code())
		return
	if state != WebSocketPeer.STATE_OPEN:
		return
	while _active_peer.get_available_packet_count() > 0:
		var packet: PackedByteArray = _active_peer.get_packet()
		var message: String = packet.get_string_from_utf8()
		_handle_message(message)


func _handle_disconnect(close_code: int) -> void:
	print("GameBridge: Client disconnected (close_code=%d)" % close_code)
	# 区分「命令正常结束」与「异常掉线」，决定是否 release_all：
	#   - CLI 每条子命令都是独立连接、跑完即干净关闭（WebSocket close frame，
	#     code 1000）。此时**不** release_all —— 否则 `hold <dur>` 的定时器还没
	#     倒计时就被清掉（只生效一帧），sticky `press` 也无法跨命令存活。持有的
	#     输入靠各自机制收尾：hold/tap/combo 的 advance_timers 定时器自然结束，
	#     sticky press 持续到显式 release / release-all。
	#   - 客户端崩溃 / 被 kill / 网络断开时不会发 close frame，get_close_code()
	#     返回 -1（< 0）。此时 release_all 兜底，避免卡死键 + 跨会话脏状态残留。
	#   （daemon 启动期的端口探活连接也可能是 -1，但那时没有任何持有输入，
	#    release_all 无副作用。）
	# pytest 的 bridge fixture 仍在 teardown 自己调 release_all() 做清理。
	if close_code < 0:
		_input_sim_api.release_all()
	_active_peer = null
	_active_stream = null


func _register_methods() -> void:
	# 低层 API（同步）
	_methods["click"] = {"callable": _low_level_api.handle_click, "kind": "sync"}
	_methods["get_property"] = {"callable": _low_level_api.handle_get_property, "kind": "sync"}
	_methods["get_properties"] = {"callable": _low_level_api.handle_get_properties, "kind": "sync"}
	_methods["set_property"] = {"callable": _low_level_api.handle_set_property, "kind": "sync"}
	_methods["call_method"] = {"callable": _low_level_api.handle_call_method, "kind": "sync"}
	_methods["get_text"] = {"callable": _low_level_api.handle_get_text, "kind": "sync"}
	_methods["node_exists"] = {"callable": _low_level_api.handle_node_exists, "kind": "sync"}
	_methods["is_visible"] = {"callable": _low_level_api.handle_is_visible, "kind": "sync"}
	_methods["get_children"] = {"callable": _low_level_api.handle_get_children, "kind": "sync"}
	_methods["get_scene_tree"] = {"callable": _low_level_api.handle_get_scene_tree, "kind": "sync"}
	# Wait API（异步）
	_methods["wait_for_node"] = {"callable": _wait_api.wait_for_node_async, "kind": "async"}
	_methods["wait_game_time"] = {"callable": _wait_api.wait_game_time_async, "kind": "async"}
	_methods["wait_frames"] = {"callable": _wait_api.wait_frames_async, "kind": "async"}
	_methods["wait_property"] = {"callable": _wait_api.wait_property_async, "kind": "async"}
	_methods["wait_signal"] = {"callable": _wait_api.wait_signal_async, "kind": "async"}
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
	# Scene API（异步）
	_methods["scene_reload"] = {"callable": _scene_api.scene_reload_async, "kind": "async"}
	_methods["scene_change"] = {"callable": _scene_api.scene_change_async, "kind": "async"}
	# Time API（issue #102）
	_methods["sprite_info"] = {"callable": _render_api.handle_sprite_info, "kind": "sync"}

	_methods["time_scale"] = {"callable": _time_api.handle_time_scale, "kind": "sync"}
	_methods["pause"] = {"callable": _time_api.handle_pause, "kind": "sync"}
	_methods["unpause"] = {"callable": _time_api.handle_unpause, "kind": "sync"}
	_methods["step_frames"] = {"callable": _time_api.step_frames_async, "kind": "async"}


# screenshot wrapper：params 透传（issue #101 起带可选 "node" 裁剪参数）
func _wrap_screenshot(params: Dictionary) -> Dictionary:
	return await _low_level_api.take_screenshot_async(params)


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
	_in_flight += 1
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
		_in_flight = max(0, _in_flight - 1)
		_send_error(id, -32603, "internal: async handler returned non-dict")
		return
	_dispatch_result(id, raw as Dictionary)


func _dispatch_result(id: String, result: Dictionary) -> void:
	_in_flight = max(0, _in_flight - 1)
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
	# 有正在处理的请求 → daemon 没闲着；把活动戳推到现在，等所有请求落地后
	# 再开始计时。否则一个 wait_game_time(idle_timeout+1) 就会被半路 quit。
	if _in_flight > 0:
		_last_activity_ms = Time.get_ticks_msec()
		return
	var idle_ms: int = Time.get_ticks_msec() - _last_activity_ms
	if idle_ms / 1000 >= _idle_timeout_secs:
		print("GameBridge: idle for %ds, shutting down" % (idle_ms / 1000))
		get_tree().quit()
