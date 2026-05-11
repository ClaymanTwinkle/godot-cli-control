## GUT 单元测试：LowLevelApi handler 边界
##
## 跑法：在 GUT 已装到 res://addons/gut/ 的项目里执行
##   godot --headless -d -s res://addons/gut/gut_cmdln.gd \
##       -gdir=res://addons/godot_cli_control/tests/gut -gexit
extends GutTest

const LowLevelApiScript := preload("res://addons/godot_cli_control/bridge/low_level_api.gd")

# Plane 没有内置 Node 暴露这个属性，用一个 Node 子类持有 `var test_plane: Plane`，
# `get_property_list()` 会汇报它的声明类型 = TYPE_PLANE，从而走 coerce 分支。
class _PlaneFixture extends Node:
	var test_plane: Plane = Plane()


var _api: Node
var _target: Node


func before_each() -> void:
	_api = LowLevelApiScript.new()
	_api.name = "LowLevelApi"
	add_child_autofree(_api)
	_target = Node.new()
	_target.name = "GutTestTarget"
	add_child_autofree(_target)


# ── handle_get_property ───────────────────────────────────────────

func test_get_property_returns_value() -> void:
	var result: Dictionary = _api.handle_get_property({
		"path": str(_target.get_path()),
		"property": "name",
	})
	assert_does_not_have(result, "error")
	assert_eq(str(result.get("value")), "GutTestTarget")


func test_get_property_nonexistent_returns_1002() -> void:
	var result: Dictionary = _api.handle_get_property({
		"path": str(_target.get_path()),
		"property": "no_such_property_xyz",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1002)


func test_get_property_missing_param_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_get_property({
		"path": str(_target.get_path()),
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_get_property_node_not_found_returns_1001() -> void:
	var result: Dictionary = _api.handle_get_property({
		"path": "/root/__definitely_does_not_exist__",
		"property": "name",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)


# ── handle_set_property blacklist ─────────────────────────────────

func test_set_property_script_blocked() -> void:
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "script",
		"value": null,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Blocked property")


func test_set_property_resource_blocked() -> void:
	# Resource 注入向量（绕过 script ban 的次级路径）也必须挡住
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "texture",
		"value": null,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


# #22：NodePath 子属性（"script:source_code"）不能绕过 top-level 黑名单
func test_set_property_nested_script_blocked() -> void:
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "script:source_code",
		"value": "extends Node\nfunc _init():\n  pass",
	})
	assert_has(result, "error", "script:xxx 必须被 top-level 黑名单挡住")
	assert_eq(int(result.error.code), -32602)


func test_set_property_nested_resource_blocked() -> void:
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "texture:resource_path",
		"value": "res://anything.png",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_set_property_non_blacklisted_nested_still_works() -> void:
	# 控制组：name:length 这类无害嵌套（不存在的子路径）应正常往下走
	# 不在黑名单里。Godot 自身可能拒绝赋值，但黑名单不应提前误挡。
	# 验证不会被 -32602 Blocked 挡，至少能通过校验阶段。
	var result: Dictionary = _api.handle_set_property({
		"path": str(_target.get_path()),
		"property": "name",  # name 本身不在黑名单
		"value": "RenamedTarget",
	})
	# name 是合法可写属性
	assert_does_not_have(result, "error")


# ── handle_set_property: Array → Vector/Color/Rect type coercion (#52) ─
#
# Issue #52: JSON 只能产 Array，但 Godot Object.set("zoom", [1.8,1.8]) 走
# 隐式构造失败路径 → 值变成 Vector2(0,0) 或被 clamp 到 0.00001，
# 然而 RPC 返 {success: true}，agent 看不到错误。
# 服务端必须查声明类型把 Array 转成 Vector2/Vector3/Vector4/Rect2/Color。

func test_set_vector2_via_array_coerces() -> void:
	var node: Node2D = Node2D.new()
	node.name = "Vec2Target"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "position",
		"value": [123.5, -42.0],
	})
	assert_does_not_have(result, "error", "set Vector2 via Array 不应返 error")
	assert_eq(node.position, Vector2(123.5, -42.0), "Vector2 应等于 Array 值，不是 (0,0)")


