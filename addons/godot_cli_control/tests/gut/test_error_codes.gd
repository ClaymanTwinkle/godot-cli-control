## GUT 单元测试：CliControlErrorCodes.hint_for —— 错误码 → 下一步提示。
##
## 契约：每个 1xxx 业务码都必须登记 hint（agent 在错误发生点当场拿到指引，
## 相当于把 SKILL.md 错误码表的「下一步」列下沉到运行时）。新增业务码忘
## 登记 → test_every_business_code_has_hint 红，与 error_codes.gd 头注释的
## 「新加业务码必须在这里登记」同一守护思路。
extends GutTest

const ErrorCodes := preload("res://addons/godot_cli_control/bridge/error_codes.gd")


func test_every_business_code_has_hint() -> void:
	# get_script_constant_map 是 Script 的实例方法：经 preload 常量（类命名空间）
	# 直呼会被 4.7 解析器判「对类调非静态函数」拒载，先落到 Script 类型变量再调。
	var script_res: Script = ErrorCodes
	var consts: Dictionary = script_res.get_script_constant_map()
	for key: String in consts:
		var value: Variant = consts[key]
		if value is int and int(value) >= 1000 and int(value) < 2000:
			assert_ne(
				ErrorCodes.hint_for(int(value)),
				"",
				"业务码 %s(%d) 未登记 hint —— 新码必须同时登记下一步提示" % [key, value]
			)


func test_unknown_code_returns_empty() -> void:
	assert_eq(ErrorCodes.hint_for(4242), "")


func test_method_unknown_hints_init_resync() -> void:
	# -32601 = client/addon 版本漂移，指引重跑 init 同步
	assert_string_contains(ErrorCodes.hint_for(-32601), "init")


func test_invalid_params_has_no_hint() -> void:
	# -32602 每个 case 的 message 已带专属文案，不加统一 hint（避免复述）
	assert_eq(ErrorCodes.hint_for(-32602), "")
