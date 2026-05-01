## GUT 单元测试：GameBridge JSON-RPC 路由器
##
## 策略：测试覆盖 _handle_message 的 dispatch 全部分支 —— 不起 TCPServer / WebSocket，
## 不进 scene tree（避免 _ready 跑 listen()/queue_free）。
##   1. TestableGameBridge.new() 是 orphan Node；_ready 不触发。
##   2. 手动塞 _low_level_api / _input_sim_api（StubLowLevelApi / StubInputSimulationApi）
##      并调 _register_methods()，让方法表指向 stub 的 handler。
##   3. 子类 override _send_json 把出站帧捕获到 captured_frames，
##      跳过 _active_peer 状态检查这一现实依赖。
##   4. 直接调 _handle_message(raw_json_string) 触发路由；async 路径用
##      await get_tree().process_frame 等待 stub 通过 callback 回响。
##
## 这套测试用来拦的回归（最近三个 commit 都改这一带，黑盒测了但缺单测）：
##   - b1f2ec9: NodePath 子属性黑名单（实际由 LowLevelApi 测 —— bridge 只测路由）
##   - e7b9768: async type-check（async handler 返回 null/非 dict 必须发 -32603 而非挂死）
##   - b654259: id/method/params 类型严校验
extends GutTest

const GameBridgeScript := preload("res://addons/godot_cli_control/bridge/game_bridge.gd")
const LowLevelApiScript := preload("res://addons/godot_cli_control/bridge/low_level_api.gd")
const InputSimulationApiScript := preload("res://addons/godot_cli_control/bridge/input_simulation_api.gd")


# ── 子类：捕获 _send_json 出站 + 跳过 peer 状态检查 ──────────────────

class TestableGameBridge:
	extends GameBridge
	var captured_frames: Array = []

	func _send_json(data: Dictionary) -> void:
		# 不检查 _active_peer —— 测试场景里它就是 null。直接把帧记下来。
		captured_frames.append(data)


# ── 桩 LowLevelApi：sync / async handler 各覆盖 1 个，返回值由测试预置 ──

class StubLowLevelApi:
	extends LowLevelApi
	# 每个 handler 的预置返回值 + 调用记录
	var click_return: Dictionary = {"success": true}
	var click_calls: Array = []
	# 父类签名 -> Dictionary 是静态类型契约，override 不能放宽到 Variant；
	# async type-guard（返回非 dict）由测试通过外部 Callable 注入到 _methods。
	var wait_for_node_return: Dictionary = {"found": true}
	var wait_for_node_calls: Array = []

	func handle_click(params: Dictionary) -> Dictionary:
		click_calls.append(params)
		return click_return

	func wait_for_node_async(params: Dictionary) -> Dictionary:
		wait_for_node_calls.append(params)
		# 推一帧让调用方真的走 await 路径
		await get_tree().process_frame
		return wait_for_node_return


# ── 桩 InputSimulationApi：sync handler + async_with_id（combo） ──

class StubInputSimulationApi:
	extends InputSimulationApi
	var press_return: Dictionary = {"success": true}
	var press_calls: Array = []
	# combo 控制：测试通过 finish_combo() 决定何时回响 + 返回什么
	var combo_calls: Array = []  # [{params, request_id}]
	var combo_callback: Callable = Callable()

	func setup(send_response: Callable) -> void:
		# GameBridge._ready 调 setup() 时不会跑（orphan 实例），但
		# _register_methods 之前测试会手动调一次。
		combo_callback = send_response

	func handle_action_press(params: Dictionary) -> Dictionary:
		press_calls.append(params)
		return press_return

	func handle_combo(params: Dictionary, request_id: String) -> void:
		# 不立刻回响 —— 把 (params, request_id) 记下来；测试通过 finish_combo
		# 显式触发 callback，模拟真实 combo 完成路径。
		combo_calls.append({"params": params, "request_id": request_id})

	func finish_combo(index: int, result: Dictionary) -> void:
		var entry: Dictionary = combo_calls[index]
		combo_callback.call(entry["request_id"], result)


# ── 测试夹具 ────────────────────────────────────────────────────────

var _bridge: TestableGameBridge
var _low: StubLowLevelApi
var _input: StubInputSimulationApi


func before_each() -> void:
	_bridge = TestableGameBridge.new()
	# orphan：不 add_child 到 tree → _ready 不触发 → 跳过 listen() / queue_free
	autofree(_bridge)

	# 但 stub APIs 需要在 tree 内才能 await get_tree().process_frame
	_low = StubLowLevelApi.new()
	_low.name = "LowLevelApi"
	add_child_autofree(_low)
	_input = StubInputSimulationApi.new()
	_input.name = "InputSimulationApi"
	add_child_autofree(_input)

	_bridge._low_level_api = _low
	_bridge._input_sim_api = _input
	# InputSim 的 callback：bridge 的 _on_async_response 把 (id, result) 转回 dispatch
	_input.setup(_bridge._on_async_response)
	_bridge._register_methods()


