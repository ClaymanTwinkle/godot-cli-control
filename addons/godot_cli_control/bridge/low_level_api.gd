class_name LowLevelApi
extends Node
## 低层 API：通用节点操作（click、属性、场景树等）
##
## 错误码常量来自 res://addons/godot_cli_control/bridge/error_codes.gd
## （class_name CliControlErrorCodes）。靠 Godot 全局 class 注册解析，无需 preload；
## 若冷启动或 GUT 跑前遇到 "Class 'CliControlErrorCodes' not found"，先跑一次
## 完整 import（`godot --editor --quit --path .`）让 .godot/global_script_class_cache.cfg 建立。

# depth=0（"无限深度"）时的递归深度兜底，防无限递归 / 病态深树。
const _BUILD_TREE_DEFAULT_MAX_DEPTH: int = 50
# 总节点数上限：宽场景（1000+ 子项的 Grid/Container）会构造极大 JSON，
# 超 outbound buffer（默认 10 MB）后客户端拿到截断包 → STATE_CLOSED 断连。
# 5000 节点对应 ~500 KB JSON，留足余量。
const _BUILD_TREE_MAX_NODES: int = 5000
# take_screenshot_async 循环上限：常态下 GameBridge 启动 gate 已保证 viewport
# ready（issue #61 H 部分），这个循环只兜动态 transient（scene transition、
# 窗口 resize 一瞬）。30 帧 ~500ms @ 60fps，超时报 1006 给 client 兜底。
const SCREENSHOT_MAX_FRAMES: int = 30

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

# 防御性白名单：声明类型在这里 = Object.set(prop, Array) 不会 silent-corrupt，原 Array
# 可以直接透传。除此之外的「未在 _coerce_array_to_declared_type 实现 coerce」的复合
# Variant 必须 fail-loud，避免未来 Godot 新增 Variant 时重蹈 #52 silent-corruption。
# 入选标准：
#   - 基本类型：Object.set 会拒收 Array（写入失败而非 silent-corrupt）
#   - 集合 / Packed* Array：本就接受 Array 输入（容器拷贝）
# 新增条目时先验证 Object.set(prop, Array) 真的安全，再加进来。
const _ARRAY_PASSTHROUGH_SAFE_TYPES: Array[int] = [
	TYPE_NIL, TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING,
	TYPE_STRING_NAME, TYPE_NODE_PATH, TYPE_RID, TYPE_OBJECT, TYPE_CALLABLE, TYPE_SIGNAL,
	TYPE_DICTIONARY, TYPE_ARRAY,
	TYPE_PACKED_BYTE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY,
	TYPE_PACKED_FLOAT32_ARRAY, TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_STRING_ARRAY,
	TYPE_PACKED_VECTOR2_ARRAY, TYPE_PACKED_VECTOR3_ARRAY, TYPE_PACKED_COLOR_ARRAY,
	TYPE_PACKED_VECTOR4_ARRAY,
]


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
	var read: Dictionary = _read_property(node, params.get("property", "") as String)
	if read.has("error"):
		return read
	# issue #99：复合 Variant 走 codec 编码（与 set 侧 array schema 对称），
	# 返回 {"value": ..., "type": <仅复合类型>}，round-trip 闭环。
	return CliControlVariantCodec.encode(read["value"])


## 读单个属性，支持 sub-path（"position:x"，与 set 侧对称走 get_indexed）。
## 返回 {"value": Variant} 或 {"error": ...}。
## sub-path 的 leaf 非法时 get_indexed 返回 null——与「真 null 值」无法区分，
## SKILL.md 已声明该边界（pitfalls 中「Sub-path reading a non-existent leaf」一条）；
## 这里只校验 ":" 前的 top-level 名存在（1002 兜底 typo）。
func _read_property(node: Node, property: String) -> Dictionary:
	if property.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'property' parameter")
	var is_sub_path: bool = ":" in property
	var top_level: String = _top_level_of(property)
	if not _has_property(node, top_level):
		return _err(CliControlErrorCodes.PROPERTY_NOT_FOUND, "Property not found: %s" % top_level)
	if is_sub_path:
		return {"value": node.get_indexed(NodePath(property))}
	return {"value": node.get(property)}


