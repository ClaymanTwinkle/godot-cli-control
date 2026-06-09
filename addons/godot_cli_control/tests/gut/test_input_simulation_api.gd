## GUT 单元测试：InputSimulationApi 状态机边界
##
## 重点：press / release / tap / hold 的状态轨道分离，combo 的 active /
## cancelled 转换，以及它们之间的互斥（combo 期间禁止 press / release）。
extends GutTest

const InputSimulationApiScript := preload("res://addons/godot_cli_control/bridge/input_simulation_api.gd")

var _api: Node


## 事件管线探针：记录 _input / _unhandled_input 收到的 InputEventAction（issue #97）
class _EventProbe extends Node:
	var input_actions: Array = []
	var unhandled_actions: Array = []

	func _input(event: InputEvent) -> void:
		if event is InputEventAction:
			input_actions.append(event)

	func _unhandled_input(event: InputEvent) -> void:
		if event is InputEventAction:
			unhandled_actions.append(event)

# combo / hold 状态机测试用占位 action。InputMap 不预设这些，由 fixture 注册。
const _FIXTURE_ACTIONS: Array[String] = ["a", "b", "alpha", "beta"]


func before_each() -> void:
	_api = InputSimulationApiScript.new()
	_api.name = "InputSimulationApi"
	add_child_autofree(_api)
	# #25 修复后 handle_action_press/release/hold 会拒未注册 action（1003）。
	# 状态机测试关心 combo / 互斥语义，不验 InputMap 校验，先把 fixture action 注册。
	for name in _FIXTURE_ACTIONS:
		if not InputMap.has_action(name):
			InputMap.add_action(name)


func after_each() -> void:
	_api.release_all()
	for name in _FIXTURE_ACTIONS:
		if InputMap.has_action(name):
			InputMap.erase_action(name)


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


# ── unknown action 校验（不在 InputMap 内） ─────────────────────────

func test_press_unknown_action_returns_1003() -> void:
	# 拼错 action 必须立刻返回错误，不能静默成功污染 _pressed_actions
	var bogus: String = "__definitely_not_an_action__"
	assert_false(InputMap.has_action(bogus))
	var result: Dictionary = _api.handle_action_press({"action": bogus})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1003)
	assert_false(bogus in _api.get_pressed_actions(), "失败时不能进 pressed 列表")


func test_release_unknown_action_returns_1003() -> void:
	var bogus: String = "__not_an_action_release__"
	var result: Dictionary = _api.handle_action_release({"action": bogus})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1003)


func test_tap_unknown_action_returns_1003() -> void:
	var bogus: String = "__not_an_action_tap__"
	var result: Dictionary = _api.handle_action_tap({"action": bogus, "duration": 0.1})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1003)
	assert_false(bogus in _api.get_pressed_actions())


func test_hold_unknown_action_returns_1003() -> void:
	var bogus: String = "__not_an_action_hold__"
	var result: Dictionary = _api.handle_hold({"action": bogus, "duration": 1.0})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1003)
	assert_false(_api.has_active_holds())


func test_hold_zero_duration_returns_invalid_params() -> void:
	# duration <= 0 是无意义的「按住 0 秒」；防御纵深拦在服务端（CLI preflight 也拦）。
	var result: Dictionary = _api.handle_hold({"action": "a", "duration": 0.0})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602, "duration=0 应回 INVALID_PARAMS(-32602)")
	assert_false(_api.has_active_holds(), "非法 duration 不应进 held 列表")
	assert_false("a" in _api.get_pressed_actions())


func test_hold_negative_duration_returns_invalid_params() -> void:
	var result: Dictionary = _api.handle_hold({"action": "a", "duration": -1.5})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_false(_api.has_active_holds())


func test_combo_unknown_action_aborts_with_1003() -> void:
	# combo step 引用未注册 action：必须 abort 整盘并通过 request_id 回 1003，
	# 否则 _combo_active 卡 true 后续全 1004。
	var probe := _ComboCallbackProbe.new()
	_api.setup(probe.record)
	_api._combo_request_id = "req-bogus"
	_api.start_combo([{"action": "__bogus_action__", "duration": 0.1}])
	assert_false(_api.is_combo_active())
	assert_eq(probe.calls.size(), 1)
	assert_has(probe.calls[0].result, "error")
	assert_eq(int(probe.calls[0].result.error.code), 1003)


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


# ── issue #97：press/release 必须走事件管线（_input / _unhandled_input 可见） ──

