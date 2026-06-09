class_name InputSimulationApi
extends Node
## 输入模拟 API：动作级按键、持续控制、组合序列
##
## 错误码常量来自 res://addons/godot_cli_control/bridge/error_codes.gd
## （class_name CliControlErrorCodes）。靠 Godot 全局 class 注册解析；若 GUT
## 测试跑前遇到 "Class 'CliControlErrorCodes' not found"，先 import 一次。

# 手动按下的动作（无定时器）
var _pressed_actions: Dictionary = {}
# 定时按住的动作：action_name -> remaining_seconds
var _held_actions: Dictionary = {}
# Combo 状态
var _combo_steps: Array = []
var _combo_index: int = 0
var _combo_timer: float = 0.0
var _combo_active: bool = false
var _combo_completed_steps: int = 0
# combo 完成时的回调 id（GameBridge 用）
var _combo_request_id: String = ""
# 用于 GameBridge 发送响应
var _send_response_callback: Callable = Callable()
# 坐标级鼠标事件（issue #154）：上一次注入的鼠标位置，用于 InputEventMouseMotion
# 的 relative 计算。viewport 物理像素系，初始 (0,0)。
var _last_mouse_pos: Vector2 = Vector2.ZERO


func setup(send_response: Callable) -> void:
	_send_response_callback = send_response


# ── 查询 ──

func get_pressed_actions() -> Array[String]:
	var result: Array[String] = []
	for action: String in _pressed_actions:
		result.append(action)
	for action: String in _held_actions:
		if not action in result:
			result.append(action)
	return result


func has_active_holds() -> bool:
	return not _held_actions.is_empty()


func is_combo_active() -> bool:
	return _combo_active


# ── 动作级 ──

func handle_action_press(params: Dictionary) -> Dictionary:
	if _combo_active:
		return _err(CliControlErrorCodes.COMBO_IN_PROGRESS, "combo in progress")
	var action: String = params.get("action", "") as String
	if not InputMap.has_action(action):
		return _err(CliControlErrorCodes.METHOD_NOT_FOUND, "Unknown action: %s" % action)
	_do_press(action)
	_pressed_actions[action] = true
	return {"success": true}


func handle_action_release(params: Dictionary) -> Dictionary:
	if _combo_active:
		return _err(CliControlErrorCodes.COMBO_IN_PROGRESS, "combo in progress")
	var action: String = params.get("action", "") as String
	if not InputMap.has_action(action):
		return _err(CliControlErrorCodes.METHOD_NOT_FOUND, "Unknown action: %s" % action)
	_do_release(action)
	_pressed_actions.erase(action)
	_held_actions.erase(action)
	return {"success": true}


## tap 只使用 _held_actions 轨道（不加入 _pressed_actions），
## 定时器到期后自动释放
func handle_action_tap(params: Dictionary) -> Dictionary:
	if _combo_active:
		return _err(CliControlErrorCodes.COMBO_IN_PROGRESS, "combo in progress")
	var action: String = params.get("action", "") as String
	if not InputMap.has_action(action):
		return _err(CliControlErrorCodes.METHOD_NOT_FOUND, "Unknown action: %s" % action)
	var duration: float = params.get("duration", 0.1) as float
	_do_press(action)
	_held_actions[action] = duration
	return {"success": true}


func handle_get_pressed(params: Dictionary) -> Dictionary:
	return {"actions": get_pressed_actions()}


func handle_list_input_actions(params: Dictionary) -> Dictionary:
	## 列举 InputMap 中已注册的动作。
	## include_builtin=false（默认）会过滤掉 ``ui_*`` 内置动作 ——
	## AI agent 通常只关心项目自身定义的动作。
	var include_builtin: bool = bool(params.get("include_builtin", false))
	var actions: Array[String] = []
	for raw in InputMap.get_actions():
		var name: String = String(raw)
		if not include_builtin and name.begins_with("ui_"):
			continue
		actions.append(name)
	actions.sort()
	return {"actions": actions}


# ── 持续控制 ──

func handle_hold(params: Dictionary) -> Dictionary:
	if _combo_active:
		return _err(CliControlErrorCodes.COMBO_IN_PROGRESS, "combo in progress")
	var action: String = params.get("action", "") as String
	if not InputMap.has_action(action):
		return _err(CliControlErrorCodes.METHOD_NOT_FOUND, "Unknown action: %s" % action)
	var duration: float = params.get("duration", 0.0) as float
	# duration <= 0 是无意义的「按住 0 秒」：advance_timers 下一帧就释放 → 只生效
	# 一帧。无限按住请用 press（sticky）。CLI preflight 也会拦，这里是防御纵深，
	# 挡住绕过 CLI 的直连 RPC。
	if duration <= 0.0:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"hold duration must be > 0 (got %s); use press for an indefinite hold" % duration,
		)
	_do_press(action)
	_held_actions[action] = duration
	return {"success": true}


func handle_release_all(_params: Dictionary) -> Dictionary:
	release_all()
	return {"success": true}


