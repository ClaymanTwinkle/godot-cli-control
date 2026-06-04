## GUT 单元测试：TimeApi（issue #102）
##
## Engine.time_scale / get_tree().paused 是全局状态，after_each 必须还原，
## 否则污染同一进程里的其它测试文件。
extends GutTest

const TimeApiScript := preload("res://addons/godot_cli_control/bridge/time_api.gd")

var _api: Node


func before_each() -> void:
	_api = TimeApiScript.new()
	_api.name = "TimeApi"
	add_child_autofree(_api)


func after_each() -> void:
	Engine.time_scale = 1.0
	get_tree().paused = false


# ── time_scale ──

func test_time_scale_read_returns_current() -> void:
	var result: Dictionary = _api.handle_time_scale({})
	assert_does_not_have(result, "error")
	assert_eq(float(result.get("time_scale")), 1.0)


func test_time_scale_write_sets_engine_and_returns_new_value() -> void:
	var result: Dictionary = _api.handle_time_scale({"value": 2.5})
	assert_does_not_have(result, "error")
	assert_eq(float(result.get("time_scale")), 2.5)
	assert_eq(Engine.time_scale, 2.5)


func test_time_scale_non_number_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_time_scale({"value": "fast"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_time_scale_zero_returns_minus_32602() -> void:
	## =0 冻死 wait-time 且与 pause 职责重叠，拒收
	var result: Dictionary = _api.handle_time_scale({"value": 0.0})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_eq(Engine.time_scale, 1.0, "拒收时不得改 Engine.time_scale")


func test_time_scale_above_max_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_time_scale({"value": 100.5})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


# ── pause / unpause ──

func test_pause_unpause_idempotent() -> void:
	var r1: Dictionary = _api.handle_pause({})
	assert_eq(r1.get("paused"), true)
	assert_true(get_tree().paused)
	var r2: Dictionary = _api.handle_pause({})
	assert_eq(r2.get("paused"), true, "重复 pause 幂等成功")
	var r3: Dictionary = _api.handle_unpause({})
	assert_eq(r3.get("paused"), false)
	assert_false(get_tree().paused)
	var r4: Dictionary = _api.handle_unpause({})
	assert_eq(r4.get("paused"), false, "重复 unpause 幂等成功")


# ── step_frames ──

func test_step_frames_not_paused_returns_1009() -> void:
	var result: Dictionary = await _api.step_frames_async({"frames": 2})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1009)
	assert_string_contains(str(result.error.message), "pause")


func test_step_frames_invalid_frames_returns_minus_32602() -> void:
	get_tree().paused = true
	for bad: Variant in [null, "abc", 0, 3601]:
		var result: Dictionary = await _api.step_frames_async({"frames": bad})
		assert_has(result, "error", "frames=%s 应报错" % [bad])
		assert_eq(int(result.error.code), -32602)


func test_step_frames_advances_and_ends_paused() -> void:
	get_tree().paused = true
	var result: Dictionary = await _api.step_frames_async({"frames": 2})
	assert_does_not_have(result, "error")
	assert_eq(int(result.get("stepped")), 2)
	assert_eq(result.get("paused"), true)
	assert_true(get_tree().paused, "step 结束必须停在 paused")


func test_step_frames_physics_advances() -> void:
	get_tree().paused = true
	var result: Dictionary = await _api.step_frames_async({"frames": 2, "physics": true})
	assert_does_not_have(result, "error")
	assert_eq(int(result.get("stepped")), 2)


# ── parse_cmdline_time_scale（静态纯函数）──

func test_parse_cmdline_time_scale_valid() -> void:
	var args := PackedStringArray(["--headless", "--cli-time-scale=2.5"])
	assert_eq(TimeApiScript.parse_cmdline_time_scale(args), 2.5)


func test_parse_cmdline_time_scale_absent_returns_minus_1() -> void:
	assert_eq(TimeApiScript.parse_cmdline_time_scale(PackedStringArray(["--headless"])), -1.0)


func test_parse_cmdline_time_scale_invalid_returns_minus_1() -> void:
	for bad: String in ["--cli-time-scale=abc", "--cli-time-scale=0", "--cli-time-scale=150", "--cli-time-scale="]:
		assert_eq(
			TimeApiScript.parse_cmdline_time_scale(PackedStringArray([bad])), -1.0,
			"%s 应返回 -1" % bad
		)