func test_press_feeds_input_event_pipeline() -> void:
	var probe := _EventProbe.new()
	add_child_autofree(probe)
	_api.handle_action_press({"action": "ui_accept"})
	# parse_input_event 注入的事件在输入泵分发；等两帧确保送达
	await wait_process_frames(2)
	assert_gt(probe.input_actions.size(), 0, "_input 应收到 InputEventAction")
	var ev: InputEventAction = probe.input_actions[0]
	assert_eq(String(ev.action), "ui_accept")
	assert_true(ev.pressed, "press 注入的事件应为 pressed=true")
	assert_eq(ev.strength, 1.0)
	assert_gt(probe.unhandled_actions.size(), 0, "_unhandled_input 也应收到 InputEventAction")
	_api.handle_action_release({"action": "ui_accept"})


func test_release_feeds_release_event() -> void:
	var probe := _EventProbe.new()
	add_child_autofree(probe)
	_api.handle_action_press({"action": "ui_accept"})
	await wait_process_frames(2)
	probe.input_actions.clear()
	_api.handle_action_release({"action": "ui_accept"})
	await wait_process_frames(2)
	assert_gt(probe.input_actions.size(), 0, "release 也应产生事件")
	var ev: InputEventAction = probe.input_actions[0]
	assert_false(ev.pressed, "release 注入的事件应为 pressed=false")


func test_press_still_updates_polling_state() -> void:
	# 轮询路径不回归：parse_input_event(InputEventAction) 同样更新 action 状态位
	_api.handle_action_press({"action": "ui_accept"})
	Input.flush_buffered_events()
	assert_true(Input.is_action_pressed("ui_accept"), "is_action_pressed 应感知 press")
	_api.handle_action_release({"action": "ui_accept"})
	Input.flush_buffered_events()
	assert_false(Input.is_action_pressed("ui_accept"), "release 后应清除")


# ── issue #154：坐标级鼠标事件注入（click-at / mouse-move，P1） ──────
#
# 走 Input.parse_input_event 注入真实管线（与 #97 InputEventAction 同思路），
# 坐标用 viewport 物理像素系；--node 糖衣复用 RenderApi.compute_node_screen_rect
# 取中心点。坐标系换算正确性直接断言 handler 返回的 x/y（纯坐标计算，headless
# 必测、不依赖事件路由到哪个 viewport）；事件序列断言用主 viewport 探针。

const _RenderApiScript := preload("res://addons/godot_cli_control/bridge/render_api.gd")


## 鼠标事件探针：记录 _input 收到的 InputEventMouseButton / InputEventMouseMotion
class _MouseProbe extends Node:
	var buttons: Array = []
	var motions: Array = []

	func _input(event: InputEvent) -> void:
		if event is InputEventMouseButton:
			buttons.append(event)
		elif event is InputEventMouseMotion:
			motions.append(event)


# ── click-at ──────────────────────────────────────────────────────

func test_click_at_emits_button_down_then_up() -> void:
	var probe := _MouseProbe.new()
	add_child_autofree(probe)
	var result: Dictionary = _api.handle_click_at({"x": 50.0, "y": 60.0})
	assert_does_not_have(result, "error")
	# parse_input_event 注入的事件在输入泵分发；等两帧确保送达（与 #97 测试一致）
	await wait_process_frames(2)
	assert_eq(probe.buttons.size(), 2, "click-at 应注入 down + up 两个鼠标按钮事件")
	var down: InputEventMouseButton = probe.buttons[0]
	var up: InputEventMouseButton = probe.buttons[1]
	assert_true(down.pressed, "第一个事件应为 pressed=true")
	assert_false(up.pressed, "第二个事件应为 pressed=false")
	assert_eq(down.button_index, MOUSE_BUTTON_LEFT, "默认左键")
	assert_eq(down.position, Vector2(50, 60), "down position 应为传入坐标")
	assert_eq(up.position, Vector2(50, 60), "up position 应为传入坐标")


func test_click_at_button_right() -> void:
	var probe := _MouseProbe.new()
	add_child_autofree(probe)
	_api.handle_click_at({"x": 10.0, "y": 10.0, "button": MOUSE_BUTTON_RIGHT})
	await wait_process_frames(2)
	assert_eq(probe.buttons.size(), 2)
	assert_eq(probe.buttons[0].button_index, MOUSE_BUTTON_RIGHT, "--button right 应注入右键")


func test_click_at_double_click_flag() -> void:
	var probe := _MouseProbe.new()
	add_child_autofree(probe)
	_api.handle_click_at({"x": 10.0, "y": 10.0, "double": true})
	await wait_process_frames(2)
	assert_eq(probe.buttons.size(), 2)
	assert_true(probe.buttons[0].double_click, "--double 应置 double_click=true")


