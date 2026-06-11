## GUT 单元测试：LowLevelApi handler 边界
##
## 跑法：在 GUT 已装到 res://addons/gut/ 的项目里执行
##   godot --headless -d -s res://addons/gut/gut_cmdln.gd \
##       -gdir=res://addons/godot_cli_control/tests/gut -gexit
extends GutTest

const LowLevelApiScript := preload("res://addons/godot_cli_control/bridge/low_level_api.gd")

# AABB / Plane / Projection 没有内置 Node 暴露这些类型的属性，用 Node 子类持有
# 带类型声明的 var，`get_property_list()` 会汇报对应 TYPE_*，从而走 coerce 分支。
# Basis / Transform2D / Transform3D 走 Node3D.basis / Node2D.transform /
# Node3D.transform 这些内置属性。
class _CoerceFixture extends Node:
	var test_plane: Plane = Plane()
	var test_aabb: AABB = AABB()
	var test_projection: Projection = Projection()
	# 用于「passthrough-safe 名单」验证：TYPE_ARRAY 不应 fail-loud。
	var test_array: Array = []
	# 用于「未在白名单 / 未在 coerce 名单」验证：自定义 Resource 类型属性
	# 声明类型 = TYPE_OBJECT，passthrough-safe（Object.set 拒收 Array 而非 silent-corrupt）。
	var test_object: Object = null


class _EmitSignalFixture extends Node:
	signal pinged(value)
	var received: Array = []
	func _on_pinged(v) -> void:
		received.append(v)


# issue #112：handler → codec 接缝测试 —— StringName 属性 fixture
class _StringNameFixture extends Node:
	var test_sn: StringName = &"hello"


# issue #119：handler → codec 接缝测试 —— Object 引用属性 fixture
# （自建属性，不依赖 Node.multiplayer 等内置属性的版本演化语义）
class _ObjectFixture extends Node:
	var test_obj: RefCounted = RefCounted.new()


# issue #169：sub-path leaf fail-loud 扩展到封闭复合类型用的 fixture。
# 各类型一条带类型声明的 var（部分无内置 Node 暴露，统一用 fixture var），
# 赋非平凡值便于区分「合法 leaf 读到非 null」与「typo 静默 null」。
# open_dict：开放/动态类型代表（typeof=TYPE_DICTIONARY 永不入封闭集），
# 验证未收录类型仍退回 get_indexed 现状、不误杀。
class _SubPathLeafFixture extends Node:
	var col: Color = Color(0.1, 0.2, 0.3, 0.4)
	var rect2: Rect2 = Rect2(1, 2, 3, 4)
	var rect2i: Rect2i = Rect2i(1, 2, 3, 4)
	var xform2d: Transform2D = Transform2D(Vector2(1, 2), Vector2(3, 4), Vector2(5, 6))
	var xform3d: Transform3D = Transform3D(Basis(Vector3(1, 2, 3), Vector3(4, 5, 6), Vector3(7, 8, 9)), Vector3(10, 11, 12))
	var basis3: Basis = Basis(Vector3(1, 2, 3), Vector3(4, 5, 6), Vector3(7, 8, 9))
	var plane: Plane = Plane(1, 2, 3, 4)
	var quat: Quaternion = Quaternion(0.1, 0.2, 0.3, 0.9)
	var aabb: AABB = AABB(Vector3(1, 2, 3), Vector3(4, 5, 6))
	var proj: Projection = Projection()
	var vec2: Vector2 = Vector2(1, 2)
	var vec2i: Vector2i = Vector2i(1, 2)
	var vec3: Vector3 = Vector3(1, 2, 3)
	var vec3i: Vector3i = Vector3i(1, 2, 3)
	var vec4: Vector4 = Vector4(1, 2, 3, 4)
	var vec4i: Vector4i = Vector4i(1, 2, 3, 4)
	var open_dict: Dictionary = {"foo": 7}


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