# ── helper ──

func _send(raw: String) -> void:
	_bridge._handle_message(raw)


func _last_frame() -> Dictionary:
	assert_true(_bridge.captured_frames.size() > 0, "应该至少有一个出站帧")
	return _bridge.captured_frames[-1]


# ── JSON / 协议层校验：-32600 ──────────────────────────────────────

func test_invalid_json_emits_minus_32600_with_empty_id() -> void:
	_send("not valid json {{")
	var f: Dictionary = _last_frame()
	assert_eq(str(f.get("id", "MISSING")), "")
	assert_has(f, "error")
	assert_eq(int(f.error.code), -32600)
	assert_string_contains(str(f.error.message), "Invalid JSON")


func test_non_dict_root_emits_minus_32600() -> void:
	# 合法 JSON 但顶层是 array —— 不是 RPC 请求
	_send("[1, 2, 3]")
	var f: Dictionary = _last_frame()
	assert_eq(int(f.error.code), -32600)


func test_id_non_string_emits_minus_32600_with_empty_id() -> void:
	# id 是数字时无法回响给客户端正确的 id，强制空串 + 协议错
	_send('{"id": 42, "method": "click", "params": {}}')
	var f: Dictionary = _last_frame()
	assert_eq(str(f.get("id")), "", "id 非字符串时响应必须用空串而非数字")
	assert_eq(int(f.error.code), -32600)
	assert_string_contains(str(f.error.message), "id must be string")


func test_method_non_string_emits_minus_32600() -> void:
	_send('{"id": "x", "method": 123, "params": {}}')
	var f: Dictionary = _last_frame()
	assert_eq(str(f.id), "x")
	assert_eq(int(f.error.code), -32600)
	assert_string_contains(str(f.error.message), "method must be string")


func test_method_empty_emits_minus_32600() -> void:
	_send('{"id": "x", "method": "", "params": {}}')
	var f: Dictionary = _last_frame()
	assert_eq(int(f.error.code), -32600)
	assert_string_contains(str(f.error.message), "Missing method")


func test_params_non_dict_emits_minus_32600() -> void:
	# params 是 array —— handler 内 .get 会崩，必须在路由层挡住
	_send('{"id": "x", "method": "click", "params": [1, 2]}')
	var f: Dictionary = _last_frame()
	assert_eq(int(f.error.code), -32600)
	assert_string_contains(str(f.error.message), "params must be object")


func test_params_missing_treated_as_empty_dict() -> void:
	# params 缺失 → handler 拿到空 dict（合法），不应报协议错
	_send('{"id": "x", "method": "click"}')
	var f: Dictionary = _last_frame()
	assert_does_not_have(f, "error")
	assert_eq(_low.click_calls.size(), 1)
	assert_eq(_low.click_calls[0].size(), 0, "params 缺失应等价空 dict")


func test_id_missing_defaults_to_empty_string() -> void:
	# 客户端用 "" 当 fire-and-forget id；缺省也走这条路径，响应 id 也是 ""
	_send('{"method": "click", "params": {}}')
	var f: Dictionary = _last_frame()
	assert_eq(str(f.get("id")), "")
	assert_does_not_have(f, "error")


# ── 方法层校验：-32601 ────────────────────────────────────────────

func test_unknown_method_emits_minus_32601() -> void:
	_send('{"id": "x", "method": "no_such_method", "params": {}}')
	var f: Dictionary = _last_frame()
	assert_eq(int(f.error.code), -32601)
	assert_string_contains(str(f.error.message), "Unknown method")


# ── sync 路径 ─────────────────────────────────────────────────────

func test_sync_handler_success_emits_result_frame() -> void:
	_low.click_return = {"success": true, "node_class": "Button"}
	_send('{"id": "abc", "method": "click", "params": {"path": "/root/Btn"}}')
	var f: Dictionary = _last_frame()
	assert_eq(str(f.id), "abc")
	assert_has(f, "result")
	assert_does_not_have(f, "error")
	assert_eq(f.result.success, true)
	assert_eq(str(f.result.node_class), "Button")
	# 参数透传到 stub
	assert_eq(_low.click_calls[0].get("path"), "/root/Btn")


