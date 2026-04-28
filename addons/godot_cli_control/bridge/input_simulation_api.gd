class_name InputSimulationApi
extends Node
## 输入模拟 API：动作级按键、持续控制、组合序列

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
		return {"code": 1004, "error": "combo in progress"}
	var action: String = params.get("action", "") as String
	_do_press(action)
	_pressed_actions[action] = true
	return {"success": true}


func handle_action_release(params: Dictionary) -> Dictionary:
	if _combo_active:
		return {"code": 1004, "error": "combo in progress"}
	var action: String = params.get("action", "") as String
	_do_release(action)
	_pressed_actions.erase(action)
	_held_actions.erase(action)
	return {"success": true}


## tap 只使用 _held_actions 轨道（不加入 _pressed_actions），
## 定时器到期后自动释放
func handle_action_tap(params: Dictionary) -> Dictionary:
	if _combo_active:
		return {"code": 1004, "error": "combo in progress"}
	var action: String = params.get("action", "") as String
	var duration: float = params.get("duration", 0.1) as float
	_do_press(action)
	_held_actions[action] = duration
	return {"success": true}


func handle_get_pressed(params: Dictionary) -> Dictionary:
	return {"actions": get_pressed_actions()}


# ── 持续控制 ──

func handle_hold(params: Dictionary) -> Dictionary:
	if _combo_active:
		return {"code": 1004, "error": "combo in progress"}
	var action: String = params.get("action", "") as String
	var duration: float = params.get("duration", 0.0) as float
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
			_send_response_callback.call(request_id, {"code": 1004, "error": "combo in progress"})
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
	var step: Dictionary = _combo_steps[_combo_index] as Dictionary
	if step.has("wait"):
		_combo_timer = step["wait"] as float
	elif step.has("action"):
		var action: String = step["action"] as String
		var duration: float = step.get("duration", 0.1) as float
		_do_press(action)
		_held_actions[action] = duration
		_combo_timer = duration


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

func _do_press(action: String) -> void:
	if InputMap.has_action(action):
		Input.action_press(action)


func _do_release(action: String) -> void:
	if InputMap.has_action(action):
		Input.action_release(action)