func test_set_vector2_zoom_via_array_coerces() -> void:
	# issue #52 中的具体场景：Camera2D.zoom 拒收 Vector2(0,0)
	var cam: Camera2D = Camera2D.new()
	cam.name = "ZoomTarget"
	add_child_autofree(cam)
	var result: Dictionary = _api.handle_set_property({
		"path": str(cam.get_path()),
		"property": "zoom",
		"value": [1.8, 1.8],
	})
	assert_does_not_have(result, "error")
	assert_eq(cam.zoom, Vector2(1.8, 1.8))


func test_set_vector3_via_array_coerces() -> void:
	var node: Node3D = Node3D.new()
	node.name = "Vec3Target"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "position",
		"value": [1.0, 2.0, 3.0],
	})
	assert_does_not_have(result, "error")
	assert_eq(node.position, Vector3(1.0, 2.0, 3.0))


func test_set_color_via_rgba_array_coerces() -> void:
	var rect: ColorRect = ColorRect.new()
	rect.name = "ColorTarget"
	add_child_autofree(rect)
	var result: Dictionary = _api.handle_set_property({
		"path": str(rect.get_path()),
		"property": "color",
		"value": [1.0, 0.0, 1.0, 1.0],
	})
	assert_does_not_have(result, "error")
	assert_eq(rect.color, Color(1.0, 0.0, 1.0, 1.0))


func test_set_color_via_rgb_array_coerces() -> void:
	# 3 元素 RGB（无 alpha）→ Color(r, g, b, 1.0)
	var rect: ColorRect = ColorRect.new()
	rect.name = "Color3Target"
	add_child_autofree(rect)
	var result: Dictionary = _api.handle_set_property({
		"path": str(rect.get_path()),
		"property": "color",
		"value": [0.25, 0.5, 0.75],
	})
	assert_does_not_have(result, "error")
	assert_eq(rect.color, Color(0.25, 0.5, 0.75, 1.0))


func test_set_rect2_via_array_coerces() -> void:
	# Sprite2D.region_rect 是 Rect2。Array [x, y, w, h] → Rect2
	var spr: Sprite2D = Sprite2D.new()
	spr.name = "RectTarget"
	add_child_autofree(spr)
	var result: Dictionary = _api.handle_set_property({
		"path": str(spr.get_path()),
		"property": "region_rect",
		"value": [1.0, 2.0, 30.0, 40.0],
	})
	assert_does_not_have(result, "error")
	assert_eq(spr.region_rect, Rect2(1.0, 2.0, 30.0, 40.0))


func test_set_vector2_wrong_length_returns_invalid_params() -> void:
	# Array 长度不匹配声明类型：必须 fail-loud，而不是 silent corruption。
	var node: Node2D = Node2D.new()
	node.name = "BadLenTarget"
	add_child_autofree(node)
	var original: Vector2 = node.position
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "position",
		"value": [1.0, 2.0, 3.0],  # Vector2 期望长度 2，给了 3
	})
	assert_has(result, "error", "长度不匹配必须返 error，不能 silent")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Vector2")
	# 没改值
	assert_eq(node.position, original)


func test_set_vector2_non_number_element_returns_invalid_params() -> void:
	# Array 元素不是数字：fail-loud
	var node: Node2D = Node2D.new()
	node.name = "BadElemTarget"
	add_child_autofree(node)
	var original: Vector2 = node.position
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "position",
		"value": ["not", "numbers"],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_eq(node.position, original)


func test_set_string_property_unaffected_by_coerce() -> void:
	# 控制组：声明 String 类型的 name 属性走原路径，没回归。
	var node: Node2D = Node2D.new()
	node.name = "StrTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "name",
		"value": "Renamed",
	})
	assert_does_not_have(result, "error")
	assert_eq(str(node.name), "Renamed")