func test_get_property_method_name_returns_1002() -> void:
	## #109 守卫：`in` 操作符对方法名也为 true，但 _has_property 的快拒后仍有
	## 线性扫确认——方法名不是属性，必须保持 1002（防止未来把快拒升级成单独判定，
	## 否则 get("get_name") 会读出 Callable，破坏 get-property 严格性）。
	var result: Dictionary = _api.handle_get_property({
		"path": str(_target.get_path()),
		"property": "get_name",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1002)


func test_get_property_category_entry_returns_1002() -> void:
	## #109 顺手修复 pin：get_property_list 里的 category 装饰条目（如 "Node"，
	## usage=PROPERTY_USAGE_CATEGORY）不是真属性。旧版线性扫会误判存在并读出
	## null；`in` 快拒后正确报 1002。
	var result: Dictionary = _api.handle_get_property({
		"path": str(_target.get_path()),
		"property": "Node",
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


func test_set_transform3d_via_array_coerces() -> void:
	# #54 Phase 2：Transform3D(basis, origin) 从 12 floats 构造
	# [axis-major: x_axis 3, y_axis 3, z_axis 3, origin 3]。
	# 用非对称非单位 basis：单位矩阵 / 对角矩阵都无法验证 v[0..2] 真的是 x_axis
	# 还是被错存成 row 0；非对称值（每个 axis 三分量都不同）才能锁住 schema。
	var node: Node3D = Node3D.new()
	node.name = "Transform3DTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "transform",
		"value": [
			1.0,  2.0,  3.0,   # x_axis
			4.0,  5.0,  6.0,   # y_axis
			7.0,  8.0,  9.0,   # z_axis
			10.0, 20.0, 30.0,  # origin
		],
	})
	assert_does_not_have(result, "error", "Transform3D 应从 12-float Array 成功 coerce")
	# 锁 schema：v[0..2] = x_axis、v[3..5] = y_axis、v[6..8] = z_axis、v[9..11] = origin
	assert_eq(node.transform.basis.x, Vector3(1.0, 2.0, 3.0), "x_axis = v[0..2]")
	assert_eq(node.transform.basis.y, Vector3(4.0, 5.0, 6.0), "y_axis = v[3..5]")
	assert_eq(node.transform.basis.z, Vector3(7.0, 8.0, 9.0), "z_axis = v[6..8]")
	assert_eq(node.transform.origin, Vector3(10.0, 20.0, 30.0))


func test_set_aabb_via_array_coerces() -> void:
	# #54 Phase 2：AABB(position, size) 6 floats: [pos.xyz, size.xyz]
	var fixture: Node = _CoerceFixture.new()
	fixture.name = "AABBTarget"
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_set_property({
		"path": str(fixture.get_path()),
		"property": "test_aabb",
		"value": [1.0, 2.0, 3.0, 10.0, 20.0, 30.0],
	})
	assert_does_not_have(result, "error")
	assert_eq(fixture.test_aabb, AABB(Vector3(1.0, 2.0, 3.0), Vector3(10.0, 20.0, 30.0)))


func test_set_basis_via_array_coerces() -> void:
	# #54 Phase 2：Basis(x_axis, y_axis, z_axis) 9 floats axis-major
	# v[0..2] = x_axis、v[3..5] = y_axis、v[6..8] = z_axis。
	# **不用对角值**：对角 / 对称矩阵在 axis-major 和 row-major 下数值相同，
	# 测不出 schema 实现错。用每个轴 3 分量都不同的非对称值。
	var node: Node3D = Node3D.new()
	node.name = "BasisTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "basis",
		"value": [1.0, 2.0, 3.0,   4.0, 5.0, 6.0,   7.0, 8.0, 9.0],
	})
	assert_does_not_have(result, "error")
	assert_eq(node.basis.x, Vector3(1.0, 2.0, 3.0), "x_axis = v[0..2]")
	assert_eq(node.basis.y, Vector3(4.0, 5.0, 6.0), "y_axis = v[3..5]")
	assert_eq(node.basis.z, Vector3(7.0, 8.0, 9.0), "z_axis = v[6..8]")


func test_set_transform2d_via_array_coerces() -> void:
	# #54 Phase 2：Transform2D(x_axis, y_axis, origin) 6 floats
	# v[0..1] = x_axis、v[2..3] = y_axis、v[4..5] = origin。
	# 用非对称非单位值锁 schema：单位 xaxis/yaxis 测不出 ctor 顺序写反。
	var node: Node2D = Node2D.new()
	node.name = "Transform2DTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "transform",
		"value": [1.0, 2.0,   3.0, 4.0,   5.0, 7.0],
	})
	assert_does_not_have(result, "error")
	assert_eq(node.transform.x, Vector2(1.0, 2.0), "x_axis = v[0..1]")
	assert_eq(node.transform.y, Vector2(3.0, 4.0), "y_axis = v[2..3]")
	assert_eq(node.transform.origin, Vector2(5.0, 7.0))


func test_set_projection_via_array_coerces() -> void:
	# #54 Phase 2：Projection(x, y, z, w) 16 floats axis-major（4 个 Vector4 axis）
	# v[0..3] = x_axis、v[4..7] = y_axis、v[8..11] = z_axis、v[12..15] = w_axis。
	# 用非对称非单位值锁 schema：identity 矩阵在 axis-major 和 row-major 下数值相同。
	var fixture: Node = _CoerceFixture.new()
	fixture.name = "ProjTarget"
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_set_property({
		"path": str(fixture.get_path()),
		"property": "test_projection",
		"value": [
			1.0,  2.0,  3.0,  4.0,    # x_axis
			5.0,  6.0,  7.0,  8.0,    # y_axis
			9.0,  10.0, 11.0, 12.0,   # z_axis
			13.0, 14.0, 15.0, 16.0,   # w_axis
		],
	})
	assert_does_not_have(result, "error")
	assert_eq(fixture.test_projection.x, Vector4(1.0, 2.0, 3.0, 4.0), "x_axis = v[0..3]")
	assert_eq(fixture.test_projection.y, Vector4(5.0, 6.0, 7.0, 8.0), "y_axis = v[4..7]")
	assert_eq(fixture.test_projection.z, Vector4(9.0, 10.0, 11.0, 12.0), "z_axis = v[8..11]")
	assert_eq(fixture.test_projection.w, Vector4(13.0, 14.0, 15.0, 16.0), "w_axis = v[12..15]")


func test_set_basis_wrong_length_returns_invalid_params() -> void:
	# Basis 期望 9，给 8：fail-loud。
	var node: Node3D = Node3D.new()
	node.name = "BadLenBasisTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "basis",
		"value": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],  # 8 个
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Basis")


func test_set_transform3d_non_number_element_returns_invalid_params() -> void:
	# Transform3D 期望全 numeric：fail-loud。
	var node: Node3D = Node3D.new()
	node.name = "BadElemTransform3DTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "transform",
		"value": [
			1.0, 0.0, 0.0,
			0.0, 1.0, 0.0,
			0.0, 0.0, "oops",  # 非数字混进 Basis 部分
			10.0, 20.0, 30.0,
		],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Transform3D")