## issue #100：多属性同帧原子读。sync handler 无 await——所有读取天然同一帧。
## 原子语义：任一属性缺失整体失败（1002 点名全部缺失项），不返回半新半旧组合。
func handle_get_properties(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var props_raw: Variant = params.get("properties", null)
	if not props_raw is Array or (props_raw as Array).is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'properties' must be a non-empty array of strings")
	var props: Array = props_raw as Array
	var missing: PackedStringArray = []
	for raw_prop: Variant in props:
		if not raw_prop is String or (raw_prop as String).is_empty():
			return _err(CliControlErrorCodes.INVALID_PARAMS, "'properties' must be a non-empty array of strings")
		var prop_name: String = raw_prop as String
		var top_level: String = _top_level_of(prop_name)
		if not _has_property(node, top_level):
			missing.append(prop_name)
	if not missing.is_empty():
		return _err(CliControlErrorCodes.PROPERTY_NOT_FOUND, "Properties not found: %s" % ", ".join(missing))
	var values: Dictionary = {}
	for raw_prop: Variant in props:
		var prop_name: String = raw_prop as String
		var read: Dictionary = _read_property(node, prop_name)
		if read.has("error"):
			return read  # 防御纵深：上面已全量校验，理论到不了这里
		values[prop_name] = CliControlVariantCodec.encode(read["value"])
	return {"values": values}


func handle_set_property(params: Dictionary) -> Dictionary:
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var property: String = params.get("property", "") as String
	if property.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'property' parameter")
	# Godot Object.set() **不接受** sub-path（"position:x"），它当作字面属性名查找会失败。
	# sub-path 必须走 Object.set_indexed(NodePath, value)。精确字符串黑名单还需要拿 ":"
	# 前的 top-level 名重新过一次，否则 "script:source_code" / "texture:resource_path"
	# 这类嵌套写入会绕开 blacklist；整串也走一次（防御深度，万一未来加非冒号反射子路径）。
	var is_sub_path: bool = ":" in property
	var top_level: String = _top_level_of(property)
	if property in _property_blacklist or top_level in _property_blacklist:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Blocked property: %s" % property)
	var value: Variant = params.get("value", null)
	# #52：JSON 只能产 Array/Number/String/Bool/null。Godot Object.set("zoom", [1.8,1.8])
	# 不会隐式构造 Vector2，会走 zero-init / clamp 到 0.00001 → silent corruption。
	if value is Array:
		if is_sub_path:
			# set_indexed("transform:origin", Array) 同样不会把 Array 隐式构造成 Vector3
			# 写进 leaf —— silent-corrupt。比起重蹈 #52 覆辙，主动 fail-loud：要写整个复合
			# Variant 请用 top-level 形式（`set <node> transform '[basis 9, origin 3]'`，
			# #54 已覆盖所有复合 Variant 的 Array 写入）。sub-path 仅适合标量赋值
			# （`position:x 1.8`）。
			return _err(CliControlErrorCodes.INVALID_PARAMS,
				"value type mismatch for '%s': sub-path + Array is not supported (Godot silently drops the value). Use top-level form `set <node> %s '[...]'` instead, or write a scalar via sub-path."
					% [property, top_level])
		var coerced: Dictionary = _coerce_array_to_declared_type(node, top_level, value)
		if coerced.has("error"):
			return coerced
		if coerced.has("value"):
			value = coerced["value"]
	if is_sub_path:
		# sub-path 必须用 set_indexed；node.set() 把整串当字面属性名找不到会 no-op 但返 success。
		node.set_indexed(NodePath(property), value)
	else:
		node.set(property, value)
	return {"success": true}


## 把 JSON Array 按声明类型转成 Variant：Vector2/2i/3/3i/4/4i / Rect2/2i /
## Color / Plane / Quaternion / AABB / Basis / Transform2D/3D / Projection。
## 节点没声明该属性：返 {} 表示"沿用原 value"。
## 声明类型在 match 之外且 ∉ _ARRAY_PASSTHROUGH_SAFE_TYPES：返 {"error": ...} fail-loud，
##   防御未来 Godot 加新 compound Variant 时 silent-corrupt 回归（详见 fallback 注释）。
## 声明类型在 _ARRAY_PASSTHROUGH_SAFE_TYPES（基本类型 / 集合 / Packed*）：返 {} 沿用原 value。
## 转换失败（长度不对 / 元素非数字）：返 {"error": ...} 让调用方 fail-loud。
## 转换成功：返 {"value": <coerced>}。
## *i 变体（Vector2i / Vector3i / Vector4i / Rect2i）允许 float 输入并截断到 int，
## 与 GDScript `Vector2i(1.7, 2.3) → (1, 2)` 构造器行为一致。
##
## Array schema 约定（issue #54，按 axis-vector 顺序——每 N 个元素 = 一个 Vector 轴）：
##   - AABB        : [pos.x, pos.y, pos.z, size.x, size.y, size.z]                      (6 floats)
##   - Basis       : [xaxis.x..z, yaxis.x..z, zaxis.x..z]                                (9 floats)
##   - Transform2D : [xaxis.x, xaxis.y, yaxis.x, yaxis.y, origin.x, origin.y]            (6 floats)
##   - Transform3D : [basis 9 axis-vector, origin.xyz]                                   (12 floats)
##   - Projection  : [xaxis.xyzw, yaxis.xyzw, zaxis.xyzw, waxis.xyzw]                    (16 floats)
## Quaternion / Plane 的 normal 不会自动归一化 —— 调用方传非单位向量后果自负。
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
			return _coerce_numeric_array(arr, [2], "Vector2", property, func(v: Array) -> Variant:
				return Vector2(v[0], v[1]))
		TYPE_VECTOR2I:
			return _coerce_numeric_array(arr, [2], "Vector2i", property, func(v: Array) -> Variant:
				return Vector2i(int(v[0]), int(v[1])))
		TYPE_VECTOR3:
			return _coerce_numeric_array(arr, [3], "Vector3", property, func(v: Array) -> Variant:
				return Vector3(v[0], v[1], v[2]))
		TYPE_VECTOR3I:
			return _coerce_numeric_array(arr, [3], "Vector3i", property, func(v: Array) -> Variant:
				return Vector3i(int(v[0]), int(v[1]), int(v[2])))
		TYPE_VECTOR4:
			return _coerce_numeric_array(arr, [4], "Vector4", property, func(v: Array) -> Variant:
				return Vector4(v[0], v[1], v[2], v[3]))
		TYPE_VECTOR4I:
			return _coerce_numeric_array(arr, [4], "Vector4i", property, func(v: Array) -> Variant:
				return Vector4i(int(v[0]), int(v[1]), int(v[2]), int(v[3])))
		TYPE_RECT2:
			return _coerce_numeric_array(arr, [4], "Rect2", property, func(v: Array) -> Variant:
				return Rect2(v[0], v[1], v[2], v[3]))
		TYPE_RECT2I:
			return _coerce_numeric_array(arr, [4], "Rect2i", property, func(v: Array) -> Variant:
				return Rect2i(int(v[0]), int(v[1]), int(v[2]), int(v[3])))
		TYPE_COLOR:
			# Color 接受 RGB（3）或 RGBA（4）。3-element 时 a 默认 1。
			return _coerce_numeric_array(arr, [3, 4], "Color", property, func(v: Array) -> Variant:
				if v.size() == 3:
					return Color(v[0], v[1], v[2])
				return Color(v[0], v[1], v[2], v[3]))
		TYPE_PLANE:
			# Plane(normal_x, normal_y, normal_z, d) —— 平面方程系数。
			# 注意：normal 不会自动归一化；非单位 normal 会让距离 / 投影计算失真。
			return _coerce_numeric_array(arr, [4], "Plane", property, func(v: Array) -> Variant:
				return Plane(v[0], v[1], v[2], v[3]))
		TYPE_QUATERNION:
			# Quaternion(x, y, z, w) —— 注意 w 在末位，与 Godot ctor 一致。
			# 注意：不会自动归一化；非单位四元数会让旋转 / slerp 失真。
			return _coerce_numeric_array(arr, [4], "Quaternion", property, func(v: Array) -> Variant:
				return Quaternion(v[0], v[1], v[2], v[3]))
		TYPE_AABB:
			# AABB(position, size) —— 6 floats: [pos.xyz, size.xyz]
			return _coerce_numeric_array(arr, [6], "AABB", property, func(v: Array) -> Variant:
				return AABB(Vector3(v[0], v[1], v[2]), Vector3(v[3], v[4], v[5])))
		TYPE_BASIS:
			# Basis(x_axis, y_axis, z_axis) —— 9 floats axis-vector 顺序：
			# v[0..2]=x_axis、v[3..5]=y_axis、v[6..8]=z_axis（每 3 个 = 一个 Basis 轴）。
			return _coerce_numeric_array(arr, [9], "Basis", property, func(v: Array) -> Variant:
				return Basis(
					Vector3(v[0], v[1], v[2]),
					Vector3(v[3], v[4], v[5]),
					Vector3(v[6], v[7], v[8])))
		TYPE_TRANSFORM2D:
			# Transform2D(x_axis, y_axis, origin) —— 6 floats axis-vector 顺序：
			# v[0..1]=x_axis、v[2..3]=y_axis、v[4..5]=origin。
			return _coerce_numeric_array(arr, [6], "Transform2D", property, func(v: Array) -> Variant:
				return Transform2D(
					Vector2(v[0], v[1]),
					Vector2(v[2], v[3]),
					Vector2(v[4], v[5])))
		TYPE_TRANSFORM3D:
			# Transform3D(basis, origin) —— 12 floats: [basis 9 axis-vector 顺序, origin 3]
			return _coerce_numeric_array(arr, [12], "Transform3D", property, func(v: Array) -> Variant:
				return Transform3D(
					Basis(
						Vector3(v[0], v[1], v[2]),
						Vector3(v[3], v[4], v[5]),
						Vector3(v[6], v[7], v[8])),
					Vector3(v[9], v[10], v[11])))
		TYPE_PROJECTION:
			# Projection(x, y, z, w) —— 16 floats axis-vector 顺序：每 4 个 = 一个 Vector4 轴。
			return _coerce_numeric_array(arr, [16], "Projection", property, func(v: Array) -> Variant:
				return Projection(
					Vector4(v[0], v[1], v[2], v[3]),
					Vector4(v[4], v[5], v[6], v[7]),
					Vector4(v[8], v[9], v[10], v[11]),
					Vector4(v[12], v[13], v[14], v[15])))
	# 防御性 fallback：声明类型不是上面的"复合 Variant"也不在已知 passthrough-safe
	# 名单里时，主动 fail-loud。目的是防止未来 Godot 加新 compound Variant（且
	# Object.set 同样 silent-corrupt-on-Array）时重蹈 #52。
	# passthrough-safe = 基本类型 / Object / 集合 / Packed*Array —— 它们要么 Godot
	# Object.set 自己拒收 Array（写入失败但不 silent），要么本就接受 Array 输入。
	if declared_type in _ARRAY_PASSTHROUGH_SAFE_TYPES:
		return {}
	return _err(CliControlErrorCodes.INVALID_PARAMS,
		"value type mismatch for '%s': Array coercion not implemented for declared type %d. If this is a known-safe passthrough, add it to _ARRAY_PASSTHROUGH_SAFE_TYPES; otherwise add a coerce branch."
			% [property, declared_type])


## 校验 Array 长度 / 元素全为数字，OK 则调 ctor 构造目标 Variant。
## `expected_lens` 是允许长度列表（Color 接 [3, 4]，其他类型固定一个长度）。
func _coerce_numeric_array(arr: Array, expected_lens: Array[int], type_name: String, property: String, ctor: Callable) -> Dictionary:
	if not arr.size() in expected_lens:
		return _err(CliControlErrorCodes.INVALID_PARAMS,
			"value type mismatch for '%s': expected %s as numeric array of length %s, got length %d"
				% [property, type_name, _format_length_list(expected_lens), arr.size()])
	if not _is_all_numeric(arr):
		return _err(CliControlErrorCodes.INVALID_PARAMS,
			"value type mismatch for '%s': expected %s as numeric array, got non-numeric element"
				% [property, type_name])
	return {"value": ctor.call(arr)}


func _format_length_list(lens: Array[int]) -> String:
	if lens.size() == 1:
		return str(lens[0])
	var parts: PackedStringArray = []
	for n in lens:
		parts.append(str(n))
	return "[%s]" % " or ".join(parts)


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
	# _BUILD_TREE_MAX_NODES (5000)**：防止恶意 / 失误调用传 max_nodes=999999
	# 时让 _build_tree 真把整棵超大树构造成 Dictionary 后才被外层错误返回丢弃
	# （DoS / OOM 路径）。clamp 后 _build_tree 内部的短路单一来源，
	# 同时承担软上限（agent truncated 信号）与硬墙（防爆 outbound buffer）。
	# 不传时也用硬墙做默认，兼容旧客户端。
	var max_nodes: int = params.get("max_nodes", _BUILD_TREE_MAX_NODES) as int
	if max_nodes <= 0 or max_nodes > _BUILD_TREE_MAX_NODES:
		max_nodes = _BUILD_TREE_MAX_NODES
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
	if counter[0] > _BUILD_TREE_MAX_NODES:
		return _err(
			CliControlErrorCodes.SCENE_TREE_TOO_LARGE,
			"scene tree too large (>%d nodes); lower 'depth' or query a subtree" % _BUILD_TREE_MAX_NODES,
		)
	# 软上限：硬墙内但超过 max_nodes 时附加 truncated 信号让 agent 决定分子树。
	var response: Dictionary = {"tree": tree}
	if counter[0] > max_nodes:
		response["truncated"] = true
		response["total_nodes"] = counter[0]
	return response


func take_screenshot_async() -> Dictionary:
	# dummy renderer (--headless) 下 RenderingServer.frame_post_draw 永不发射，
	# await 会永久挂死。RenderingServer.get_rendering_device() 在 dummy driver
	# 下返回 null，用它检测后改走 process_frame 推进路径。
	# windowed 下循环等到 ready 是 issue #61 的 D 部分：兜底动态 transient
	# （scene 切换 / 窗口 resize 一瞬）。常态下 GameBridge._wait_first_frame_ready
	# 已经保证 client 连上 = viewport 至少画过一帧（H），所以通常第一次就拿到 image。
	# SCREENSHOT_MAX_FRAMES 后仍 null 才报 1006 —— 1006 是 last-resort 兜底，
	# 仍是合法 transient（client 仍应处理）。
	# dummy 路径只试一次：headless 下 viewport texture 永远拿不到 image（无真 GPU），
	# 循环 N 次只会让 Godot 内部 "Parameter t is null" push_error 噪音放大 N 倍。
	var dummy: bool = RenderingServer.get_rendering_device() == null
	var max_iters: int = 1 if dummy else SCREENSHOT_MAX_FRAMES
	var image: Image = null
	for _i in max_iters:
		if dummy:
			await get_tree().process_frame
			await get_tree().process_frame
		else:
			await RenderingServer.frame_post_draw
		image = get_viewport().get_texture().get_image()
		if image != null:
			break
	if image == null:
		# 1006 (transient) ≠ 1003 (schema)：agent 可短重试，等下一帧 viewport 就绪。
		return _err(CliControlErrorCodes.RESOURCE_UNAVAILABLE, "Screenshot unavailable (viewport texture is null)")
	var png_buffer: PackedByteArray = image.save_png_to_buffer()
	var base64_str: String = Marshalls.raw_to_base64(png_buffer)
	return {"image": base64_str}


func _get_node_or_error(params: Dictionary) -> Node:
	var path: String = params.get("path", "") as String
	return get_tree().root.get_node_or_null(path)


## sub-path "position:x" → "position"；无冒号直接返回原值。
## 集中替代三处 `property.split(":", true, 1)[0]` 字面重复（issue #112）。
func _top_level_of(property: String) -> String:
	if ":" in property:
		return property.split(":", true, 1)[0]
	return property


func _node_not_found(path: String) -> Dictionary:
	return _err(CliControlErrorCodes.NODE_NOT_FOUND, "Node not found: %s" % path)


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}


func _has_property(node: Node, property: String) -> bool:
	for prop: Dictionary in node.get_property_list():
		if prop["name"] == property:
			return true
	return false


## depth=0 表示"无限深度"，使用硬限制 _BUILD_TREE_DEFAULT_MAX_DEPTH (50) 防止无限递归。
## counter 是 by-ref 计数器：超过 max_nodes 时短路（不再递归子节点），
## 调用方读 counter[0] > max_nodes 决定是否附加 truncated 信号，
## 读 counter[0] > _BUILD_TREE_MAX_NODES 决定是否走 1005 (SCENE_TREE_TOO_LARGE) 错误路径。
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
	var effective_max: int = _BUILD_TREE_DEFAULT_MAX_DEPTH if max_depth == 0 else max_depth
	if current_depth < effective_max:
		var children: Array[Dictionary] = []
		for child: Node in node.get_children():
			children.append(_build_tree(child, effective_max, current_depth + 1, counter, max_nodes))
		entry["children"] = children
	return entry