func test_click_at_node_returns_screen_rect_center() -> void:
	# 坐标系回归（#137 同类）：--node 中心点必须经 viewport final transform，落在
	# 取图侧的物理像素系。SubViewport size(200,100)+override(100,50)+stretch = scale 2，
	# Control (10,20,30,40) → rect (20,40,60,80) → center (50,80)。
	var viewport := SubViewport.new()
	viewport.size = Vector2i(200, 100)
	viewport.size_2d_override = Vector2i(100, 50)
	viewport.size_2d_override_stretch = true
	add_child_autofree(viewport)
	var ctl := Control.new()
	ctl.position = Vector2(10, 20)
	ctl.size = Vector2(30, 40)
	viewport.add_child(ctl)
	var expected: Vector2 = (_RenderApiScript.compute_node_screen_rect(ctl) as Rect2).get_center()
	var result: Dictionary = _api.handle_click_at({"node": String(ctl.get_path())})
	assert_does_not_have(result, "error")
	assert_eq(Vector2(result.x, result.y), expected, "click-at --node 应点在节点中心（物理像素系）")


func test_click_at_node_not_found_returns_1001() -> void:
	var result: Dictionary = _api.handle_click_at({"node": "/root/__no_such_node_154__"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001, "--node 找不到应回 NODE_NOT_FOUND(1001)")


func test_click_at_node_non_canvasitem_returns_1010() -> void:
	var plain := Node.new()
	plain.name = "PlainNode154"
	add_child_autofree(plain)
	var result: Dictionary = _api.handle_click_at({"node": String(plain.get_path())})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1010, "--node 非 CanvasItem 应回 UNSUPPORTED_NODE_TYPE(1010)")


# ── mouse-move ────────────────────────────────────────────────────

func test_mouse_move_emits_motion_with_relative() -> void:
	var probe := _MouseProbe.new()
	add_child_autofree(probe)
	# fresh _api，_last_mouse_pos 默认 (0,0)
	var result: Dictionary = _api.handle_mouse_move({"x": 100.0, "y": 120.0})
	assert_does_not_have(result, "error")
	await wait_process_frames(2)
	assert_eq(probe.motions.size(), 1, "mouse-move 应注入一个 motion 事件")
	var mv: InputEventMouseMotion = probe.motions[0]
	assert_eq(mv.position, Vector2(100, 120))
	assert_eq(mv.relative, Vector2(100, 120), "首次 move 的 relative = 目标 - (0,0)")
	assert_eq(Vector2(result.relative[0], result.relative[1]), Vector2(100, 120))


func test_mouse_move_relative_accumulates() -> void:
	# 第二次 move 的 relative 应是与上一次位置的差值（_last_mouse_pos 被更新）
	_api.handle_mouse_move({"x": 100.0, "y": 100.0})
	var probe := _MouseProbe.new()
	add_child_autofree(probe)
	_api.handle_mouse_move({"x": 130.0, "y": 140.0})
	await wait_process_frames(2)
	assert_eq(probe.motions.size(), 1)
	assert_eq(probe.motions[0].relative, Vector2(30, 40), "relative = 与上次位置差值")


func test_mouse_move_node_returns_center() -> void:
	var ctl := Control.new()
	ctl.position = Vector2(10, 20)
	ctl.size = Vector2(40, 60)
	add_child_autofree(ctl)
	var expected: Vector2 = (_RenderApiScript.compute_node_screen_rect(ctl) as Rect2).get_center()
	var result: Dictionary = _api.handle_mouse_move({"node": String(ctl.get_path())})
	assert_does_not_have(result, "error")
	assert_eq(Vector2(result.x, result.y), expected, "mouse-move --node 应移到节点中心")


# ── drag（issue #154 P2）────────────────────────────────────────────
# handle_drag 是协程（kind=async）：button-down 与 mutex 在第一个 await 前同步完成，
# 随后按 duration/steps 分摊插值出 motion，最后 button-up。测试统一用小 duration，
# await 整个协程拿到所有已注入事件（push_input 同步分发，与 click-at 测试同源）。