func test_set_aabb_wrong_length_returns_invalid_params() -> void:
	# AABB 期望 6，给 5：fail-loud。
	var fixture: Node = _CoerceFixture.new()
	fixture.name = "BadLenAABBTarget"
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_set_property({
		"path": str(fixture.get_path()),
		"property": "test_aabb",
		"value": [1.0, 2.0, 3.0, 10.0, 20.0],  # 5 个
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "AABB")


func test_set_transform2d_wrong_length_returns_invalid_params() -> void:
	# Transform2D 期望 6，给 4：fail-loud。
	var node: Node2D = Node2D.new()
	node.name = "BadLenTransform2DTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "transform",
		"value": [1.0, 0.0, 0.0, 1.0],  # 4 个
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Transform2D")


func test_set_subpath_array_fails_loud() -> void:
	# Godot 的 Object.set("transform:origin", Array) 会 silent-corrupt：Vector3 leaf 不会
	# 从 Array 隐式构造，origin 仍是 (0,0,0) 但 RPC 返 success（#52 同源 footgun）。
	# 比起静默坏掉，handle_set_property 直接 fail-loud，让 agent 改用 top-level Array
	# 形式（`set <node> transform '[...]'`，#54 已覆盖所有复合 Variant 的整体写入）。
	# sub-path 仅用于标量赋值（`position:x 1.8`），不收 Array。
	var node: Node3D = Node3D.new()
	node.name = "SubpathArrayTarget"
	add_child_autofree(node)
	var original: Transform3D = node.transform
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "transform:origin",
		"value": [10.0, 20.0, 30.0],
	})
	assert_has(result, "error", "sub-path + Array 必须 fail-loud，不能 silent fall-through")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "sub-path")
	# 原值未被改动
	assert_eq(node.transform, original)


func test_set_array_declared_passthrough_does_not_fail_loud() -> void:
	# TYPE_ARRAY 在 _ARRAY_PASSTHROUGH_SAFE_TYPES 名单里：Array 直接透传给
	# Object.set 是合法的（属性本身就是 Array）。验证防御性 fallback 不会
	# 误伤这类合法 passthrough。
	var fixture: Node = _CoerceFixture.new()
	fixture.name = "ArrayPassthroughTarget"
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_set_property({
		"path": str(fixture.get_path()),
		"property": "test_array",
		"value": [1, "two", 3.0],
	})
	assert_does_not_have(result, "error", "TYPE_ARRAY 属性写 Array 不能 fail-loud")
	assert_eq(fixture.test_array, [1, "two", 3.0])


func test_set_subpath_scalar_still_works() -> void:
	# 控制组：sub-path + 标量（不是 Array）必须沿用原路径。fail-loud 只针对 Array。
	var node: Node2D = Node2D.new()
	node.name = "SubpathScalarTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()),
		"property": "position:x",
		"value": 42.5,
	})
	assert_does_not_have(result, "error", "sub-path + 标量必须 OK")
	assert_eq(node.position.x, 42.5)