func release_all() -> void:
	for action: String in _pressed_actions:
		_do_release(action)
	_pressed_actions.clear()
	# _end_combo 也会清理 _held_actions，避免重复释放
	if _combo_active:
		_end_combo()
	else:
		for action: String in _held_actions:
			_do_release(action)
		_held_actions.clear()


# ── Combo ──

func handle_combo(params: Dictionary, request_id: String) -> void:
	if _combo_active:
		if _send_response_callback.is_valid():
			_send_response_callback.call(request_id, _err(CliControlErrorCodes.COMBO_IN_PROGRESS, "combo in progress"))
		return
	var steps: Array = params.get("steps", []) as Array
	_combo_request_id = request_id
	start_combo(steps)


func start_combo(steps: Array) -> void:
	_combo_steps = steps
	_combo_index = 0
	_combo_timer = 0.0
	_combo_active = true
	_combo_completed_steps = 0
	_begin_combo_step()


func cancel_combo() -> Dictionary:
	var completed: int = _combo_completed_steps
	# 抓 _combo_request_id 副本：_end_combo 会清空它，否则
	# 后续 callback 拿不到原 combo() 调用的 id，client 端 await 挂死到超时。
	var req_id: String = _combo_request_id
	_end_combo()
	if _send_response_callback.is_valid() and not req_id.is_empty():
		_send_response_callback.call(
			req_id,
			{"success": true, "completed_steps": completed, "cancelled": true},
		)
	return {"success": true, "completed_steps": completed}


func handle_combo_cancel(_params: Dictionary) -> Dictionary:
	if not _combo_active:
		return {"success": true, "completed_steps": 0}
	return cancel_combo()


func _begin_combo_step() -> void:
	if _combo_index >= _combo_steps.size():
		# 所有 step 执行完毕
		var completed: int = _combo_completed_steps
		var req_id: String = _combo_request_id
		_end_combo()
		if _send_response_callback.is_valid() and not req_id.is_empty():
			_send_response_callback.call(req_id, {"success": true, "completed_steps": completed})
		return
	# 非法 step 必须把 combo 整盘 abort 并通过原 request_id 回错误。
	# 否则 _combo_active 卡 true，后续 press / release / combo 全部 1004，
	# 客户端原 await 也挂死到 timeout。
	var raw: Variant = _combo_steps[_combo_index]
	if not raw is Dictionary:
		_abort_combo_with_error(
			CliControlErrorCodes.INVALID_PARAMS, "combo step must be object at index %d" % _combo_index
		)
		return
	var step: Dictionary = raw as Dictionary
	if step.has("wait"):
		_combo_timer = step["wait"] as float
	elif step.has("action"):
		var action: String = step["action"] as String
		if not InputMap.has_action(action):
			_abort_combo_with_error(
				CliControlErrorCodes.METHOD_NOT_FOUND, "Unknown action at combo step %d: %s" % [_combo_index, action]
			)
			return
		var duration: float = step.get("duration", 0.1) as float
		_do_press(action)
		_held_actions[action] = duration
		_combo_timer = duration
	else:
		_abort_combo_with_error(
			CliControlErrorCodes.INVALID_PARAMS, "combo step missing 'wait' or 'action' at index %d" % _combo_index
		)


func _abort_combo_with_error(code: int, message: String) -> void:
	var req_id: String = _combo_request_id
	_end_combo()
	if _send_response_callback.is_valid() and not req_id.is_empty():
		_send_response_callback.call(req_id, _err(code, message))


func _end_combo() -> void:
	# 释放所有 combo 持有的按键
	for action: String in _held_actions:
		_do_release(action)
	_held_actions.clear()
	_combo_active = false
	_combo_steps = []
	_combo_index = 0
	_combo_timer = 0.0
	_combo_request_id = ""


# ── 定时器推进（供 _process 和测试使用）──

func advance_timers(delta: float) -> void:
	# 更新 held actions
	var to_release: Array[String] = []
	for action: String in _held_actions:
		_held_actions[action] = (_held_actions[action] as float) - delta
		if (_held_actions[action] as float) <= 0.0:
			to_release.append(action)
	for action: String in to_release:
		_do_release(action)
		_held_actions.erase(action)
		_pressed_actions.erase(action)
	# 更新 combo
	if _combo_active:
		_combo_timer -= delta
		if _combo_timer <= 0.0:
			_combo_completed_steps += 1
			_combo_index += 1
			_begin_combo_step()


func _process(delta: float) -> void:
	advance_timers(delta)


# ── 底层输入操作 ──

## issue #97：走 Input.parse_input_event 而非 Input.action_press——
## InputEventAction 经输入泵进 SceneTree 事件管线（_input / _unhandled_input
## 可见），同时仍更新 action 状态位（is_action_pressed / get_vector 不回归）。
## 注意：InputEventAction 无坐标，依赖鼠标位置的 _gui_input 控件请用 click。
func _do_press(action: String) -> void:
	if not InputMap.has_action(action):
		return
	var ev: InputEventAction = InputEventAction.new()
	ev.action = action
	ev.pressed = true
	ev.strength = 1.0
	Input.parse_input_event(ev)


