class_name CliControlVariantCodec
extends RefCounted
## Variant → JSON-safe 编码（issue #99）。
##
## 与 set 侧 low_level_api.gd::_coerce_array_to_declared_type 的 array schema
## **完全对称**（axis-vector 顺序，见该函数 docstring 的 schema 表）：
## get 输出的 value 数组可原样灌回 set，round-trip 闭环。
## Color 不对称提示：set 接受 3 或 4 元，这里恒输出 4 元（4 灌 4 成立）。
##
## 全函数：任何 Variant 都有确定输出、不报错——杜绝「响应永远不发出」类挂死。
## type 字段只在 encode() 顶层出现；嵌套（Array/Dictionary 内）复合类型由
## encode_value() 递归编码为数组但不带 type（spec 声明的取舍）。

const _COMPOUND_TYPE_NAMES: Dictionary = {
	TYPE_VECTOR2: "Vector2", TYPE_VECTOR2I: "Vector2i",
	TYPE_VECTOR3: "Vector3", TYPE_VECTOR3I: "Vector3i",
	TYPE_VECTOR4: "Vector4", TYPE_VECTOR4I: "Vector4i",
	TYPE_RECT2: "Rect2", TYPE_RECT2I: "Rect2i",
	TYPE_COLOR: "Color", TYPE_PLANE: "Plane",
	TYPE_QUATERNION: "Quaternion", TYPE_AABB: "AABB",
	TYPE_BASIS: "Basis", TYPE_TRANSFORM2D: "Transform2D",
	TYPE_TRANSFORM3D: "Transform3D", TYPE_PROJECTION: "Projection",
}

# 递归编码深度上限：防病态深树 / 自引用容器爆栈挂死 daemon
# （与 low_level_api.gd _BUILD_TREE_DEFAULT_MAX_DEPTH 同源精神）。
# 超限降级为哨兵字符串而非报错——保住「全函数」承诺。
const _MAX_ENCODE_DEPTH: int = 64
const _DEPTH_SENTINEL: String = "<max-depth-exceeded>"


## 顶层编码：返回 {"value": <json-safe>}，复合类型 / StringName / NodePath /
## Object 额外带 "type" 字段。
## Object / StringName / NodePath 是诊断性单向编码（不参与 round-trip 闭环），
## 与 Vector/Color/Array 等复合类型的无损 round-trip 严格区分。
## 深度上限：容器嵌套超过 _MAX_ENCODE_DEPTH 层时以哨兵字符串降级，保住全函数承诺。
static func encode(v: Variant) -> Dictionary:
	var t: int = typeof(v)
	if _COMPOUND_TYPE_NAMES.has(t):
		return {"value": encode_value(v), "type": _COMPOUND_TYPE_NAMES[t]}
	match t:
		TYPE_STRING_NAME:
			return {"value": String(v), "type": "StringName"}
		TYPE_NODE_PATH:
			return {"value": String(v), "type": "NodePath"}
		TYPE_OBJECT:
			return {"value": str(v), "type": "Object"}
	return {"value": encode_value(v)}


## 递归值编码（无 type 信息）。JSON 原生类型原样；复合 → set-schema 数组；
## 非有限 float → "inf"/"-inf"/"nan" 字符串（JSON.stringify 对 inf/nan 产非法 JSON）。
## depth 超过 _MAX_ENCODE_DEPTH 时返回哨兵字符串，不报错——保住全函数承诺。
static func encode_value(v: Variant, depth: int = 0) -> Variant:
	if depth > _MAX_ENCODE_DEPTH:
		return _DEPTH_SENTINEL
	match typeof(v):
		TYPE_FLOAT:
			return _f(v)
		TYPE_VECTOR2:
			return [_f(v.x), _f(v.y)]
		TYPE_VECTOR2I:
			return [v.x, v.y]
		TYPE_VECTOR3:
			return [_f(v.x), _f(v.y), _f(v.z)]
		TYPE_VECTOR3I:
			return [v.x, v.y, v.z]
		TYPE_VECTOR4:
			return [_f(v.x), _f(v.y), _f(v.z), _f(v.w)]
		TYPE_VECTOR4I:
			return [v.x, v.y, v.z, v.w]
		TYPE_RECT2:
			return [_f(v.position.x), _f(v.position.y), _f(v.size.x), _f(v.size.y)]
		TYPE_RECT2I:
			return [v.position.x, v.position.y, v.size.x, v.size.y]
		TYPE_COLOR:
			return [_f(v.r), _f(v.g), _f(v.b), _f(v.a)]
		TYPE_PLANE:
			return [_f(v.normal.x), _f(v.normal.y), _f(v.normal.z), _f(v.d)]
		TYPE_QUATERNION:
			return [_f(v.x), _f(v.y), _f(v.z), _f(v.w)]
		TYPE_AABB:
			return [
				_f(v.position.x), _f(v.position.y), _f(v.position.z),
				_f(v.size.x), _f(v.size.y), _f(v.size.z),
			]
		TYPE_BASIS:
			return _basis_to_array(v)
		TYPE_TRANSFORM2D:
			return [_f(v.x.x), _f(v.x.y), _f(v.y.x), _f(v.y.y), _f(v.origin.x), _f(v.origin.y)]
		TYPE_TRANSFORM3D:
			var t3_out: Array = _basis_to_array(v.basis)
			t3_out.append_array([_f(v.origin.x), _f(v.origin.y), _f(v.origin.z)])
			return t3_out
		TYPE_PROJECTION:
			return [
				_f(v.x.x), _f(v.x.y), _f(v.x.z), _f(v.x.w),
				_f(v.y.x), _f(v.y.y), _f(v.y.z), _f(v.y.w),
				_f(v.z.x), _f(v.z.y), _f(v.z.z), _f(v.z.w),
				_f(v.w.x), _f(v.w.y), _f(v.w.z), _f(v.w.w),
			]
		TYPE_STRING_NAME, TYPE_NODE_PATH:
			return String(v)
		TYPE_OBJECT:
			return str(v)
		TYPE_ARRAY:
			var arr_out: Array = []
			for item: Variant in (v as Array):
				arr_out.append(encode_value(item, depth + 1))
			return arr_out
		TYPE_DICTIONARY:
			var dict_out: Dictionary = {}
			var dict_in: Dictionary = v as Dictionary
			for key: Variant in dict_in:
				dict_out[str(key)] = encode_value(dict_in[key], depth + 1)
			return dict_out
		TYPE_PACKED_BYTE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY, TYPE_PACKED_STRING_ARRAY:
			return Array(v)
		TYPE_PACKED_FLOAT32_ARRAY, TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_VECTOR2_ARRAY, TYPE_PACKED_VECTOR3_ARRAY, TYPE_PACKED_COLOR_ARRAY, TYPE_PACKED_VECTOR4_ARRAY:
			var packed_out: Array = []
			for item: Variant in v:
				packed_out.append(encode_value(item, depth + 1))
			return packed_out
	return v  # bool / int / String / null


## axis-vector 顺序：x/y/z 轴各 3 floats，与 set 侧 Basis(x_axis, y_axis, z_axis) 一致
static func _basis_to_array(b: Basis) -> Array:
	return [
		_f(b.x.x), _f(b.x.y), _f(b.x.z),
		_f(b.y.x), _f(b.y.y), _f(b.y.z),
		_f(b.z.x), _f(b.z.y), _f(b.z.z),
	]


static func _f(f: float) -> Variant:
	if is_nan(f):
		return "nan"
	if is_inf(f):
		return "inf" if f > 0.0 else "-inf"
	return f