func test_set_projection_non_number_element_returns_invalid_params() -> void:
	# Projection 期望全 numeric：fail-loud。
	var fixture: Node = _CoerceFixture.new()
	fixture.name = "BadElemProjTarget"
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_set_property({
		"path": str(fixture.get_path()),
		"property": "test_projection",
		"value": [
			1.0, 0.0, 0.0, 0.0,
			0.0, 1.0, 0.0, 0.0,
			0.0, 0.0, 1.0, 0.0,
			0.0, 0.0, 0.0, "nope",  # 非数字
		],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_string_contains(str(result.error.message), "Projection")


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
	var fixture: Node = _CoerceFixture.new()
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
	var fixture: Node = _CoerceFixture.new()
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
	var counter: Array[int] = [LowLevelApiScript._BUILD_TREE_MAX_NODES]
	# 第 5 参数 max_nodes = LIMIT（5000）；leaf 计入后 counter == LIMIT+1 > max_nodes，触发短路
	var entry: Dictionary = _api._build_tree(leaf, 5, 0, counter, LowLevelApiScript._BUILD_TREE_MAX_NODES)
	# leaf 自身被计入 → counter 变成 LIMIT+1
	assert_eq(int(counter[0]), LowLevelApiScript._BUILD_TREE_MAX_NODES + 1)
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
	var entry: Dictionary = _api._build_tree(parent, 5, 0, counter, LowLevelApiScript._BUILD_TREE_MAX_NODES)
	assert_has(entry, "children")
	assert_eq((entry.children as Array).size(), 1)


func test_handle_get_scene_tree_clamps_oversized_max_nodes() -> void:
	# P1 回归：恶意/失误客户端传 max_nodes=999999 时，
	# 入口必须 clamp 到 _BUILD_TREE_MAX_NODES，
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


func test_handle_get_scene_tree_with_path_returns_subtree() -> void:
	# issue #150：传 path 时以该节点为子树根（从 /root 解析，与 children 同世界观）。
	# _target 是 before_each 挂在测试树下的节点，其 get_path() 是 /root 起的绝对路径，
	# 不在 current_scene 之下——正好验证 autoload-on-/root 那类兄弟子树也能取到。
	var result: Dictionary = _api.handle_get_scene_tree({
		"path": str(_target.get_path()),
		"depth": 3,
	})
	assert_does_not_have(result, "error")
	assert_has(result, "tree")
	assert_eq(str(result.tree.name), "GutTestTarget")


func test_handle_get_scene_tree_bad_path_returns_node_not_found() -> void:
	# issue #150：path 不存在 → 复用 1001 NODE_NOT_FOUND，与 children 一致，不新增码。
	var result: Dictionary = _api.handle_get_scene_tree({
		"path": "/root/DefinitelyDoesNotExist_150",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)  # CliControlErrorCodes.NODE_NOT_FOUND


func test_handle_get_scene_tree_without_path_keeps_current_scene_root() -> void:
	# issue #150 回归：不传 path 时默认根维持 current_scene（fallback root），行为不变。
	var result: Dictionary = _api.handle_get_scene_tree({"depth": 2})
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


# ── take_screenshot_async（issue #61 D 部分）──────────────────────
# GUT 跑在 --headless 下，RenderingServer.get_rendering_device() == null，
# 走 dummy 推帧路径。这两条测试主要防 await 死锁回归（早期版本一次没等
# 到 ready 就报 1006，绕过 H 启动 gate 后会再撞）。

func test_screenshot_returns_image_or_1006_under_headless() -> void:
	# headless 下 viewport texture 可能是 null（dummy 无真实 RenderingDevice）。
	# 合法结果：要么拿到 base64 image，要么循环跑满后报 1006。
	# 关键不变量：函数必须返回（不死锁）、不抛、payload 形状正确。
	var result: Dictionary = await _api.take_screenshot_async()
	assert_true(result.has("image") or result.has("error"),
		"take_screenshot_async must return image or error envelope")
	if result.has("error"):
		var err: Dictionary = result["error"] as Dictionary
		assert_eq(int(err.get("code")), 1006,
			"headless null viewport must report RESOURCE_UNAVAILABLE not other code")


func test_screenshot_max_frames_constant_is_positive() -> void:
	# 防回归：常量被改成 0 或负数会让循环立刻退出 → 等同未修。
	assert_gt(LowLevelApiScript.SCREENSHOT_MAX_FRAMES, 0)


# ── issue #99：get 编码 + sub-path 读 ──

func test_get_property_encodes_vector2_with_type() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(1.5, -2.0)
	var result: Dictionary = _api.handle_get_property({"path": str(node.get_path()), "property": "position"})
	assert_eq(result.get("value"), [1.5, -2.0])
	assert_eq(result.get("type"), "Vector2")


func test_get_property_primitive_has_no_type_field() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.visible = false
	var result: Dictionary = _api.handle_get_property({"path": str(node.get_path()), "property": "visible"})
	assert_eq(result, {"value": false})


func test_get_property_sub_path_reads_leaf() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(7.0, 9.0)
	var result: Dictionary = _api.handle_get_property({"path": str(node.get_path()), "property": "position:x"})
	assert_eq(result, {"value": 7.0})


func test_get_property_sub_path_bogus_top_level_is_1002() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_property({"path": str(node.get_path()), "property": "nope:x"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1002)


func test_get_set_round_trip_vector2() -> void:
	# round-trip 闭环：get 输出的数组原样灌回 set，再 get 等值
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(3.25, -4.5)
	var got: Dictionary = _api.handle_get_property({"path": str(node.get_path()), "property": "position"})
	var set_result: Dictionary = _api.handle_set_property({
		"path": str(node.get_path()), "property": "position", "value": got["value"],
	})
	assert_false(set_result.has("error"))
	var got2: Dictionary = _api.handle_get_property({"path": str(node.get_path()), "property": "position"})
	assert_eq(got2["value"], got["value"])


# ── issue #100：get_properties 同帧原子读 ──

func test_get_properties_returns_all_encoded() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(1.0, 2.0)
	node.visible = true
	var result: Dictionary = _api.handle_get_properties({
		"path": str(node.get_path()), "properties": ["position", "visible", "position:y"],
	})
	assert_false(result.has("error"))
	var values: Dictionary = result["values"]
	assert_eq(values["position"], {"value": [1.0, 2.0], "type": "Vector2"})
	assert_eq(values["visible"], {"value": true})
	assert_eq(values["position:y"], {"value": 2.0})


func test_get_properties_missing_prop_fails_atomically_naming_all() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_properties({
		"path": str(node.get_path()), "properties": ["position", "nope1", "nope2"],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1002)
	assert_string_contains(str(result.error.message), "nope1")
	assert_string_contains(str(result.error.message), "nope2")


# ── issue #112：handle_get_property → codec 接缝测试（Object / StringName） ──

func test_get_property_object_type_returns_string_type_field() -> void:
	## handle_get_property 读 Object 属性 → codec 编码为 {"value": "<str>", "type": "Object"}
	## 检验 handler → codec 全链路（codec 单测已覆盖 encode，这里测 handler 调 codec 的接缝）。
	## 用自建 fixture 持有 Object 引用属性（issue #119：不依赖内置 multiplayer
	## 属性的版本演化语义；内容断言只锚类名，不锚 str(obj) 的实例 id 部分）。
	var fixture := _ObjectFixture.new()
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_get_property({
		"path": str(fixture.get_path()), "property": "test_obj",
	})
	assert_does_not_have(result, "error", "test_obj 是合法 Object 属性，不应报错")
	assert_eq(result.get("type"), "Object", "Object 类型属性必须带 type=Object 字段")
	assert_true(result.get("value") is String, "Object 编码应是字符串（str(obj)）")
	# 内容断言锚类名（str(obj) 形如 <RefCounted#id>），不锚实例 id 部分
	assert_string_contains(str(result.get("value")), "RefCounted")


func test_get_property_stringname_type_returns_string_type_field() -> void:
	## handle_get_property 读 StringName 属性 → codec 编码为 {"value": "<str>", "type": "StringName"}
	## 内置属性在 GDScript 暴露面里都是 String（如 Node.name），用自建 fixture 持有 StringName。
	var fixture := _StringNameFixture.new()
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_get_property({
		"path": str(fixture.get_path()), "property": "test_sn",
	})
	assert_does_not_have(result, "error", "test_sn 是合法 StringName 属性，不应报错")
	assert_eq(result.get("type"), "StringName", "StringName 属性必须带 type=StringName 字段")
	assert_true(result.get("value") is String, "StringName 编码应转为普通 String")
	assert_eq(result.get("value"), "hello", "StringName 值应正确编码为原始字串")


func test_get_properties_rejects_empty_or_non_string() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	for bad: Variant in [[], null, [1], [""]]:
		var result: Dictionary = _api.handle_get_properties({
			"path": str(node.get_path()), "properties": bad,
		})
		assert_has(result, "error", "properties=%s 应报错" % [bad])
		assert_eq(int(result.error.code), -32602)


# ── get_properties 边界：sub-path 缺失项 + 重复属性（review 遗留）──

func test_get_properties_missing_sub_path_names_full_key() -> void:
	# 缺失项是 sub-path 形式时，1002 error message 必须点全名（含 ":"）。
	# 回归：早期实现只校验 top-level 名，message 里丢掉了 sub-path 后半部分，
	# 让 agent 无法直接从 message 得知是哪个完整 key 出了问题。
	var node := Node2D.new()
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_properties({
		"path": str(node.get_path()),
		"properties": ["position", "nope:x"],
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1002)
	# message 必须含完整的 "nope:x"，而不是只有 "nope"
	assert_string_contains(str(result.error.message), "nope:x")


func test_get_properties_duplicate_property_deduped_by_dict() -> void:
	# 重复属性（["position", "position"]）行为锁定：Dictionary key 语义去重，
	# values 里 "position" 只有一个 entry（有意行为，和 JSON Dict 的 key-uniqueness 对齐）。
	# 注意：这是锁定已有行为，而非新增功能——改动此断言前请确认变更是有意而为之。
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(5.0, 6.0)
	var result: Dictionary = _api.handle_get_properties({
		"path": str(node.get_path()),
		"properties": ["position", "position"],
	})
	assert_false(result.has("error"), "重复属性不应报错")
	var values: Dictionary = result["values"]
	# Dictionary 赋值重复 key 只保留最后一次，故 values 恰好有 1 个 key
	assert_eq(values.size(), 1, "重复 key 在 Dictionary 语义下去重为 1 条（有意行为）")


# ── handle_find_nodes（issue #153：服务端节点搜索）──────────────────
# 测试统一加 "from": _target 子树作用域 —— GUT runner 自带 GUI（按钮/标签一堆），
# 不 scope 的话 type/text 过滤会撞 runner 自己的控件，断言不可确定。

## 程序化 UI fixture：匿名节点（add_child 自动起 @Button@N 名）+ 无 owner
## （代码创建的节点 owner == null，正是 issue #153 的核心场景——
## Node.find_children(owned=true) 找不到它们）。
func _build_find_fixture() -> void:
	var panel: Control = Control.new()
	panel.name = "Panel"
	_target.add_child(panel)
	var start_btn: Button = Button.new()  # 匿名：进树后名字形如 @Button@N
	start_btn.text = "开始游戏"
	panel.add_child(start_btn, false)
	var quit_btn: Button = Button.new()
	quit_btn.text = "退出"
	panel.add_child(quit_btn, false)
	var title: Label = Label.new()
	title.name = "Title"
	title.text = "开始"
	_target.add_child(title)
	var inventory: Control = Control.new()
	inventory.name = "InventoryPanel"
	_target.add_child(inventory)


func test_find_nodes_by_type_matches_subclasses() -> void:
	_build_find_fixture()
	# BaseButton 是 Button 的父类：type 过滤必须按继承匹配（is_class 语义），
	# 否则 agent 得逐个猜具体子类。
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "type": "BaseButton",
	})
	assert_does_not_have(result, "error")
	var matches: Array = result.get("matches", [])
	assert_eq(matches.size(), 2, "两个 Button 都应命中 BaseButton 过滤")
	for m: Dictionary in matches:
		assert_eq(str(m.get("type")), "Button")


func test_find_nodes_text_exact_vs_contains() -> void:
	_build_find_fixture()
	# 精确档：text == "开始" 只命中 Title（"开始游戏" 不算）
	var exact: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "text": "开始",
	})
	assert_does_not_have(exact, "error")
	var exact_matches: Array = exact.get("matches", [])
	assert_eq(exact_matches.size(), 1)
	assert_eq(str((exact_matches[0] as Dictionary).get("name")), "Title")
	# 子串档：text_contains "开始" 命中 Title + 开始游戏按钮
	var sub: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "text_contains": "开始",
	})
	assert_does_not_have(sub, "error")
	assert_eq((sub.get("matches", []) as Array).size(), 2)


