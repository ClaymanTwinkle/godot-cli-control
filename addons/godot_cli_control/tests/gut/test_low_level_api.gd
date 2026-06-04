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


# ── issue #96：wait_frames ──

func test_wait_frames_advances_n_process_frames() -> void:
	var start: int = Engine.get_process_frames()
	var result: Dictionary = await _api.wait_frames_async({"frames": 3})
	assert_eq(result, {"success": true, "frames": 3})
	assert_gte(Engine.get_process_frames() - start, 3)


func test_wait_frames_physics_mode() -> void:
	var start: int = Engine.get_physics_frames()
	var result: Dictionary = await _api.wait_frames_async({"frames": 2, "physics": true})
	assert_eq(result, {"success": true, "frames": 2})
	assert_gte(Engine.get_physics_frames() - start, 2)


func test_wait_frames_rejects_bad_input() -> void:
	for bad: Variant in [null, 0, -1, 3601, "x"]:
		var result: Dictionary = await _api.wait_frames_async({"frames": bad})
		assert_has(result, "error", "frames=%s 应报错" % [bad])
		assert_eq(int(result.error.code), -32602)
