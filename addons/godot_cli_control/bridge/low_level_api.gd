class_name LowLevelApi
extends Node
## 低层 API：通用节点操作（click、属性、场景树等）
##
## 错误码常量来自 res://addons/godot_cli_control/bridge/error_codes.gd
## （class_name CliControlErrorCodes）。靠 Godot 全局 class 注册解析，无需 preload；
## 若冷启动或 GUT 跑前遇到 "Class 'CliControlErrorCodes' not found"，先跑一次
## 完整 import（`godot --editor --quit --path .`）让 .godot/global_script_class_cache.cfg 建立。

const _BUILD_TREE_HARD_LIMIT: int = 50
# 总节点数上限：宽场景（1000+ 子项的 Grid/Container）会构造极大 JSON，
# 超 outbound buffer（默认 10 MB）后客户端拿到截断包 → STATE_CLOSED 断连。
# 5000 节点对应 ~500 KB JSON，留足余量。
const _BUILD_TREE_NODE_LIMIT: int = 5000
# wait_game_time_async 防呆上限：防止误传 1e9 之类的数值挂死 session
const _MAX_WAIT_SECONDS: float = 3600.0

# ProjectSettings 路径：第三方项目通过这两条额外补 ban 自家属性 / 方法。
# 合并到内置黑名单（去重，不能减只能加 —— 不开放 unban 是为了防止误删安全网）。
const SETTING_PROPERTY_BLACKLIST_EXTRA: String = "godot_cli_control/property_blacklist_extra"
const SETTING_METHOD_BLACKLIST_EXTRA: String = "godot_cli_control/method_blacklist_extra"

# 内置安全黑名单（不可禁用）
const _METHOD_BLACKLIST: PackedStringArray = [
	"queue_free", "free", "set_script", "add_child", "remove_child", "replace_by",
	# 反射类：可绕过 _PROPERTY_BLACKLIST 设置 script / texture 等被禁属性
	"set", "set_indexed", "set_deferred", "set_meta",
	# 任意 callable / 异步派发：等价于 RCE 入口
	"call", "callv", "call_deferred", "call_group", "call_group_flags",
	# 信号面：注入回调或断开关键信号
	"connect", "disconnect", "emit_signal", "add_user_signal",
]
const _PROPERTY_BLACKLIST: PackedStringArray = [
	"script", "process_mode",
	# Resource 注入防护：string → Resource 隐式解析可能触发自定义
	# Resource 的 _init / setter，避开 script 黑名单达到 RCE。
	"texture", "material", "shader", "mesh", "stream", "shape",
	"resource", "resource_path", "resource_name",
]

# 运行期合并 = 内置 ∪ ProjectSettings extras。_ready 里初始化一次。
var _property_blacklist: PackedStringArray = _PROPERTY_BLACKLIST.duplicate()
var _method_blacklist: PackedStringArray = _METHOD_BLACKLIST.duplicate()


func _ready() -> void:
	_property_blacklist = _merge_extra(_property_blacklist, SETTING_PROPERTY_BLACKLIST_EXTRA)
	_method_blacklist = _merge_extra(_method_blacklist, SETTING_METHOD_BLACKLIST_EXTRA)


func _merge_extra(base: PackedStringArray, setting_key: String) -> PackedStringArray:
	var raw: Variant = ProjectSettings.get_setting(setting_key, PackedStringArray())
	# ProjectSettings 写盘后类型可能是 Array 而非 PackedStringArray，宽松处理。
	if not (raw is PackedStringArray or raw is Array):
		return base
	var merged: PackedStringArray = base.duplicate()
	for item in raw:
		var name_str: String = str(item)
		if name_str.is_empty():
			continue
		if not (name_str in merged):
			merged.append(name_str)
	return merged