func test_find_nodes_finds_anonymous_unowned_nodes() -> void:
	# issue #153 核心场景：代码创建的匿名节点（@Button@N、owner==null）
	# 必须能按 text 定位到，且 path 可直接用于后续 click。
	_build_find_fixture()
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "text": "开始游戏",
	})
	assert_does_not_have(result, "error")
	var matches: Array = result.get("matches", [])
	assert_eq(matches.size(), 1)
	var entry: Dictionary = matches[0] as Dictionary
	# 匿名自动名形如 @Button@N
	assert_string_contains(str(entry.get("name")), "@Button@")
	# 返回的 path 必须真实可解析（agent 拿去 click）
	var found: Node = get_tree().root.get_node_or_null(str(entry.get("path")))
	assert_not_null(found)
	assert_eq(str(found.get("text")), "开始游戏")


func test_find_nodes_name_pattern_wildcard() -> void:
	_build_find_fixture()
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "name_pattern": "Inventory*",
	})
	assert_does_not_have(result, "error")
	var matches: Array = result.get("matches", [])
	assert_eq(matches.size(), 1)
	assert_eq(str((matches[0] as Dictionary).get("name")), "InventoryPanel")


func test_find_nodes_combined_filters_are_and_semantics() -> void:
	_build_find_fixture()
	# type=Label + text_contains=开始 → 只有 Title（开始游戏按钮被 type 滤掉）
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()),
		"type": "Label", "text_contains": "开始",
	})
	var matches: Array = result.get("matches", [])
	assert_eq(matches.size(), 1)
	assert_eq(str((matches[0] as Dictionary).get("name")), "Title")