func test_sync_handler_error_dict_emits_error_frame() -> void:
	# handler 主动返回 {"error": {...}} —— _dispatch_result 应识别并发 error 帧
	_low.click_return = {"error": {"code": 1001, "message": "Node not found"}}
	_send('{"id": "x", "method": "click", "params": {"path": "/missing"}}')
	var f: Dictionary = _last_frame()
	assert_has(f, "error")
	assert_does_not_have(f, "result", "error 路径不应同时带 result")
	assert_eq(int(f.error.code), 1001)
	assert_eq(str(f.error.message), "Node not found")


# ── async 路径 ────────────────────────────────────────────────────

func test_async_handler_success_emits_result_frame() -> void:
	_low.wait_for_node_return = {"found": true}
	_send('{"id": "wait1", "method": "wait_for_node", "params": {"path": "/X", "timeout": 1.0}}')
	# stub 的 await get_tree().process_frame 让响应延后一帧；推两帧足够
	await get_tree().process_frame
	await get_tree().process_frame
	var f: Dictionary = _last_frame()
	assert_eq(str(f.id), "wait1")
	assert_has(f, "result")
	assert_eq(f.result.get("found"), true)


func _async_returning_null(_params: Dictionary) -> Variant:
	# 故意返回非 Dictionary 触发 _run_async 的 type-guard。
	# 父类 LowLevelApi.wait_for_node_async 签名锁死 -> Dictionary，没法在 stub 里
	# 直接 override 类型放宽，绕道：测试通过 Callable 注入 _methods 项。
	await get_tree().process_frame
	return null


func _async_returning_string(_params: Dictionary) -> Variant:
	await get_tree().process_frame
	return "oops"


func test_async_handler_returning_non_dict_emits_minus_32603() -> void:
	# 关键回归（commit e7b9768）：async handler 返回 null / 字符串 → 必须发
	# -32603，不能让响应永远不发，否则客户端 await 挂死到 30s timeout。
	_bridge._methods["wait_for_node"] = {
		"callable": _async_returning_null,
		"kind": "async",
	}
	_send('{"id": "wait_null", "method": "wait_for_node", "params": {}}')
	await get_tree().process_frame
	await get_tree().process_frame
	var f: Dictionary = _last_frame()
	assert_eq(str(f.id), "wait_null")
	assert_has(f, "error", "async handler 返回 null 必须落到 -32603 错误")
	assert_eq(int(f.error.code), -32603)
	assert_string_contains(str(f.error.message), "non-dict")


func test_async_handler_returning_string_emits_minus_32603() -> void:
	# 防御性：handler 不小心 return "" 也走 type-guard
	_bridge._methods["wait_for_node"] = {
		"callable": _async_returning_string,
		"kind": "async",
	}
	_send('{"id": "wait_str", "method": "wait_for_node", "params": {}}')
	await get_tree().process_frame
	await get_tree().process_frame
	var f: Dictionary = _last_frame()
	assert_eq(int(f.error.code), -32603)


# ── async_with_id 路径（input_combo） ──────────────────────────────

func test_async_with_id_routes_request_id_to_handler() -> void:
	_send('{"id": "combo1", "method": "input_combo", "params": {"steps": [{"action": "a", "duration": 0.1}]}}')
	# handler 不立即响应：captured_frames 应为空
	assert_eq(_bridge.captured_frames.size(), 0, "input_combo 是 async_with_id，不应同步发响应")
	assert_eq(_input.combo_calls.size(), 1)
	assert_eq(str(_input.combo_calls[0].request_id), "combo1")
	# 触发 callback 回响
	_input.finish_combo(0, {"success": true, "completed_steps": 1})
	var f: Dictionary = _last_frame()
	assert_eq(str(f.id), "combo1")
	assert_has(f, "result")
	assert_eq(int(f.result.completed_steps), 1)


func test_async_with_id_error_callback_emits_error_frame() -> void:
	# combo handler 通过 callback 回 error dict → bridge 应转成 error 帧
	_send('{"id": "combo_err", "method": "input_combo", "params": {"steps": []}}')
	_input.finish_combo(0, {"error": {"code": 1004, "message": "combo in progress"}})
	var f: Dictionary = _last_frame()
	assert_eq(str(f.id), "combo_err")
	assert_has(f, "error")
	assert_eq(int(f.error.code), 1004)


