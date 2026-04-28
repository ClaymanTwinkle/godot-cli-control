class_name LowLevelApi
extends Node
## 低层 API：通用节点操作（click、属性、场景树等）

const _BUILD_TREE_HARD_LIMIT: int = 50
# wait_game_time_async 防呆上限：防止误传 1e9 之类的数值挂死 session
const _MAX_WAIT_SECONDS: float = 3600.0

# 安全黑名单
const _METHOD_BLACKLIST: PackedStringArray = [
	"queue_free", "free", "set_script", "add_child", "remove_child", "replace_by",
]
const _PROPERTY_BLACKLIST: PackedStringArray = [
	"script", "process_mode",
	# Resource 注入防护：string → Resource 隐式解析可能触发自定义
	# Resource 的 _init / setter，避开 script 黑名单达到 RCE。
	"texture", "material", "shader", "mesh", "stream", "shape",
	"resource", "resource_path", "resource_name",
]


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
	return {"error": "Node is not clickable: %s" % node.get_class(), "code": -32602}


func handle_get_property(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var property: String = params.get("property", "") as String
	if property.is_empty():
		return {"error": "Missing 'property' parameter", "code": -32602}
	if not _has_property(node, property):
		return {"error": "Property not found: %s" % property, "code": 1002}
	return {"value": node.get(property)}


func handle_set_property(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var property: String = params.get("property", "") as String
	if property.is_empty():
		return {"error": "Missing 'property' parameter", "code": -32602}
	if property in _PROPERTY_BLACKLIST:
		return {"error": "Blocked property: %s" % property, "code": -32602}
	var value: Variant = params.get("value", null)
	node.set(property, value)
	return {"success": true}


func handle_call_method(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var method: String = params.get("method", "") as String
	if method in _METHOD_BLACKLIST:
		return {"error": "Blocked method: %s" % method, "code": -32602}
	if not node.has_method(method):
		return {"error": "Method not found: %s" % method, "code": 1003}
	var args: Array = params.get("args", []) as Array
	# 注意：GDScript 没有 try-catch，callv 参数不匹配会产生引擎错误而非可捕获异常
	var result: Variant = node.callv(method, args)
	return {"result": result}


func handle_get_text(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	if not "text" in node:
		return {"error": "Node does not have a 'text' property", "code": 1002}
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
	var root: Node = get_tree().current_scene
	if root == null:
		root = get_tree().root
	var tree: Dictionary = _build_tree(root, max_depth, 0)
	return {"tree": tree}


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
	# headless dummy renderer 下 frame_post_draw 不会主动发射；先用 timer 推进
	# 一个 process tick，让 RenderingServer 跑一次再 await，避免 wrapper 单独
	# 调 screenshot 时永久挂死。GUI 模式额外 50ms 延迟可忽略。
	await get_tree().create_timer(0.05).timeout
	await RenderingServer.frame_post_draw
	var image: Image = get_viewport().get_texture().get_image()
	if image == null:
		return {"error": "Screenshot unavailable (viewport texture is null)", "code": 1003}
	var png_buffer: PackedByteArray = image.save_png_to_buffer()
	var base64_str: String = Marshalls.raw_to_base64(png_buffer)
	return {"image": base64_str}


func wait_game_time_async(params: Dictionary) -> Dictionary:
	var seconds: float = params.get("seconds", 0.0) as float
	if seconds < 0.0:
		return {"error": "seconds must be >= 0", "code": -32602}
	if seconds > _MAX_WAIT_SECONDS:
		return {"error": "seconds must be <= %s" % _MAX_WAIT_SECONDS, "code": -32602}
	if seconds == 0.0:
		return {"success": true}
	await get_tree().create_timer(seconds).timeout
	return {"success": true}


func _get_node_or_error(params: Dictionary) -> Node:
	var path: String = params.get("path", "") as String
	return get_tree().root.get_node_or_null(path)


func _node_not_found(path: String) -> Dictionary:
	return {"error": "Node not found: %s" % path, "code": 1001}


func _has_property(node: Node, property: String) -> bool:
	for prop: Dictionary in node.get_property_list():
		if prop["name"] == property:
			return true
	return false


## depth=0 表示"无限深度"，使用硬限制 _BUILD_TREE_HARD_LIMIT (50) 防止无限递归
func _build_tree(node: Node, max_depth: int, current_depth: int) -> Dictionary:
	var entry: Dictionary = {
		"name": node.name,
		"type": node.get_class(),
		"path": str(node.get_path()),
	}
	if node is CanvasItem:
		entry["visible"] = (node as CanvasItem).visible
	if "text" in node:
		entry["text"] = str(node.get("text"))
	var effective_max: int = _BUILD_TREE_HARD_LIMIT if max_depth == 0 else max_depth
	if current_depth < effective_max:
		var children: Array[Dictionary] = []
		for child: Node in node.get_children():
			children.append(_build_tree(child, effective_max, current_depth + 1))
		entry["children"] = children
	return entry