func test_find_nodes_limit_attaches_truncated_signal() -> void:
	_build_find_fixture()
	# 4 个 Control 系节点（Panel/2×Button/InventoryPanel… Button 也是 Control），
	# limit=2 → 恰好 2 条 matches + truncated:true（复用 tree 截断风格）
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "type": "Control", "limit": 2,
	})
	assert_does_not_have(result, "error")
	assert_eq((result.get("matches", []) as Array).size(), 2)
	assert_true(result.get("truncated", false) == true, "超 limit 必须附 truncated 信号")
	# 控制组：limit 足够时无 truncated 字段
	var all_result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "type": "Control", "limit": 100,
	})
	assert_does_not_have(all_result, "truncated")


func test_find_nodes_bfs_returns_shallow_matches_first() -> void:
	_build_find_fixture()
	# BFS 浅层优先：_target 直下的 Label(Title) 应排在 Panel 下两个 Button 之前
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "type": "Control",
	})
	var matches: Array = result.get("matches", [])
	assert_gt(matches.size(), 2)
	var first_paths: Array[String] = []
	for m: Dictionary in matches:
		first_paths.append(str(m.get("path")))
	# 深层节点（Panel/@Button@N）不得出现在浅层（_target 直下）之前
	var panel_child_idx: int = -1
	var direct_child_idx: int = -1
	for i in range(first_paths.size()):
		if "@Button@" in first_paths[i] and panel_child_idx == -1:
			panel_child_idx = i
		if first_paths[i].ends_with("/InventoryPanel"):
			direct_child_idx = i
	assert_true(direct_child_idx != -1 and panel_child_idx != -1)
	assert_lt(direct_child_idx, panel_child_idx, "浅层匹配必须排在深层之前（BFS 序）")


func test_find_nodes_entry_carries_text_and_visible() -> void:
	_build_find_fixture()
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()), "text": "开始",
	})
	var entry: Dictionary = (result.get("matches", []) as Array)[0] as Dictionary
	# entry 字段形状与 tree 的 _build_tree 对齐：name/type/path + text + visible
	assert_eq(str(entry.get("text")), "开始")
	assert_has(entry, "visible")
	assert_has(entry, "path")
	assert_has(entry, "type")


