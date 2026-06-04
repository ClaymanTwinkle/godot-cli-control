class_name SceneApi
extends Node
## 场景生命周期 API：scene_reload / scene_change（issue #98）
##
## 两者都等新场景 ready 才返回（逐帧轮询，模式对齐 wait_api.gd），
## 调用方不需要再 wait-node。错误码常量来自 error_codes.gd。

# 等新场景 ready 的超时上限 / 默认值（秒）
const _MAX_SCENE_TIMEOUT: float = 3600.0
const _DEFAULT_TIMEOUT: float = 10.0


func scene_reload_async(params: Dictionary) -> Dictionary:
	var timeout_or_err: Variant = _parse_timeout(params)
	if timeout_or_err is Dictionary:
		return timeout_or_err as Dictionary
	var timeout: float = timeout_or_err as float
	var old: Node = get_tree().current_scene
	if old == null:
		return _err(CliControlErrorCodes.SCENE_UNAVAILABLE, "no current scene to reload")
	var err: int = get_tree().reload_current_scene()
	if err != OK:
		return _err(
			CliControlErrorCodes.SCENE_UNAVAILABLE,
			"reload_current_scene failed: %s" % error_string(err),
		)
	return await _await_new_scene_ready(old, timeout)


func scene_change_async(params: Dictionary) -> Dictionary:
	var path_raw: Variant = params.get("path", null)
	if not (path_raw is String) or (path_raw as String).is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'path' parameter")
	var path: String = path_raw as String
	var timeout_or_err: Variant = _parse_timeout(params)
	if timeout_or_err is Dictionary:
		return timeout_or_err as Dictionary
	var timeout: float = timeout_or_err as float
	if not ResourceLoader.exists(path, "PackedScene"):
		return _err(CliControlErrorCodes.SCENE_UNAVAILABLE, "scene not found: %s" % path)
	# old 在 change 之前捕获，可为 null（script-mode / 启动早期）——
	# 此时 current_scene != null 本身即构成新实例判定，不会死循环。
	var old: Node = get_tree().current_scene
	var err: int = get_tree().change_scene_to_file(path)
	if err != OK:
		return _err(
			CliControlErrorCodes.SCENE_UNAVAILABLE,
			"change_scene_to_file failed: %s" % error_string(err),
		)
	return await _await_new_scene_ready(old, timeout)


## 逐帧等到 current_scene 是「不同于 old 的新实例」且 ready，返回新场景信息。
## reload 后 scene_file_path 不变，只能比实例 id。
func _await_new_scene_ready(old: Node, timeout: float) -> Dictionary:
	var old_id: int = old.get_instance_id() if old != null else 0
	var start_ms: int = Time.get_ticks_msec()
	while true:
		var cur: Node = get_tree().current_scene
		if cur != null and cur.get_instance_id() != old_id and cur.is_node_ready():
			return {"scene_path": cur.scene_file_path, "name": String(cur.name)}
		if float(Time.get_ticks_msec() - start_ms) / 1000.0 >= timeout:
			break
		await get_tree().process_frame
	return _err(
		CliControlErrorCodes.SCENE_UNAVAILABLE, "timeout waiting for new scene ready"
	)


## timeout 参数校验：合法返回 float，非法返回 error Dictionary（调用方透传）。
func _parse_timeout(params: Dictionary) -> Variant:
	var raw: Variant = params.get("timeout", _DEFAULT_TIMEOUT)
	if not (raw is int or raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'timeout' must be a number")
	var t: float = float(raw)
	if t < 0.0 or t > _MAX_SCENE_TIMEOUT:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"timeout must be 0..%s" % _MAX_SCENE_TIMEOUT,
		)
	return t


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