func test_set_float_property_unaffected_by_coerce() -> void:
	# 控制组：声明 float 类型走原路径。
	var node: Node2D = Node2D.new()
	node.name = "FloatTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "rotation",
		"value": 1.5,
	})
	assert_does_not_have(result, "error")
	assert_almost_eq(node.rotation, 1.5, 0.0001)


func test_set_transform3d_via_array_fails_loud() -> void:
	# 复合 Variant（Transform3D/Quaternion/Basis/...）暂未实现 Array → typed 转换。
	# 不实现是一回事，让请求 silent fall-through 到 Object.set 重蹈 #52 是另一回事 ——
	# 这条锁住「未实现 = fail-loud，绝不静默坏值」契约。
	var node: Node3D = Node3D.new()
	node.name = "Transform3DTarget"
	add_child_autofree(node)
	var original: Transform3D = node.transform
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "transform",
		"value": [1.0, 2.0, 3.0, 4.0],  # 任意 Array：Transform3D 不收 Array
	})
	assert_has(result, "error", "复合 Variant + Array 必须 fail-loud，不能 silent fall-through")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Array coercion not supported")
	# 没改值
	assert_eq(node.transform, original)


func test_set_quaternion_via_array_coerces() -> void:
	# #54 Phase 1：Quaternion(x, y, z, w) 4-float 构造，从 #53 的 fail-loud
	# 名单提升到 coerce 名单。
	var node: Node3D = Node3D.new()
	node.name = "QuatTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "quaternion",
		"value": [0.0, 0.7071068, 0.0, 0.7071068],  # 绕 Y 轴 90°
	})
	assert_does_not_have(result, "error", "Quaternion 应当从 Array 成功 coerce")
	# 浮点 compare：Quaternion(0, 0.707, 0, 0.707) 期望分量
	assert_almost_eq(node.quaternion.x, 0.0, 0.0001)
	assert_almost_eq(node.quaternion.y, 0.7071068, 0.0001)
	assert_almost_eq(node.quaternion.z, 0.0, 0.0001)
	assert_almost_eq(node.quaternion.w, 0.7071068, 0.0001)


func test_set_plane_via_array_coerces() -> void:
	# #54 Phase 1：Plane(a, b, c, d) 平面方程，4-float 构造。
	var fixture: Node = _PlaneFixture.new()
	fixture.name = "PlaneTarget"
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_set_property({
		"path": str(fixture.get_path()),
		"property": "test_plane",
		"value": [1.0, 0.0, 0.0, 5.0],  # x = 5 平面
	})
	assert_does_not_have(result, "error", "Plane 应当从 Array 成功 coerce")
	var p: Plane = fixture.test_plane
	assert_almost_eq(p.normal.x, 1.0, 0.0001)
	assert_almost_eq(p.normal.y, 0.0, 0.0001)
	assert_almost_eq(p.normal.z, 0.0, 0.0001)
	assert_almost_eq(p.d, 5.0, 0.0001)