func test_find_nodes_no_filter_is_invalid_params() -> void:
	# 全空过滤器 = tree 的活，find 必须拒绝（-32602），与 CLI preflight 对齐
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()),
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_find_nodes_text_modes_mutually_exclusive() -> void:
	var result: Dictionary = _api.handle_find_nodes({
		"from": str(_target.get_path()),
		"text": "a", "text_contains": "b",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_find_nodes_missing_from_reports_1001() -> void:
	var result: Dictionary = _api.handle_find_nodes({
		"from": "/root/__no_such_node__", "type": "Button",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)


func test_find_nodes_from_scopes_search_to_subtree() -> void:
	_build_find_fixture()
	# from=Panel 时只搜 Panel 子树：Title（_target 直下）不应出现
	var panel_path: String = str(_target.get_path()) + "/Panel"
	var result: Dictionary = _api.handle_find_nodes({
		"from": panel_path, "text_contains": "开始",
	})
	var matches: Array = result.get("matches", [])
	assert_eq(matches.size(), 1, "Panel 子树内只有 开始游戏 按钮含 开始")
	assert_string_contains(str((matches[0] as Dictionary).get("name")), "@Button@")


# ── _mark_screenshot_stale_check (#156 子问题 B / B3) ────────────────

func test_stale_check_first_call_not_stale() -> void:
	var buf := PackedByteArray([1, 2, 3])
	assert_false(_api._mark_screenshot_stale_check(buf), "首张截图永不 stale")

func test_stale_check_same_bytes_is_stale() -> void:
	_api._mark_screenshot_stale_check(PackedByteArray([1, 2, 3]))  # 第一张，记录
	assert_true(_api._mark_screenshot_stale_check(PackedByteArray([1, 2, 3])), "独立对象、相同字节 → stale_suspect")

func test_stale_check_different_bytes_not_stale() -> void:
	_api._mark_screenshot_stale_check(PackedByteArray([1, 2, 3]))
	assert_false(_api._mark_screenshot_stale_check(PackedByteArray([4, 5, 6])), "不同字节 → 非 stale")


# ── sub-path leaf fail-loud（#157 vector 系 + #169 封闭复合类型）──────

# #169 实证锚点：每个封闭复合 Variant 的「宽松候选超集」（文档成员 + 派生访问器 +
# 常见 typo），keyed by typeof()，值 = [fixture var 名, 候选 leaf 列表]。parity 测试
# 用它重跑 discovery：候选里 get_indexed 读到非 null 的子集 = 该类型真合法集，必须与
# _SUBPATH_CLOSED_LEAVES 逐字相等（防漏列误杀、防多列漏 typo、防 Godot 升级后漂移）。
func _leaf_probes() -> Dictionary:
	return {
		TYPE_VECTOR2: ["vec2", ["x", "y", "z", "w"]],
		TYPE_VECTOR2I: ["vec2i", ["x", "y", "z", "w"]],
		TYPE_VECTOR3: ["vec3", ["x", "y", "z", "w"]],
		TYPE_VECTOR3I: ["vec3i", ["x", "y", "z", "w"]],
		TYPE_VECTOR4: ["vec4", ["x", "y", "z", "w", "v"]],
		TYPE_VECTOR4I: ["vec4i", ["x", "y", "z", "w", "v"]],
		TYPE_COLOR: ["col", ["r", "g", "b", "a", "r8", "g8", "b8", "a8", "h", "s", "v",
			"ok_hsl_h", "ok_hsl_s", "ok_hsl_l", "luminance", "r16", "alpha", "red",
			"rr", "value", "hue", "saturation"]],
		TYPE_RECT2: ["rect2", ["position", "size", "end", "x", "y", "origin", "start",
			"p", "s", "center", "area", "w", "h", "width", "height"]],
		TYPE_RECT2I: ["rect2i", ["position", "size", "end", "x", "y", "origin", "start",
			"p", "s", "center", "area", "w", "h", "width", "height"]],
		TYPE_TRANSFORM2D: ["xform2d", ["x", "y", "origin", "z", "w", "basis", "o",
			"columns", "rows", "a", "b", "c", "d"]],
		TYPE_TRANSFORM3D: ["xform3d", ["basis", "origin", "x", "y", "z", "w", "o", "b"]],
		TYPE_BASIS: ["basis3", ["x", "y", "z", "w", "origin", "rows", "columns", "scale", "a"]],
		TYPE_PLANE: ["plane", ["x", "y", "z", "d", "normal", "w", "origin", "distance", "n"]],
		TYPE_QUATERNION: ["quat", ["x", "y", "z", "w", "d", "normal", "i", "j", "k"]],
		TYPE_AABB: ["aabb", ["position", "size", "end", "x", "y", "z", "origin", "start",
			"center", "volume", "p", "s"]],
		TYPE_PROJECTION: ["proj", ["x", "y", "z", "w", "origin", "columns", "rows", "a"]],
	}


func _sorted_strings(src: Array) -> Array:
	var out: Array = []
	for item in src:
		out.append(str(item))
	out.sort()
	return out


func test_subpath_closed_leaves_match_godot_get_indexed_members() -> void:
	# 实证 parity（双向）：
	#   1. _leaf_probes() 里声明的每个「意图封闭」类型，必须真被登记进 _SUBPATH_CLOSED_LEAVES
	#      （漏加即红——也是新增类型的 RED 驱动）。
	#   2. 对每个登记类型，用 get_indexed 重跑 discovery，断言「Godot 实际接受的 leaf 集」==
	#      「白名单登记集」。漏列 → 误杀合法读取；多列 → 漏掉 typo；Godot 升级改成员 → 先炸。
	#   3. 白名单里的每个类型都必须有 parity 候选超集兜底，否则覆盖有盲区。
	var fixture := _SubPathLeafFixture.new()
	fixture.name = "LeafParity"
	add_child_autofree(fixture)
	var probes: Dictionary = _leaf_probes()
	var closed: Dictionary = _api._SUBPATH_CLOSED_LEAVES
	for t: int in probes:
		assert_true(closed.has(t),
			"typeof=%d 已在 _leaf_probes 声明为封闭类型，但未登记进 _SUBPATH_CLOSED_LEAVES" % t)
		if not closed.has(t):
			continue
		var var_name: String = probes[t][0]
		var candidates: Array = probes[t][1]
		var empirical: Array = []
		for leaf in candidates:
			var v: Variant = fixture.get_indexed(NodePath("%s:%s" % [var_name, leaf]))
			if v != null:
				empirical.append(leaf)
		assert_eq(_sorted_strings(empirical), _sorted_strings(closed[t]),
			"typeof=%d 白名单 leaf 集与 Godot get_indexed 实际成员不一致" % t)
	for t2: int in closed:
		assert_true(probes.has(t2),
			"typeof=%d 在 _SUBPATH_CLOSED_LEAVES 里但缺 parity 候选超集（_leaf_probes 补一行）" % t2)


func test_subpath_closed_leaves_all_read_through_production_path() -> void:
	# 无误杀守卫：白名单里登记的每个 leaf，走生产路径（handle_get_property）必须读到值、
	# 不报 error。覆盖嵌套复合 leaf（rect2:position=Vector2 / xform3d:basis=Basis 等），
	# 确认 codec 编码也不炸。
	var fixture := _SubPathLeafFixture.new()
	fixture.name = "LeafReadThrough"
	add_child_autofree(fixture)
	var probes: Dictionary = _leaf_probes()
	var closed: Dictionary = _api._SUBPATH_CLOSED_LEAVES
	for t: int in closed:
		var var_name: String = probes[t][0]
		for leaf: String in closed[t]:
			var result: Dictionary = _api.handle_get_property({
				"path": str(fixture.get_path()),
				"property": "%s:%s" % [var_name, leaf],
			})
			assert_does_not_have(result, "error",
				"合法 leaf '%s:%s' 被误杀（typeof=%d）" % [var_name, leaf, t])


func test_subpath_typo_rejected_for_each_closed_compound() -> void:
	# 对每个意图封闭的复合类型，一个绝不合法的 leaf（"zzz_nope"）走生产路径必须 → 1002，
	# 且 message 带合法 leaf 列表（agent 据此自纠）。遍历 _leaf_probes()（意图集）→
	# 类型未登记时 typo 会静默 null、本测试红，构成新增类型的 RED 驱动。
	var fixture := _SubPathLeafFixture.new()
	fixture.name = "LeafTypo"
	add_child_autofree(fixture)
	var probes: Dictionary = _leaf_probes()
	for t: int in probes:
		var var_name: String = probes[t][0]
		var result: Dictionary = _api.handle_get_property({
			"path": str(fixture.get_path()),
			"property": "%s:zzz_nope" % var_name,
		})
		assert_has(result, "error", "typeof=%d typo leaf 必须 fail-loud" % t)
		if result.has("error"):
			assert_eq(int(result["error"]["code"]), CliControlErrorCodes.PROPERTY_NOT_FOUND,
				"typeof=%d typo 必须报 1002" % t)
			assert_string_contains(str(result["error"]["message"]), "valid leaves:")


func test_subpath_color_modulate_valid_and_typo() -> void:
	# #169 headline：Color 经真实内置属性 Node2D.modulate（不只 fixture var）。
	# 合法 `modulate:a` 读到 alpha；typo `modulate:zzz` → 1002 且列出合法 leaf。
	var node := Node2D.new()
	node.name = "SubPathModulate"
	node.modulate = Color(0.1, 0.2, 0.3, 0.4)
	add_child_autofree(node)
	var ok: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()), "property": "modulate:a",
	})
	assert_does_not_have(ok, "error")
	assert_almost_eq(ok.get("value"), 0.4, 0.0001)
	var typo: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()), "property": "modulate:zzz",
	})
	assert_has(typo, "error")
	if typo.has("error"):
		assert_eq(int(typo["error"]["code"]), CliControlErrorCodes.PROPERTY_NOT_FOUND)
		assert_string_contains(str(typo["error"]["message"]), "valid leaves: r, g, b, a")