func test_drag_emits_down_interp_motions_up() -> void:
	var probe := _MouseProbe.new()
	add_child_autofree(probe)
	var result: Dictionary = await _api.handle_drag(
		{"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 50.0, "duration": 0.05, "steps": 5}
	)
	assert_does_not_have(result, "error")
	await wait_process_frames(2)
	assert_eq(probe.buttons.size(), 2, "drag 应注入 down + up 两个按钮事件")
	assert_eq(probe.motions.size(), 5, "steps=5 应注入 5 个 motion")
	var down: InputEventMouseButton = probe.buttons[0]
	var up: InputEventMouseButton = probe.buttons[1]
	assert_true(down.pressed, "第一个按钮事件应为 down")
	assert_false(up.pressed, "最后一个按钮事件应为 up")
	assert_eq(down.position, Vector2(0, 0), "down 落在起点")
	assert_eq(up.position, Vector2(100, 50), "up 落在终点")
	# 线性插值：i/steps，i=1..5 → (20,10) (40,20) (60,30) (80,40) (100,50)
	assert_eq(probe.motions[0].position, Vector2(20, 10), "首个 motion = lerp(1/5)")
	assert_eq(probe.motions[4].position, Vector2(100, 50), "末个 motion 落在终点")
	assert_eq(probe.motions[0].relative, Vector2(20, 10), "首个 motion relative = 与起点差值")
	assert_eq(Vector2(result.to[0], result.to[1]), Vector2(100, 50), "result.to 回终点")


func test_drag_motions_carry_button_mask() -> void:
	# drag 过程中按钮处于按下态：每个 motion 的 button_mask 必须带住该键，
	# 否则被拖物体的 _gui_input 收到「没按键的移动」会误判为悬停而非拖拽。
	var probe := _MouseProbe.new()
	add_child_autofree(probe)
	await _api.handle_drag(
		{"x1": 0.0, "y1": 0.0, "x2": 60.0, "y2": 0.0, "duration": 0.03, "steps": 3}
	)
	await wait_process_frames(2)
	for mv: InputEventMouseMotion in probe.motions:
		assert_eq(
			mv.button_mask, MOUSE_BUTTON_MASK_LEFT, "drag 期间 motion 应带住左键 mask"
		)


func test_drag_concurrent_returns_1014() -> void:
	# 第一个 drag 跑到首个 await create_timer 让出，此时 _mouse_drag_active=true。
	# 不 await（让其挂起），立即发第二个 drag —— 应同步拿到 1014，不被允许并发。
	_api.handle_drag(
		{"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 100.0, "duration": 0.02, "steps": 2}
	)
	var second: Dictionary = await _api.handle_drag({"x1": 5.0, "y1": 5.0, "x2": 9.0, "y2": 9.0})
	assert_has(second, "error")
	assert_eq(int(second.error.code), 1014, "drag 进行中再次 drag 应回 DRAG_IN_PROGRESS(1014)")
	# 排空第一个 drag，避免悬挂协程污染后续测试
	await wait_seconds(0.1)
	assert_false(_api._mouse_drag_active, "首个 drag 完成后 mutex 应复位")


func test_drag_release_all_during_drag_emits_button_up() -> void:
	# 中断保护：drag 进行中（button 已 down）被 release_all 打断时，必须补一个
	# mouse-up，否则被拖控件永远收不到释放、卡在「正在拖拽」态。
	var probe := _MouseProbe.new()
	add_child_autofree(probe)
	_api.handle_drag(
		{"x1": 0.0, "y1": 0.0, "x2": 200.0, "y2": 200.0, "duration": 0.5, "steps": 50}
	)
	await wait_process_frames(2)
	assert_true(_api._mouse_drag_active, "drag 进行中 mutex 应为 true")
	_api.release_all()
	await wait_process_frames(2)
	var ups: Array = probe.buttons.filter(func(e: InputEventMouseButton) -> bool: return not e.pressed)
	assert_gt(ups.size(), 0, "release_all 中断 drag 应补一个 mouse-up")
	assert_false(_api._mouse_drag_active, "release_all 应复位 drag mutex")
	# 排空已被取消的协程（resume 后看到 flag=false 即早退，不再注入事件）
	await wait_seconds(0.1)


func test_drag_from_node_resolves_center() -> void:
	var ctl := Control.new()
	ctl.position = Vector2(10, 20)
	ctl.size = Vector2(40, 60)
	add_child_autofree(ctl)
	var expected: Vector2 = (_RenderApiScript.compute_node_screen_rect(ctl) as Rect2).get_center()
	var result: Dictionary = await _api.handle_drag(
		{"from_node": String(ctl.get_path()), "x2": 5.0, "y2": 5.0, "duration": 0.02, "steps": 2}
	)
	assert_does_not_have(result, "error")
	assert_eq(Vector2(result.from[0], result.from[1]), expected, "from_node 应解析到节点中心")
	assert_eq(Vector2(result.to[0], result.to[1]), Vector2(5, 5), "to 用字面坐标")


func test_drag_from_node_not_found_returns_1001() -> void:
	var result: Dictionary = await _api.handle_drag(
		{"from_node": "/root/__no_drag_node__", "x2": 1.0, "y2": 1.0}
	)
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001, "from_node 找不到应回 NODE_NOT_FOUND(1001)")


func test_drag_invalid_steps_returns_invalid_params() -> void:
	var result: Dictionary = await _api.handle_drag(
		{"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0, "steps": 0}
	)
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602, "steps<1 应回 INVALID_PARAMS(-32602)")