func handle_click(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	if node is BaseButton:
		(node as BaseButton).emit_signal("pressed")
		return {"success": true}
	if node is Control:
		var control: Control = node as Control
		var center: Vector2 = control.size / 2.0
		var click_event: InputEventMouseButton = InputEventMouseButton.new()
		click_event.button_index = MOUSE_BUTTON_LEFT
		click_event.position = center
		click_event.pressed = true
		control.gui_input.emit(click_event)
		return {"success": true}
	if node is Area2D:
		var area: Area2D = node as Area2D
		var click_event: InputEventMouseButton = InputEventMouseButton.new()
		click_event.button_index = MOUSE_BUTTON_LEFT
		click_event.pressed = true
		area.input_event.emit(get_viewport(), click_event, 0)
		return {"success": true}
	return _err(CliControlErrorCodes.INVALID_PARAMS, "Node is not clickable: %s" % node.get_class())


func handle_get_property(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var property: String = params.get("property", "") as String
	if property.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'property' parameter")
	if not _has_property(node, property):
		return _err(CliControlErrorCodes.PROPERTY_NOT_FOUND, "Property not found: %s" % property)
	return {"value": node.get(property)}


func handle_set_property(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var property: String = params.get("property", "") as String
	if property.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'property' parameter")
	# Godot Object.set() 接受 NodePath 形式的子属性（如 "position:x"）。
	# 精确字符串黑名单会漏掉 "script:source_code" / "texture:resource_path" 这类
	# 嵌套写入向量 —— 拿 ":" 前的 top-level 名重新过一次黑名单。
	# 同时整串也走一次（防御深度，万一未来加非冒号语法的反射子路径）。
	var top_level: String = property.split(":", true, 1)[0]
	if property in _property_blacklist or top_level in _property_blacklist:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Blocked property: %s" % property)
	var value: Variant = params.get("value", null)
	# #52：JSON 只能产 Array/Number/String/Bool/null。Godot Object.set("zoom", [1.8,1.8])
	# 不会隐式构造 Vector2，会走 zero-init / clamp 到 0.00001 → silent corruption。
	# 子路径（含 ":"）不查类型，让 Object.set 走 NodePath 子属性原路径（标量 OK）。
	if value is Array and not ":" in property:
		var coerced: Dictionary = _coerce_array_to_declared_type(node, top_level, value)
		if coerced.has("error"):
			return coerced
		if coerced.has("value"):
			value = coerced["value"]
	node.set(property, value)
	return {"success": true}


## 把 JSON Array 按声明类型转成 Vector2/2i/3/3i/4/4i / Rect2/2i / Color。
## 节点没声明该属性 / 声明类型不在「支持转换 + 已知会 silent-corrupt」名单：返 {} 表示"沿用原 value"。
## 转换失败（长度不对 / 元素非数字）：返 {"error": ...} 让调用方 fail-loud。
## 转换成功：返 {"value": <coerced>}。
## 已知会 silent-corrupt 但暂未实现转换的复合 Variant（Plane/Quaternion/AABB/Basis/
## Transform2D/Transform3D/Projection）也走 fail-loud 分支，避免重蹈 #52 覆辙。
## *i 变体（Vector2i / Vector3i / Vector4i / Rect2i）允许 float 输入并截断到 int，
## 与 GDScript `Vector2i(1.7, 2.3) → (1, 2)` 构造器行为一致。
func _coerce_array_to_declared_type(node: Node, property: String, arr: Array) -> Dictionary:
	var declared_type: int = -1
	for prop_info: Dictionary in node.get_property_list():
		if prop_info["name"] == property:
			declared_type = int(prop_info["type"])
			break
	if declared_type == -1:
		return {}  # 动态 / 未声明属性，沿用原 value
	match declared_type:
		TYPE_VECTOR2:
			return _coerce_numeric_array(arr, 2, "Vector2", property, func(v: Array) -> Variant:
				return Vector2(v[0], v[1]))
		TYPE_VECTOR2I:
			return _coerce_numeric_array(arr, 2, "Vector2i", property, func(v: Array) -> Variant:
				return Vector2i(int(v[0]), int(v[1])))
		TYPE_VECTOR3:
			return _coerce_numeric_array(arr, 3, "Vector3", property, func(v: Array) -> Variant:
				return Vector3(v[0], v[1], v[2]))
		TYPE_VECTOR3I:
			return _coerce_numeric_array(arr, 3, "Vector3i", property, func(v: Array) -> Variant:
				return Vector3i(int(v[0]), int(v[1]), int(v[2])))
		TYPE_VECTOR4:
			return _coerce_numeric_array(arr, 4, "Vector4", property, func(v: Array) -> Variant:
				return Vector4(v[0], v[1], v[2], v[3]))
		TYPE_VECTOR4I:
			return _coerce_numeric_array(arr, 4, "Vector4i", property, func(v: Array) -> Variant:
				return Vector4i(int(v[0]), int(v[1]), int(v[2]), int(v[3])))
		TYPE_RECT2:
			return _coerce_numeric_array(arr, 4, "Rect2", property, func(v: Array) -> Variant:
				return Rect2(v[0], v[1], v[2], v[3]))
		TYPE_RECT2I:
			return _coerce_numeric_array(arr, 4, "Rect2i", property, func(v: Array) -> Variant:
				return Rect2i(int(v[0]), int(v[1]), int(v[2]), int(v[3])))
		TYPE_COLOR:
			# Color 接受 RGB（3）或 RGBA（4）。
			if arr.size() == 3:
				if not _is_all_numeric(arr):
					return _err(CliControlErrorCodes.INVALID_PARAMS,
						"value type mismatch for '%s': Color expects numeric array" % property)
				return {"value": Color(arr[0], arr[1], arr[2])}
			if arr.size() == 4:
				if not _is_all_numeric(arr):
					return _err(CliControlErrorCodes.INVALID_PARAMS,
						"value type mismatch for '%s': Color expects numeric array" % property)
				return {"value": Color(arr[0], arr[1], arr[2], arr[3])}
			return _err(CliControlErrorCodes.INVALID_PARAMS,
				"value type mismatch for '%s': Color expects [r,g,b] or [r,g,b,a], got length %d"
					% [property, arr.size()])
		TYPE_PLANE, TYPE_QUATERNION, TYPE_AABB, TYPE_BASIS, \
		TYPE_TRANSFORM2D, TYPE_TRANSFORM3D, TYPE_PROJECTION:
			# 同 #52 路径：Object.set(prop, Array) 对这些复合 Variant 会 silent-corrupt。
			# 暂未实现构造转换；显式 fail-loud 比沿用原 value 让 agent 看不见错误更好。
			# 想写入时走 `call <node> <setter>` 或拆成子路径 (e.g. `transform:origin`)。
			return _err(CliControlErrorCodes.INVALID_PARAMS,
				"value type mismatch for '%s': Array coercion not supported for declared type %d; use call_method or sub-path form (e.g. 'transform:origin')"
					% [property, declared_type])
	# 其他声明类型（基本类型 / Array / Dictionary / Packed*Array 等）原样透传
	return {}


func _coerce_numeric_array(arr: Array, expected_len: int, type_name: String, property: String, ctor: Callable) -> Dictionary:
	if arr.size() != expected_len:
		return _err(CliControlErrorCodes.INVALID_PARAMS,
			"value type mismatch for '%s': expected %s as numeric array of length %d, got length %d"
				% [property, type_name, expected_len, arr.size()])
	if not _is_all_numeric(arr):
		return _err(CliControlErrorCodes.INVALID_PARAMS,
			"value type mismatch for '%s': expected %s as numeric array, got non-numeric element"
				% [property, type_name])
	return {"value": ctor.call(arr)}


func _is_all_numeric(arr: Array) -> bool:
	for item: Variant in arr:
		if not (item is float or item is int):
			return false
	return true


func handle_call_method(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var method: String = params.get("method", "") as String
	if method in _method_blacklist:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Blocked method: %s" % method)
	if not node.has_method(method):
		return _err(CliControlErrorCodes.METHOD_NOT_FOUND, "Method not found: %s" % method)
	var args: Array = params.get("args", []) as Array
	# 注意：GDScript 没有 try-catch，callv 参数不匹配会产生引擎错误而非可捕获异常
	var result: Variant = node.callv(method, args)
	return {"result": result}


func handle_get_text(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	if not "text" in node:
		return _err(CliControlErrorCodes.PROPERTY_NOT_FOUND, "Node does not have a 'text' property")
	return {"text": str(node.get("text"))}


func handle_node_exists(params: Dictionary) -> Dictionary:
	var path: String = params.get("path", "") as String
	var node: Node = get_tree().root.get_node_or_null(path)
	return {"exists": node != null}


func handle_is_visible(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	if not node is CanvasItem:
		return {"visible": true}
	var canvas_item: CanvasItem = node as CanvasItem
	return {"visible": canvas_item.visible}


func handle_get_children(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var type_filter: String = params.get("type_filter", "") as String
	var children: Array[Dictionary] = []
	for child: Node in node.get_children():
		if not type_filter.is_empty() and child.get_class() != type_filter:
			continue
		var entry: Dictionary = {
			"name": child.name,
			"type": child.get_class(),
			"path": str(child.get_path()),
		}
		children.append(entry)
	return {"children": children}


func handle_get_scene_tree(params: Dictionary) -> Dictionary:
	var max_depth: int = params.get("depth", 5) as int
	# max_nodes 是客户端控制的软上限。**入口处 clamp 到硬墙
	# _BUILD_TREE_NODE_LIMIT (5000)**：防止恶意 / 失误调用传 max_nodes=999999
	# 时让 _build_tree 真把整棵超大树构造成 Dictionary 后才被外层错误返回丢弃
	# （DoS / OOM 路径）。clamp 后 _build_tree 内部的短路单一来源，
	# 同时承担软上限（agent truncated 信号）与硬墙（防爆 outbound buffer）。
	# 不传时也用硬墙做默认，兼容旧客户端。
	var max_nodes: int = params.get("max_nodes", _BUILD_TREE_NODE_LIMIT) as int
	if max_nodes <= 0 or max_nodes > _BUILD_TREE_NODE_LIMIT:
		max_nodes = _BUILD_TREE_NODE_LIMIT
	var root: Node = get_tree().current_scene
	if root == null:
		root = get_tree().root
	# counter 用 Array[int] 当 by-ref 计数器：GDScript 没指针/inout，
	# Array 是引用类型，递归子调用对 counter[0] 的写入对调用方可见。
	var counter: Array[int] = [0]
	var tree: Dictionary = _build_tree(root, max_depth, 0, counter, max_nodes)
	# 触发 1005 的语义：counter 越过硬墙 = 场景大到不该序列化。
	# 因为 max_nodes 已 clamp 到 ≤ LIMIT，counter 最多比 LIMIT 多 ~1，
	# 所以这条分支只在 max_nodes==LIMIT（客户端没传或传了 ≥LIMIT）时被触发。
	# max_nodes < LIMIT 的客户端永远走 truncated 软信号路径，不会撞 1005。
	if counter[0] > _BUILD_TREE_NODE_LIMIT:
		return _err(
			CliControlErrorCodes.SCENE_TREE_TOO_LARGE,
			"scene tree too large (>%d nodes); lower 'depth' or query a subtree" % _BUILD_TREE_NODE_LIMIT,
		)
	# 软上限：硬墙内但超过 max_nodes 时附加 truncated 信号让 agent 决定分子树。
	var response: Dictionary = {"tree": tree}
	if counter[0] > max_nodes:
		response["truncated"] = true
		response["total_nodes"] = counter[0]
	return response


func wait_for_node_async(params: Dictionary) -> Dictionary:
	var path: String = params.get("path", "") as String
	var timeout: float = params.get("timeout", 5.0) as float
	var elapsed: float = 0.0
	var poll_interval: float = 0.1
	while elapsed < timeout:
		var node: Node = get_tree().root.get_node_or_null(path)
		if node != null:
			return {"found": true}
		await get_tree().create_timer(poll_interval).timeout
		elapsed += poll_interval
	return {"found": false}


func take_screenshot_async() -> Dictionary:
	# dummy renderer (--headless) 下 RenderingServer.frame_post_draw 永不发射，
	# await 会永久挂死。RenderingServer.get_rendering_device() 在 dummy driver
	# 下返回 null，用它检测后改走 process_frame 推进路径。
	if RenderingServer.get_rendering_device() == null:
		# dummy 路径：连续推 2 帧，让 viewport 跑一次完整 update
		await get_tree().process_frame
		await get_tree().process_frame
	else:
		await RenderingServer.frame_post_draw
	var image: Image = get_viewport().get_texture().get_image()
	if image == null:
		# 1006 (transient) ≠ 1003 (schema)：agent 可短重试，等下一帧 viewport 就绪。
		return _err(CliControlErrorCodes.RESOURCE_UNAVAILABLE, "Screenshot unavailable (viewport texture is null)")
	var png_buffer: PackedByteArray = image.save_png_to_buffer()
	var base64_str: String = Marshalls.raw_to_base64(png_buffer)
	return {"image": base64_str}


func wait_game_time_async(params: Dictionary) -> Dictionary:
	var seconds: float = params.get("seconds", 0.0) as float
	if seconds < 0.0:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "seconds must be >= 0")
	if seconds > _MAX_WAIT_SECONDS:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "seconds must be <= %s" % _MAX_WAIT_SECONDS)
	if seconds == 0.0:
		return {"success": true}
	await get_tree().create_timer(seconds).timeout
	return {"success": true}


func _get_node_or_error(params: Dictionary) -> Node:
	var path: String = params.get("path", "") as String
	return get_tree().root.get_node_or_null(path)


func _node_not_found(path: String) -> Dictionary:
	return _err(CliControlErrorCodes.NODE_NOT_FOUND, "Node not found: %s" % path)


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}


func _has_property(node: Node, property: String) -> bool:
	for prop: Dictionary in node.get_property_list():
		if prop["name"] == property:
			return true
	return false


## depth=0 表示"无限深度"，使用硬限制 _BUILD_TREE_HARD_LIMIT (50) 防止无限递归。
## counter 是 by-ref 计数器：超过 max_nodes 时短路（不再递归子节点），
## 调用方读 counter[0] > max_nodes 决定是否附加 truncated 信号，
## 读 counter[0] > _BUILD_TREE_NODE_LIMIT 决定是否走 1005 (SCENE_TREE_TOO_LARGE) 错误路径。
func _build_tree(node: Node, max_depth: int, current_depth: int, counter: Array[int], max_nodes: int) -> Dictionary:
	counter[0] += 1
	var entry: Dictionary = {
		"name": node.name,
		"type": node.get_class(),
		"path": str(node.get_path()),
	}
	if node is CanvasItem:
		entry["visible"] = (node as CanvasItem).visible
	if "text" in node:
		entry["text"] = str(node.get("text"))
	if counter[0] > max_nodes:
		# 超软上限：不再下递归，但当前 entry 已计入；调用方附加 truncated 信号
		return entry
	var effective_max: int = _BUILD_TREE_HARD_LIMIT if max_depth == 0 else max_depth
	if current_depth < effective_max:
		var children: Array[Dictionary] = []
		for child: Node in node.get_children():
			children.append(_build_tree(child, effective_max, current_depth + 1, counter, max_nodes))
		entry["children"] = children
	return entry
