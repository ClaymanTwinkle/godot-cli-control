## GUT：CliControlVariantCodec —— Variant → JSON-safe 编码（issue #99）
## 关键不变量：编码输出与 set 侧 _coerce_array_to_declared_type 的 array schema
## 完全对称（axis-vector 顺序），get 输出可原样灌回 set。
extends GutTest

const Codec := preload("res://addons/godot_cli_control/bridge/variant_codec.gd")


func test_primitives_pass_through_without_type() -> void:
	assert_eq(Codec.encode(true), {"value": true})
	assert_eq(Codec.encode(42), {"value": 42})
	assert_eq(Codec.encode(1.5), {"value": 1.5})
	assert_eq(Codec.encode("hi"), {"value": "hi"})
	assert_eq(Codec.encode(null), {"value": null})


func test_compound_types_table() -> void:
	# [输入 Variant, 期望 value, 期望 type]
	var cases: Array = [
		[Vector2(1.5, -2.0), [1.5, -2.0], "Vector2"],
		[Vector2i(1, 2), [1, 2], "Vector2i"],
		[Vector3(1, 2, 3), [1.0, 2.0, 3.0], "Vector3"],
		[Vector3i(1, 2, 3), [1, 2, 3], "Vector3i"],
		[Vector4(1, 2, 3, 4), [1.0, 2.0, 3.0, 4.0], "Vector4"],
		[Vector4i(1, 2, 3, 4), [1, 2, 3, 4], "Vector4i"],
		[Rect2(1, 2, 3, 4), [1.0, 2.0, 3.0, 4.0], "Rect2"],
		[Rect2i(1, 2, 3, 4), [1, 2, 3, 4], "Rect2i"],
		[Color(0.1, 0.2, 0.3), [Color(0.1, 0.2, 0.3).r, Color(0.1, 0.2, 0.3).g, Color(0.1, 0.2, 0.3).b, 1.0], "Color"],
		[Plane(0, 1, 0, 5), [0.0, 1.0, 0.0, 5.0], "Plane"],
		[Quaternion(0, 0, 0, 1), [0.0, 0.0, 0.0, 1.0], "Quaternion"],
		[AABB(Vector3(1, 2, 3), Vector3(4, 5, 6)), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], "AABB"],
		[Basis.IDENTITY, [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], "Basis"],
		[Transform2D.IDENTITY, [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], "Transform2D"],
		[Transform3D.IDENTITY, [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0], "Transform3D"],
		[Projection.IDENTITY, [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0], "Projection"],
	]
	for case: Array in cases:
		var encoded: Dictionary = Codec.encode(case[0])
		assert_eq(encoded.get("type"), case[2], "type for %s" % [case[0]])
		assert_eq(encoded.get("value"), case[1], "value for %s" % [case[0]])


func test_string_name_node_path_object() -> void:
	assert_eq(Codec.encode(&"sn"), {"value": "sn", "type": "StringName"})
	assert_eq(Codec.encode(NodePath("/root/A")), {"value": "/root/A", "type": "NodePath"})
	var node := Node.new()
	var encoded: Dictionary = Codec.encode(node)
	assert_eq(encoded.get("type"), "Object")
	assert_true(encoded.get("value") is String)
	node.free()


func test_nested_compound_encodes_without_type() -> void:
	# 嵌套复合类型：递归编码为数组但不带 type（spec 已声明的取舍）
	var encoded: Dictionary = Codec.encode([Vector2(1, 2), {"p": Vector3(3, 4, 5)}])
	assert_false(encoded.has("type"))
	assert_eq(encoded["value"], [[1.0, 2.0], {"p": [3.0, 4.0, 5.0]}])


func test_non_finite_floats_become_strings() -> void:
	assert_eq(Codec.encode(INF), {"value": "inf"})
	assert_eq(Codec.encode(-INF), {"value": "-inf"})
	assert_eq(Codec.encode(NAN), {"value": "nan"})
	assert_eq(Codec.encode(Vector2(INF, 1.0)), {"value": ["inf", 1.0], "type": "Vector2"})


func test_packed_arrays_become_plain_arrays() -> void:
	assert_eq(Codec.encode(PackedInt32Array([1, 2])), {"value": [1, 2]})
	assert_eq(Codec.encode(PackedFloat64Array([1.5])), {"value": [1.5]})
	assert_eq(Codec.encode(PackedStringArray(["a"])), {"value": ["a"]})
	assert_eq(
		Codec.encode(PackedVector2Array([Vector2(1, 2)])),
		{"value": [[1.0, 2.0]]}
	)


func test_deep_nesting_truncates_at_max_depth() -> void:
	# 超过 _MAX_ENCODE_DEPTH 的深树降级为哨兵，不爆栈
	var deep: Array = []
	var cursor: Array = deep
	for _i in 100:
		var inner: Array = []
		cursor.append(inner)
		cursor = inner
	cursor.append(42)
	var encoded: Dictionary = Codec.encode(deep)
	assert_true(JSON.stringify(encoded["value"]).contains("<max-depth-exceeded>"))


func test_self_referential_array_terminates() -> void:
	var a: Array = [1]
	a.append(a)
	var encoded: Dictionary = Codec.encode(a)
	assert_true(JSON.stringify(encoded["value"]).contains("<max-depth-exceeded>"))
	a.clear()  # 解开自引用，避免 Godot 退出时 ObjectDB 泄漏警告


func test_self_referential_dictionary_terminates() -> void:
	var d: Dictionary = {"x": 1}
	d["self"] = d
	var encoded: Dictionary = Codec.encode(d)
	assert_true(JSON.stringify(encoded["value"]).contains("<max-depth-exceeded>"))
	d.clear()  # 解开自引用，避免 Godot 退出时 ObjectDB 泄漏警告


func test_packed_byte_array_minor() -> void:
	assert_eq(Codec.encode(PackedByteArray([1, 2])), {"value": [1, 2]})


func test_packed_int64_array_minor() -> void:
	assert_eq(Codec.encode(PackedInt64Array([9])), {"value": [9]})


func test_int_key_dictionary_minor() -> void:
	assert_eq(Codec.encode({1: "a"}), {"value": {"1": "a"}})