func test_subpath_valid_vector2_leaf_reads_scalar() -> void:
	var node := Node2D.new()
	node.name = "SubPathV2"
	node.position = Vector2(3, 7)
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()),
		"property": "position:y",
	})
	assert_does_not_have(result, "error")
	assert_eq(result.get("value"), 7.0)


func test_subpath_typo_vector2_leaf_returns_1002_with_valid_list() -> void:
	var node := Node2D.new()
	node.name = "SubPathV2Typo"
	node.position = Vector2(3, 7)
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()),
		"property": "position:z",  # Vector2 无 z
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.PROPERTY_NOT_FOUND)
	assert_string_contains(result["error"]["message"], "valid leaves: x, y")


func test_subpath_typo_vector3_leaf_returns_1002() -> void:
	var node := Node3D.new()
	node.name = "SubPathV3Typo"
	node.position = Vector3(1, 2, 3)
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()),
		"property": "position:w",  # Vector3 无 w
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.PROPERTY_NOT_FOUND)


func test_subpath_uncovered_compound_type_passes_through() -> void:
	# 开放/动态类型（Dictionary 永不入封闭集，key 任意无法枚举）→ 退回 get_indexed
	# 现状：合法 sub-path 仍读到值，不误杀。#169 后 Color 等已纳入，故改用 Dictionary
	# 作「未收录类型」代表，守住「误杀比静默更坏」的放行契约。
	var fixture := _SubPathLeafFixture.new()
	fixture.name = "SubPathOpenDict"
	add_child_autofree(fixture)
	var result: Dictionary = _api.handle_get_property({
		"path": str(fixture.get_path()),
		"property": "open_dict:foo",
	})
	assert_does_not_have(result, "error")
	assert_eq(result.get("value"), 7)


# ── emit-signal opt-in 逃生门（#157 item4）──────────────────────

func test_emit_signal_disabled_by_default_returns_1015() -> void:
	var node := Node.new()
	node.name = "EmitTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_emit_signal({
		"path": str(node.get_path()), "signal": "ready", "args": [],
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.EMIT_SIGNAL_DISABLED)


func test_emit_signal_allowed_emits_with_args() -> void:
	var node := _EmitSignalFixture.new()
	node.name = "EmitFixture"
	node.pinged.connect(node._on_pinged)
	add_child_autofree(node)
	_api._emit_signal_allowed = true
	var result: Dictionary = _api.handle_emit_signal({
		"path": str(node.get_path()), "signal": "pinged", "args": [42],
	})
	assert_does_not_have(result, "error")
	assert_eq(result.get("emitted"), true)
	assert_eq(node.received, [42])


func test_emit_signal_allowed_unknown_signal_returns_1007() -> void:
	var node := Node.new()
	node.name = "EmitTarget2"
	add_child_autofree(node)
	_api._emit_signal_allowed = true
	var result: Dictionary = _api.handle_emit_signal({
		"path": str(node.get_path()), "signal": "no_such_signal", "args": [],
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.SIGNAL_NOT_FOUND)


func test_call_emit_signal_still_blocked_when_opted_in() -> void:
	# 回归守卫：opt-in 只放开 emit-signal 子命令；通用 call 面 emit_signal 仍黑。
	var node := Node.new()
	node.name = "EmitTarget3"
	add_child_autofree(node)
	_api._emit_signal_allowed = true
	var result: Dictionary = _api.handle_call_method({
		"path": str(node.get_path()), "method": "emit_signal", "args": ["ready"],
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.INVALID_PARAMS)