func _do_release(action: String) -> void:
	if not InputMap.has_action(action):
		return
	var ev: InputEventAction = InputEventAction.new()
	ev.action = action
	ev.pressed = false
	Input.parse_input_event(ev)


# ── 坐标级鼠标事件注入（issue #154，P1: click-at / mouse-move）──
#
# 经 viewport 注入真实事件管线（详见 _emit_mouse_button 上方的路径取舍说明），
# 让依赖光标位置的 _gui_input 控件 / 全局 _input / 物理 picking 能命中——这是
# click（定点 emit、需预知目标节点）补不上的坐标级能力。坐标统一用 viewport 物理
# 像素系；--node 糖衣复用 RenderApi.compute_node_screen_rect 取节点中心点
# （已含 viewport.get_final_transform()，与 screenshot 取图侧同系，#137）。

func handle_click_at(params: Dictionary) -> Dictionary:
	var point: Variant = _resolve_point(params, "node", "x", "y")
	if point is Dictionary:
		return point as Dictionary
	var pos: Vector2 = point as Vector2
	var button: int = int(params.get("button", MOUSE_BUTTON_LEFT))
	var double: bool = bool(params.get("double", false))
	_emit_mouse_button(pos, button, true, double)
	_emit_mouse_button(pos, button, false, double)
	return {"success": true, "x": pos.x, "y": pos.y, "button": button, "double": double}


func handle_mouse_move(params: Dictionary) -> Dictionary:
	var point: Variant = _resolve_point(params, "node", "x", "y")
	if point is Dictionary:
		return point as Dictionary
	var pos: Vector2 = point as Vector2
	var rel: Vector2 = pos - _last_mouse_pos
	_emit_mouse_motion(pos, 0)
	return {"success": true, "x": pos.x, "y": pos.y, "relative": [rel.x, rel.y]}


## 解析坐标：优先 ``node`` key（取节点中心点），否则字面 ``x``/``y``。
## 返回 Vector2（物理像素）或 error Dictionary（1001 找不到 / 1010 非 CanvasItem /
## -32602 既无 node 又无坐标）。compute_node_screen_rect 失败时本身就返回
## {"error": {...}}（1010），直接透传。
func _resolve_point(params: Dictionary, node_key: String, x_key: String, y_key: String) -> Variant:
	if params.has(node_key):
		var path: String = params[node_key] as String
		var node: Node = get_tree().root.get_node_or_null(path)
		if node == null:
			return _err(CliControlErrorCodes.NODE_NOT_FOUND, "Node not found: %s" % path)
		var rect_or_err: Variant = RenderApi.compute_node_screen_rect(node)
		if rect_or_err is Dictionary:
			return rect_or_err
		return (rect_or_err as Rect2).get_center()
	if params.has(x_key) and params.has(y_key):
		return Vector2(params[x_key] as float, params[y_key] as float)
	return _err(
		CliControlErrorCodes.INVALID_PARAMS,
		"requires literal x,y or a node path (got neither)",
	)


# 鼠标事件统一走 get_viewport().push_input()，而非 action 事件用的
# Input.parse_input_event。原因（与 action 的取舍不同，刻意区分）：
#   1. relative 自控：parse_input_event 会用 Input 单例内部追踪的 mouse_pos
#      重算 InputEventMouseMotion.relative，覆盖我们设的差值（headless 下 mouse_pos
#      恒 (0,0)，relative 直接错）。push_input 保留 ev.relative —— 这是 P2 drag
#      插值序列正确性的刚需。
#   2. 同路径保序：button 与 motion 必须走同一管道，否则 Input 单例的 buffer 与
#      viewport 直分发的到达顺序可能错乱（drag 的 down→motion→up 会乱序）。
#   3. 与现有 click handler 的直接 emit 同精神（都不经 Input 单例）。
# 已知限制：不更新 Input 单例的全局鼠标轮询（get_global_mouse_position /
# is_mouse_button_pressed）。事件的 position / relative / button_mask 对
# _input / _gui_input / 物理 picking 有效；读鼠标态请从事件参数读，勿用轮询。
func _emit_mouse_button(pos: Vector2, button: int, pressed: bool, double: bool = false) -> void:
	var ev: InputEventMouseButton = InputEventMouseButton.new()
	ev.button_index = button
	ev.pressed = pressed
	ev.double_click = double
	ev.position = pos
	ev.global_position = pos
	# 按下时带本键的 button_mask（松开归零），与真实事件一致。mask = 1 << (idx-1)。
	ev.button_mask = (1 << (button - 1)) if pressed else 0
	get_viewport().push_input(ev)
	_last_mouse_pos = pos


func _emit_mouse_motion(pos: Vector2, button_mask: int = 0) -> void:
	var ev: InputEventMouseMotion = InputEventMouseMotion.new()
	ev.position = pos
	ev.global_position = pos
	ev.relative = pos - _last_mouse_pos
	ev.button_mask = button_mask
	get_viewport().push_input(ev)
	_last_mouse_pos = pos


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
