## GUT 单元测试：InputSimulationApi 状态机边界
##
## 重点：press / release / tap / hold 的状态轨道分离，combo 的 active /
## cancelled 转换，以及它们之间的互斥（combo 期间禁止 press / release）。
extends GutTest

const InputSimulationApiScript := preload("res://addons/godot_cli_control/bridge/input_simulation_api.gd")

var _api: Node


func before_each() -> void:
	_api = InputSimulationApiScript.new()
	_api.name = "InputSimulationApi"
	add_child_autofree(_api)


# ── action_press / action_release / action_tap ────────────────────

func test_press_then_release_clears_state() -> void:
	_api.handle_action_press({"action": "ui_accept"})
	assert_true("ui_accept" in _api.get_pressed_actions(), "press 后应在 pressed 列表")
	_api.handle_action_release({"action": "ui_accept"})
	assert_false("ui_accept" in _api.get_pressed_actions(), "release 后应清掉")


func test_tap_uses_held_track_not_pressed() -> void:
	# tap 把动作放进 _held_actions（自动定时释放），不进 _pressed_actions
	_api.handle_action_tap({"action": "ui_accept", "duration": 0.1})
	# 通过 pressed list 看：tap 后 action 应可见（pressed list 合并了两条轨道）
	assert_true("ui_accept" in _api.get_pressed_actions())
	# 但 _pressed_actions 不应包含它（间接验证：release 同名 action 后还应清干净）
	_api.handle_action_release({"action": "ui_accept"})
	assert_false("ui_accept" in _api.get_pressed_actions())


# ── combo 状态机 ──────────────────────────────────────────────────

func test_press_blocked_during_combo() -> void:
	_api.start_combo([{"action": "a", "duration": 1.0}])
	assert_true(_api.is_combo_active())
	var result: Dictionary = _api.handle_action_press({"action": "ui_accept"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1004)


func test_release_blocked_during_combo() -> void:
	_api.start_combo([{"action": "a", "duration": 1.0}])
	var result: Dictionary = _api.handle_action_release({"action": "ui_accept"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1004)


func test_handle_combo_cancel_when_inactive_no_op() -> void:
	# 没有 combo 在跑时 cancel 应直接返回 success / completed_steps=0
	assert_false(_api.is_combo_active())
	var result: Dictionary = _api.handle_combo_cancel({})
	assert_does_not_have(result, "error")
	assert_eq(int(result.get("completed_steps", -1)), 0)


func test_cancel_combo_returns_completed_count() -> void:
	_api.start_combo([{"action": "a", "duration": 1.0}, {"action": "b", "duration": 1.0}])
	# 模拟 1 步已 commit
	_api._combo_completed_steps = 1
	var result: Dictionary = _api.cancel_combo()
	assert_does_not_have(result, "error")
	assert_eq(int(result.get("completed_steps", -1)), 1)
	assert_false(_api.is_combo_active(), "cancel 后 combo 应不再 active")


# ── combo 非法 step：不能让 _combo_active 卡死 ───────────────────

class _ComboCallbackProbe:
	var calls: Array = []
	func record(id: String, result: Dictionary) -> void:
		calls.append({"id": id, "result": result})


func test_combo_aborts_on_non_dict_step() -> void:
	# 客户端误传字符串 / 数字当 step：必须 abort 整盘 combo 并通过 request_id
	# 回 -32602；否则 _combo_active 卡 true 导致后续所有 RPC 1004，client 挂死。
	var probe := _ComboCallbackProbe.new()
	_api.setup(probe.record)
	_api._combo_request_id = "req-1"
	_api.start_combo(["not a dict"])
	assert_false(_api.is_combo_active(), "非法 step 后 combo 必须 abort")
	assert_eq(probe.calls.size(), 1, "应通过原 request_id 回响应")
	assert_eq(str(probe.calls[0].id), "req-1")
	assert_has(probe.calls[0].result, "error")
	assert_eq(int(probe.calls[0].result.error.code), -32602)


func test_combo_aborts_on_step_missing_action_and_wait() -> void:
	# step 是 dict 但既无 wait 又无 action：同样 abort。
	var probe := _ComboCallbackProbe.new()
	_api.setup(probe.record)
	_api._combo_request_id = "req-2"
	_api.start_combo([{"unknown_key": 42}])
	assert_false(_api.is_combo_active())
	assert_eq(probe.calls.size(), 1)
	assert_has(probe.calls[0].result, "error")
	assert_eq(int(probe.calls[0].result.error.code), -32602)


# ── release_all ────────────────────────────────────────────────────

func test_release_all_clears_pressed_and_held() -> void:
	_api.handle_action_press({"action": "alpha"})
	_api.handle_hold({"action": "beta", "duration": 1.0})
	assert_eq(_api.get_pressed_actions().size(), 2)
	_api.release_all()
	assert_eq(_api.get_pressed_actions().size(), 0)
	assert_false(_api.has_active_holds())


# ── list_input_actions（0.2.0 新增：AI agent 发现项目动作） ──────────

func test_list_input_actions_default_filters_ui_builtins() -> void:
	if not InputMap.has_action("test_jump"):
		InputMap.add_action("test_jump")
	if not InputMap.has_action("ui_test_accept"):
		InputMap.add_action("ui_test_accept")

	var result: Dictionary = _api.handle_list_input_actions({})
	assert_has(result, "actions")
	var actions: Array = result.actions
	assert_true("test_jump" in actions, "项目自定义动作应出现")
	assert_false("ui_test_accept" in actions, "ui_* 内置默认应被过滤")

	InputMap.erase_action("test_jump")
	InputMap.erase_action("ui_test_accept")


func test_list_input_actions_include_builtin_returns_all() -> void:
	if not InputMap.has_action("test_attack"):
		InputMap.add_action("test_attack")
	if not InputMap.has_action("ui_test_cancel"):
		InputMap.add_action("ui_test_cancel")

	var result: Dictionary = _api.handle_list_input_actions({"include_builtin": true})
	var actions: Array = result.actions
	assert_true("test_attack" in actions)
	assert_true("ui_test_cancel" in actions, "include_builtin=true 应包含 ui_*")

	InputMap.erase_action("test_attack")
	InputMap.erase_action("ui_test_cancel")


func test_list_input_actions_returns_sorted() -> void:
	# 排序让 AI agent 输出可预测、便于 diff。
	for name in ["zzz_act", "aaa_act", "mmm_act"]:
		if not InputMap.has_action(name):
			InputMap.add_action(name)

	var result: Dictionary = _api.handle_list_input_actions({})
	var actions: Array = result.actions
	var picked: Array = []
	for a in actions:
		if a in ["aaa_act", "mmm_act", "zzz_act"]:
			picked.append(a)
	assert_eq(picked, ["aaa_act", "mmm_act", "zzz_act"], "actions 应按字母序返回")

	for name in ["zzz_act", "aaa_act", "mmm_act"]:
		InputMap.erase_action(name)
