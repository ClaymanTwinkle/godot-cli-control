## GUT 单元测试：SceneApi 错误分支（issue #98）
##
## GUT cmdln script-mode 下 current_scene 恒为 null，真 reload/change
## 由 python/tests/test_e2e_scene.py 兜底；这里测错误分支与参数校验。
extends GutTest

const SceneApiScript := preload("res://addons/godot_cli_control/bridge/scene_api.gd")

var _api: Node


func before_each() -> void:
	_api = SceneApiScript.new()
	_api.name = "SceneApi"
	add_child_autofree(_api)


func test_scene_reload_without_current_scene_returns_1008() -> void:
	var result: Dictionary = await _api.scene_reload_async({})
	assert_has(result, "error", "script-mode 无 current_scene，reload 应报错")
	assert_eq(int(result.error.code), 1008)
	assert_string_contains(str(result.error.message), "no current scene")


func test_scene_reload_invalid_timeout_returns_minus_32602() -> void:
	var result: Dictionary = await _api.scene_reload_async({"timeout": "abc"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_scene_change_missing_path_returns_minus_32602() -> void:
	var result: Dictionary = await _api.scene_change_async({})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_scene_change_nonexistent_scene_returns_1008() -> void:
	var result: Dictionary = await _api.scene_change_async({
		"path": "res://__definitely_missing__.tscn",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1008)
	assert_string_contains(str(result.error.message), "scene not found")


func test_scene_change_timeout_out_of_range_returns_minus_32602() -> void:
	var result: Dictionary = await _api.scene_change_async({
		"path": "res://anything.tscn", "timeout": -1.0,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