func test_set_plane_wrong_length_returns_invalid_params() -> void:
	# Plane 期望长度 4，给 3：fail-loud 不能 silent。
	var fixture: Node = _PlaneFixture.new()
	fixture.name = "BadLenPlaneTarget"
	add_child_autofree(fixture)
	var original: Plane = fixture.test_plane
	var result: Dictionary = _api.handle_set_property({
		"path": str(fixture.get_path()),
		"property": "test_plane",
		"value": [1.0, 0.0, 0.0],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Plane")
	# 没改值
	assert_eq(fixture.test_plane, original)


func test_set_quaternion_non_number_element_returns_invalid_params() -> void:
	# Quaternion 期望全 numeric，给字符串：fail-loud。
	var node: Node3D = Node3D.new()
	node.name = "BadElemQuatTarget"
	add_child_autofree(node)
	var original: Quaternion = node.quaternion
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "quaternion",
		"value": ["a", "b", "c", "d"],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Quaternion")
	assert_eq(node.quaternion, original)


# ── handle_call_method blacklist ──────────────────────────────────

func test_call_method_queue_free_blocked() -> void:
	var result: Dictionary = _api.handle_call_method({
		"path": str(_target.get_path()),
		"method": "queue_free",
		"args": [],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_call_method_reflection_set_blocked() -> void:
	# `set` 是反射类入口，可绕过 PROPERTY_BLACKLIST，必须 ban
	var result: Dictionary = _api.handle_call_method({
		"path": str(_target.get_path()),
		"method": "set",
		"args": ["script", null],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_call_method_nonexistent_returns_1003() -> void:
	var result: Dictionary = _api.handle_call_method({
		"path": str(_target.get_path()),
		"method": "no_such_method_xyz",
		"args": [],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1003)


# ── _build_tree 节点总数上限（防 outbound buffer 超限） ───────────────

func test_build_tree_short_circuits_above_node_limit() -> void:
	# 不打满 5000+ 节点（慢且与场景耦合）：预填 counter 到上限，
	# 验证 _build_tree 递归一层后短路、返回的 entry 不含 children。
	var leaf: Node = Node.new()
	leaf.name = "Leaf"
	add_child_autofree(leaf)
	var counter: Array[int] = [LowLevelApiScript._BUILD_TREE_NODE_LIMIT]
	# 第 5 参数 max_nodes = LIMIT（5000）；leaf 计入后 counter == LIMIT+1 > max_nodes，触发短路
	var entry: Dictionary = _api._build_tree(leaf, 5, 0, counter, LowLevelApiScript._BUILD_TREE_NODE_LIMIT)
	# leaf 自身被计入 → counter 变成 LIMIT+1
	assert_eq(int(counter[0]), LowLevelApiScript._BUILD_TREE_NODE_LIMIT + 1)
	# 超 limit 后立刻 return，不下递归 children
	assert_does_not_have(entry, "children")


func test_build_tree_under_limit_includes_children() -> void:
	# 控制组：counter 远未到上限时正常带 children
	var parent: Node = Node.new()
	parent.name = "Parent"
	add_child_autofree(parent)
	var c1: Node = Node.new()
	c1.name = "C1"
	parent.add_child(c1)
	var counter: Array[int] = [0]
	# 第 5 参数 max_nodes 给硬墙 5000，远超 2 个节点，不会触发软截断
	var entry: Dictionary = _api._build_tree(parent, 5, 0, counter, LowLevelApiScript._BUILD_TREE_NODE_LIMIT)
	assert_has(entry, "children")
	assert_eq((entry.children as Array).size(), 1)


func test_handle_get_scene_tree_clamps_oversized_max_nodes() -> void:
	# P1 回归：恶意/失误客户端传 max_nodes=999999 时，
	# 入口必须 clamp 到 _BUILD_TREE_NODE_LIMIT，
	# 否则 _build_tree 会构造完整大字典再被外层 1005 丢弃（DoS 风险）。
	# 这里 happy path 验证 clamp 不破坏正常调用——简单场景下无 1005 报错、无 truncated 信号。
	var result: Dictionary = _api.handle_get_scene_tree({
		"depth": 3,
		"max_nodes": 999999,
	})
	assert_does_not_have(result, "error")
	assert_has(result, "tree")
	# 简单场景节点数远小于 LIMIT，clamp 后等价于 max_nodes=LIMIT，不应触发 truncated
	assert_does_not_have(result, "truncated")


func test_handle_get_scene_tree_negative_max_nodes_falls_back_to_limit() -> void:
	# 客户端传 0 / 负值 → 当作 "用服务端默认"，等价于不传
	var result: Dictionary = _api.handle_get_scene_tree({
		"depth": 3,
		"max_nodes": -5,
	})
	assert_does_not_have(result, "error")
	assert_has(result, "tree")


# ── handle_node_exists / handle_get_children ──────────────────────

func test_node_exists_true_for_real_path() -> void:
	var result: Dictionary = _api.handle_node_exists({
		"path": str(_target.get_path()),
	})
	assert_true(result.get("exists"))


func test_node_exists_false_for_missing_path() -> void:
	var result: Dictionary = _api.handle_node_exists({
		"path": "/root/__missing__",
	})
	assert_false(result.get("exists"))
