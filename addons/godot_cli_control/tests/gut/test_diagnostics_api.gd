## GUT 单元测试：DiagnosticsApi push_error 捕获 + errors RPC（issue #103）
##
## 测试自身真的调 push_error / push_warning（会在 GUT 输出里留 ERROR 噪音，
## 属预期——正是要验证它们被结构化捕获）。Logger 实例只在用例期间挂载
## （before_each add / autofree 后 _exit_tree 摘除），不污染其它 suite。
extends GutTest

const DiagApiScript := preload("res://addons/godot_cli_control/bridge/diagnostics_api.gd")

var _api: Node


func before_each() -> void:
	_api = DiagApiScript.new()
	_api.name = "DiagnosticsApi"
	add_child_autofree(_api)


func test_push_error_is_captured_with_source() -> void:
	push_error("diag boom")
	var result: Dictionary = _api.handle_errors({})
	assert_does_not_have(result, "error")
	assert_eq(result.errors.size(), 1)
	var entry: Dictionary = result.errors[0]
	assert_eq(str(entry.type), "error")
	assert_eq(str(entry.message), "diag boom")
	assert_eq(int(entry.seq), 1)
	assert_eq(int(result.marker), 1)
	assert_eq(int(result.dropped), 0)
	assert_false(bool(result.truncated))
	# source 来自 ScriptBacktrace 首帧 —— 应指回本测试文件，而非 C++ 位置
	assert_string_contains(str(entry.source), "test_diagnostics_api.gd")


func test_push_warning_captured_as_warning_type() -> void:
	push_warning("diag warn")
	var result: Dictionary = _api.handle_errors({})
	assert_eq(result.errors.size(), 1)
	assert_eq(str(result.errors[0].type), "warning")


func test_since_filters_old_entries() -> void:
	push_error("first")
	push_error("second")
	push_error("third")
	var result: Dictionary = _api.handle_errors({"since": 2})
	assert_eq(result.errors.size(), 1)
	assert_eq(str(result.errors[0].message), "third")
	assert_eq(int(result.marker), 3)


func test_limit_truncates_and_marker_paginates() -> void:
	for i in 5:
		push_error("err %d" % i)
	var page1: Dictionary = _api.handle_errors({"limit": 2})
	assert_eq(page1.errors.size(), 2)
	assert_true(bool(page1.truncated))
	# 截断时 marker = 本批最后一条，下一页 since=marker 接着翻
	assert_eq(int(page1.marker), 2)
	var page2: Dictionary = _api.handle_errors({"since": page1.marker, "limit": 100})
	assert_eq(page2.errors.size(), 3)
	assert_false(bool(page2.truncated))
	assert_eq(int(page2.marker), 5)


func test_limit_zero_is_baseline_query() -> void:
	push_error("noise")
	var result: Dictionary = _api.handle_errors({"limit": 0})
	assert_eq(result.errors.size(), 0)
	assert_eq(int(result.marker), 1, "limit=0 拿当前基线 marker，不取数据")
	assert_false(bool(result.truncated), "limit=0 是基线查询，不算截断")


func test_ring_overflow_reports_dropped() -> void:
	_api._ring_cap = 3
	for i in 5:
		push_error("flood %d" % i)
	var result: Dictionary = _api.handle_errors({})
	assert_eq(result.errors.size(), 3, "ring cap=3 只留最后 3 条")
	assert_eq(int(result.dropped), 2, "挤掉的 2 条要透出 dropped 信号")
	assert_eq(str(result.errors[0].message), "flood 2")
	assert_eq(int(result.marker), 5)


func test_long_message_is_truncated() -> void:
	push_error("x".repeat(5000))
	var result: Dictionary = _api.handle_errors({})
	var msg: String = str(result.errors[0].message)
	assert_true(msg.length() < 1100, "超长消息必须截断（契约 6），实际 %d" % msg.length())
	assert_string_ends_with(msg, "...[truncated]")


func test_invalid_since_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_errors({"since": "abc"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_negative_since_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_errors({"since": -1})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_limit_over_max_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_errors({"limit": 1001})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_float_params_accepted_when_integral() -> void:
	# JSON 数字解析常落为 float：since=1.0 必须等价于 since=1
	push_error("a")
	push_error("b")
	var result: Dictionary = _api.handle_errors({"since": 1.0, "limit": 50.0})
	assert_does_not_have(result, "error")
	assert_eq(result.errors.size(), 1)


func test_logger_detaches_and_reattaches_with_tree() -> void:
	push_error("while attached")
	assert_eq(_api.handle_errors({}).errors.size(), 1)
	remove_child(_api)  # _exit_tree → OS.remove_logger
	push_error("after detach")
	add_child(_api)  # _enter_tree → 重挂 logger（_ready 只触发一次，故用 enter_tree）
	push_error("after reattach")
	var result: Dictionary = _api.handle_errors({})
	var messages: Array = []
	for e: Dictionary in result.errors:
		messages.append(str(e.message))
	assert_has(messages, "while attached")
	assert_has(messages, "after reattach", "重挂后必须恢复捕获")
	assert_does_not_have(messages, "after detach", "摘除期间的错误不应被捕获")