func test_async_with_id_id_isolation_across_concurrent_requests() -> void:
	# 三个 combo 请求并发，回调乱序触发：每条响应必须用对应的原始 id
	_send('{"id": "c1", "method": "input_combo", "params": {"steps": []}}')
	_send('{"id": "c2", "method": "input_combo", "params": {"steps": []}}')
	_send('{"id": "c3", "method": "input_combo", "params": {"steps": []}}')
	assert_eq(_input.combo_calls.size(), 3)
	# 乱序回响
	_input.finish_combo(1, {"success": true, "marker": "second"})
	_input.finish_combo(0, {"success": true, "marker": "first"})
	_input.finish_combo(2, {"success": true, "marker": "third"})
	# 收集 (id, marker) 对，验证配对正确
	var pairs: Dictionary = {}
	for f in _bridge.captured_frames:
		pairs[str(f.id)] = str((f.result as Dictionary).get("marker", ""))
	assert_eq(pairs.get("c1"), "first")
	assert_eq(pairs.get("c2"), "second")
	assert_eq(pairs.get("c3"), "third")


# ── 边界：空 id 仍是合法的 fire-and-forget ─────────────────────────

func test_empty_id_fire_and_forget_still_routes() -> void:
	# id="" 是合约：客户端不等响应，但 bridge 仍按协议发响应（带 id=""）
	_send('{"id": "", "method": "click", "params": {"path": "/x"}}')
	var f: Dictionary = _last_frame()
	assert_eq(str(f.id), "")
	assert_has(f, "result")


# ── 注册表完整性：断 sync 实际有路由（防 _register_methods 退化） ────

func test_sync_input_action_press_routes_to_input_sim_api() -> void:
	_send('{"id": "p1", "method": "input_action_press", "params": {"action": "jump"}}')
	var f: Dictionary = _last_frame()
	assert_does_not_have(f, "error")
	assert_eq(_input.press_calls.size(), 1)
	assert_eq(str(_input.press_calls[0].action), "jump")


# ── idle-timeout / in-flight 计数 ─────────────────────────────────
# 防止回归：长操作（>idle_timeout 的 wait_game_time / combo）期间 _check_idle
# 不能 quit，否则客户端拿不到响应。

func test_in_flight_starts_at_zero() -> void:
	assert_eq(_bridge._in_flight, 0)


func test_sync_dispatch_returns_in_flight_to_zero() -> void:
	_send('{"id": "s1", "method": "click", "params": {}}')
	assert_eq(_bridge._in_flight, 0, "sync 派发完成后 _in_flight 必须归零")


func test_async_dispatch_returns_in_flight_to_zero() -> void:
	_send('{"id": "a1", "method": "wait_for_node", "params": {"path": "/X", "timeout": 1.0}}')
	# stub 在 await get_tree().process_frame 期间 _in_flight 应保持 1
	assert_eq(_bridge._in_flight, 1, "async 等待期间 _in_flight 应为 1")
	await get_tree().process_frame
	await get_tree().process_frame
	assert_eq(_bridge._in_flight, 0, "async 派发完成后 _in_flight 必须归零")


func test_async_with_id_keeps_in_flight_until_callback() -> void:
	# 关键场景：input_combo 长动作（实际可能 5s+）期间，daemon 不能因 idle quit
	_send('{"id": "c1", "method": "input_combo", "params": {"steps": []}}')
	assert_eq(_bridge._in_flight, 1, "async_with_id handler 未回响前 _in_flight 应为 1")
	_input.finish_combo(0, {"success": true})
	assert_eq(_bridge._in_flight, 0, "回调后 _in_flight 必须归零")


func test_async_handler_returning_non_dict_decrements_in_flight() -> void:
	# type-guard 路径也必须减计数，否则 handler bug 会让 daemon 永远不 idle
	_bridge._methods["wait_for_node"] = {
		"callable": _async_returning_null,
		"kind": "async",
	}
	_send('{"id": "wn", "method": "wait_for_node", "params": {}}')
	await get_tree().process_frame
	await get_tree().process_frame
	assert_eq(_bridge._in_flight, 0, "非 dict 错误路径也必须把 _in_flight 减回 0")


func test_validation_failure_does_not_increment_in_flight() -> void:
	# Invalid JSON / 协议错没进过 handler，不应碰计数
	_send("not json")
	_send('{"id": 42, "method": "click"}')  # id 非串
	_send('{"id": "x", "method": "no_such"}')  # 未知方法
	assert_eq(_bridge._in_flight, 0, "校验失败路径不应增 _in_flight")


func test_check_idle_resets_activity_when_busy() -> void:
	# _check_idle 在 _in_flight > 0 时必须把活动戳推到现在 —— 否则一个跨越
	# 整个 idle_timeout 的长操作会被 quit。
	_bridge._idle_timeout_secs = 1
	_bridge._in_flight = 1
	_bridge._last_activity_ms = Time.get_ticks_msec() - 60_000  # 装作 60s 前
	_bridge._check_idle()
	var now: int = Time.get_ticks_msec()
	assert_almost_eq(_bridge._last_activity_ms, now, 200, "busy 时 _check_idle 必须把活动戳推到 ~now")
