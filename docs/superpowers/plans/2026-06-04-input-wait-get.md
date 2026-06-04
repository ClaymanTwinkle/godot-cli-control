# 输入事件管线 + get 编码/原子读 + 条件等待原语 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按依赖序交付三个 PR：#97（press/tap/hold/combo 走 `Input.parse_input_event` 进事件管线）、#99+#100（Variant→JSON 编码器 + `get_properties` 同帧原子读）、#96（wait-prop / wait-signal / wait-frames 条件等待原语）。

**Architecture:** GDScript 侧新增 `variant_codec.gd`（静态编码器，与 set 侧 array schema 对称）+ `low_level_api.gd` 三个 async wait handler；Python 侧按 CLAUDE.md 标准流程（client → bridge → cli RpcSpec → GDScript handler → SKILL.md/README）逐层加薄包装。spec 见 `docs/superpowers/specs/2026-06-04-input-wait-get-design.md`（已批准，**实现遇到歧义以 spec 为准**）。

**Tech Stack:** Godot 4 GDScript（GUT 测试）、Python ≥3.10（pytest + pytest-asyncio，覆盖率门槛 80%，必须 `coverage run -m pytest` 不能 `pytest --cov`）。

---

## 全局约定（每个 Task 都适用）

- **跑测试一律委托 subagent（model: sonnet）**，主会话只收精简结论（全局 CLAUDE.md 规则）。
- pytest 从 `python/` 目录跑：`cd python && python -m pytest tests/<file>::<test> -v`（单测迭代）；全量+覆盖率：`cd python && coverage run -m pytest -q && coverage report | tail -3`。
- GUT 全量：`GODOT_BIN=<godot路径> python addons/godot_cli_control/tests/run_gut.py`（`which godot` 找不到时用 `GODOT_BIN` env 或 macOS .app 路径）。
- gh 命令必须绕过本地代理：`env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY gh ...`，且 Bash 调用需禁沙箱。
- GDScript 缩进用 **Tab**（与现有文件一致）；Python 遵守 ruff 基线（E4/E7/E9/F）。
- 分支策略：**stacked**——`feat/97-input-event-pipeline` ← main；`feat/99-100-get-codec` ← PR1 分支；`feat/96-wait-primitives` ← PR2 分支。前序 PR 合并后，后续分支 rebase 到 main 再 retarget。
- commit message 末尾带 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

## Phase 0：预备

- [ ] **Step 0.1: 提交遗留的 README 修改**

工作区有未提交的 `README.md` 改动（补 `GODOT_CLI_LONG_OP_TIMEOUT` + config.json 文档缺口，本计划之前的收尾活）。在 main 上单独提交：

```bash
git add README.md
git commit -m "docs(readme): 补 GODOT_CLI_LONG_OP_TIMEOUT 与 config.json idle_timeout 文档缺口"
```

- [ ] **Step 0.2: 建 PR1 分支**

```bash
git checkout -b feat/97-input-event-pipeline
```

---

# Phase 1（PR1）：#97 输入注入走事件管线

**Files:**
- Modify: `addons/godot_cli_control/bridge/input_simulation_api.gd:271-280`（`_do_press` / `_do_release`）
- Test: `addons/godot_cli_control/tests/gut/test_input_simulation_api.gd`（追加）
- Test: `python/tests/test_e2e_input.py`（追加）
- Docs: `python/godot_cli_control/templates/skill/SKILL.md`、`addons/godot_cli_control/README.md`、`addons/godot_cli_control/CHANGELOG.md`

### Task 1.1: GUT — 事件管线可见性（TDD）

- [ ] **Step 1: 写失败测试**

在 `test_input_simulation_api.gd` 末尾追加（注意 Tab 缩进；`EventProbe` 是文件级 inner class，放在所有 func 之前、const 之后）：

```gdscript
## 事件管线探针：记录 _input / _unhandled_input 收到的 InputEventAction（issue #97）
class EventProbe:
	extends Node
	var input_actions: Array = []
	var unhandled_actions: Array = []

	func _input(event: InputEvent) -> void:
		if event is InputEventAction:
			input_actions.append(event)

	func _unhandled_input(event: InputEvent) -> void:
		if event is InputEventAction:
			unhandled_actions.append(event)
```

测试函数（追加到文件末尾）：

```gdscript
# ── issue #97：press/release 必须走事件管线（_input / _unhandled_input 可见） ──

func test_press_feeds_input_event_pipeline() -> void:
	var probe := EventProbe.new()
	add_child_autofree(probe)
	_api.handle_action_press({"action": "ui_accept"})
	# parse_input_event 注入的事件在输入泵分发；等两帧确保送达
	await wait_frames(2)
	assert_gt(probe.input_actions.size(), 0, "_input 应收到 InputEventAction")
	var ev: InputEventAction = probe.input_actions[0]
	assert_eq(String(ev.action), "ui_accept")
	assert_true(ev.pressed, "press 注入的事件应为 pressed=true")
	_api.handle_action_release({"action": "ui_accept"})


func test_release_feeds_release_event() -> void:
	var probe := EventProbe.new()
	add_child_autofree(probe)
	_api.handle_action_press({"action": "ui_accept"})
	await wait_frames(2)
	probe.input_actions.clear()
	_api.handle_action_release({"action": "ui_accept"})
	await wait_frames(2)
	assert_gt(probe.input_actions.size(), 0, "release 也应产生事件")
	var ev: InputEventAction = probe.input_actions[0]
	assert_false(ev.pressed, "release 注入的事件应为 pressed=false")


func test_press_still_updates_polling_state() -> void:
	# 轮询路径不回归：parse_input_event(InputEventAction) 同样更新 action 状态位
	_api.handle_action_press({"action": "ui_accept"})
	Input.flush_buffered_events()
	assert_true(Input.is_action_pressed("ui_accept"), "is_action_pressed 应感知 press")
	_api.handle_action_release({"action": "ui_accept"})
	Input.flush_buffered_events()
	assert_false(Input.is_action_pressed("ui_accept"), "release 后应清除")
```

> 备注：GUT 的 `wait_frames` 是 GutTest 自带帧等待助手，与本计划 PR3 新增的 RPC 同名但无关。
> 若 headless 下 `_input` 收不到事件（平台差异），先用 `--gui` 本机验证一次行为成立，
> 再按实际情况调整等待帧数——**不许改成只测轮询状态绕过事件断言**。

- [ ] **Step 2: 跑测试确认失败**（subagent）

```bash
GODOT_BIN=$(command -v godot || echo "$GODOT_BIN") python addons/godot_cli_control/tests/run_gut.py
```

预期：新增 3 个测试中 `test_press_feeds_input_event_pipeline` / `test_release_feeds_release_event` FAIL（事件数为 0——当前 `Input.action_press` 不产生事件）；`test_press_still_updates_polling_state` 可能 PASS（action_press 本来就翻状态位）。

- [ ] **Step 3: 实现**

`input_simulation_api.gd` 替换 `_do_press` / `_do_release`（约 273-280 行）：

```gdscript
# ── 底层输入操作 ──

## issue #97：走 Input.parse_input_event 而非 Input.action_press——
## InputEventAction 经输入泵进 SceneTree 事件管线（_input / _unhandled_input
## 可见），同时仍更新 action 状态位（is_action_pressed / get_vector 不回归）。
## 注意：InputEventAction 无坐标，依赖鼠标位置的 _gui_input 控件请用 click。
func _do_press(action: String) -> void:
	if not InputMap.has_action(action):
		return
	var ev: InputEventAction = InputEventAction.new()
	ev.action = action
	ev.pressed = true
	ev.strength = 1.0
	Input.parse_input_event(ev)


func _do_release(action: String) -> void:
	if not InputMap.has_action(action):
		return
	var ev: InputEventAction = InputEventAction.new()
	ev.action = action
	ev.pressed = false
	Input.parse_input_event(ev)
```

- [ ] **Step 4: 跑 GUT 全量确认通过**（subagent）

同 Step 2 命令。预期：新增 3 个测试 PASS，**且原有全部测试 0 失败**（重点盯 combo / hold / release_all 状态机用例——它们读内部 dict 不读 Input 状态，理论不受影响）。

- [ ] **Step 5: Commit**

```bash
git add addons/godot_cli_control/bridge/input_simulation_api.gd addons/godot_cli_control/tests/gut/test_input_simulation_api.gd
git commit -m "feat(input): press/release 走 Input.parse_input_event，事件管线可见（#97）"
```

### Task 1.2: e2e — 事件回调式游戏真实链路

- [ ] **Step 1: 写失败 e2e**

`python/tests/test_e2e_input.py`：先读文件现有 project fixture 的构造方式（`_PROJECT_GODOT` / `_MAIN_TSCN` + tmp project 装配 + daemon start/stop 模式），按同样模式追加一个**带事件探针脚本**的测试。新增模块级常量：

```python
_EVENT_PROBE_GD = """\
extends Node

var saw_jump_event: bool = false

func _unhandled_input(event: InputEvent) -> void:
    if event is InputEventAction and event.action == "jump" and event.is_pressed():
        saw_jump_event = true
"""

_MAIN_WITH_PROBE_TSCN = """\
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://probe.gd" id="1"]

[node name="Main" type="Node"]
script = ExtResource("1")
"""
```

测试（复用文件内既有的 daemon 启停 helper / fixture；下面以现有测试同款结构写，执行时对齐实际 helper 名）：

```python
def test_press_reaches_unhandled_input(tmp_path: Path) -> None:
    """issue #97：事件回调式游戏（_unhandled_input）必须能看到 press 注入的事件。"""
    project = _make_project(tmp_path, main_tscn=_MAIN_WITH_PROBE_TSCN,
                            extra_files={"probe.gd": _EVENT_PROBE_GD})
    _start_daemon(project)
    try:
        payload = _run_cli(project, "press", "jump")
        assert payload["ok"] is True, payload
        _run_cli(project, "wait-time", "0.2")
        got = _run_cli(project, "get", "/root/Main", "saw_jump_event")
        assert got["ok"] is True, got
        # PR1 阶段 get 仍是旧编码（裸值）；PR2 落地后此断言改 result["value"]
        assert got["result"] is True, got
    finally:
        _run_cli(project, "release-all")
        _stop_daemon(project)
```

> 执行注意：若文件内没有 `_make_project` / `_start_daemon` 这类抽象，而是每个测试
> 内联构造，就照内联模式写——**对齐文件现状，不发明新抽象**。

- [ ] **Step 2: 跑 e2e 确认失败**（subagent；本机需有 godot，没有则该文件整体 skip——此时跳过本 Step，在 Step 4 用 GUT 结果兜底并在 PR 描述注明）

```bash
cd python && python -m pytest tests/test_e2e_input.py::test_press_reaches_unhandled_input -v
```

预期（在 Task 1.1 实现已提交的前提下）：**PASS**。若想严格验证测试有效性，可临时 `git stash` Task 1.1 的实现再跑——应 FAIL（saw_jump_event 为 False），随后 `git stash pop`。

- [ ] **Step 3: Commit**

```bash
git add python/tests/test_e2e_input.py
git commit -m "test(e2e): 事件回调式输入链路回归（#97）"
```

### Task 1.3: 文档同步

- [ ] **Step 1: SKILL.md 模板**（`python/godot_cli_control/templates/skill/SKILL.md`）

- 输入命令组（press/tap/hold/combo）说明改为正面声明：输入走 `Input.parse_input_event`，
  轮询（`is_action_pressed`/`get_vector`）与事件回调（`_input`/`_unhandled_input`）两种游戏写法都可见。
- Common pitfalls 增/改一条：`InputEventAction` 无鼠标坐标——依赖位置的 `_gui_input` 控件用 `click`，不要用 press 模拟鼠标。

- [ ] **Step 2: addon README + CHANGELOG**

- `addons/godot_cli_control/README.md`：输入命令表附注同步上述语义。
- `addons/godot_cli_control/CHANGELOG.md`：Unreleased 段加 `- feat(input): press/tap/hold/combo 注入 InputEventAction 进事件管线（#97）`（条目格式对齐文件现状）。

- [ ] **Step 3: 渲染验证**（subagent）

```bash
cd python && python -c "from godot_cli_control import cli; print(cli.format_full_help())" >/dev/null && echo RENDER_OK
cd python && python -m pytest tests/test_skills_install.py -q
```

预期：`RENDER_OK` + skills install 全绿。

- [ ] **Step 4: Commit**

```bash
git add python/godot_cli_control/templates/skill/SKILL.md addons/godot_cli_control/README.md addons/godot_cli_control/CHANGELOG.md
git commit -m "docs: 输入事件管线语义同步 SKILL.md / addon README（#97）"
```

### Task 1.4: 全量验证 + 提 PR

- [ ] **Step 1: 全量测试**（subagent，一次性跑完回报精简结论）

```bash
GODOT_BIN=... python addons/godot_cli_control/tests/run_gut.py
cd python && coverage run -m pytest -q && coverage report | tail -3
cd python && ruff check .
```

预期：GUT 0 失败；pytest 0 失败、覆盖率 ≥80%；ruff 全绿。

- [ ] **Step 2: push + PR**（绕代理 + 禁沙箱）

```bash
git push -u origin feat/97-input-event-pipeline
env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  gh pr create --title "feat(input): press/release 走 Input.parse_input_event，事件管线可见 (#97)" \
  --body "Closes #97。<按仓库 PR 模板填：动机 / 改动 / 验证证据（GUT+pytest+e2e 结果）/ 边界说明（_gui_input 用 click）>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

# Phase 2（PR2）：#99 Variant 编码器 + #100 get_properties

**Files:**
- Create: `addons/godot_cli_control/bridge/variant_codec.gd`
- Create: `addons/godot_cli_control/tests/gut/test_variant_codec.gd`
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd`（`handle_get_property` 改造 + `_read_property` + `handle_get_properties`）
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd:178`（注册 `get_properties`）
- Modify: `python/godot_cli_control/client.py`（`get_properties`）、`bridge.py`、`cli.py`（get nargs+ / formatter）
- Test: `addons/godot_cli_control/tests/gut/test_low_level_api.gd`、`python/tests/test_cli.py`、`python/tests/test_client.py`、`python/tests/test_e2e_client_direct.py`、`python/tests/test_e2e_input.py`（PR1 探针断言改 shape）
- Docs: SKILL.md 模板、addon README、CHANGELOG、根 README（Recent changes）

先建分支：`git checkout -b feat/99-100-get-codec`（基于 PR1 分支）。

### Task 2.1: variant_codec.gd（TDD）

- [ ] **Step 1: 写失败 GUT 测试**

新建 `addons/godot_cli_control/tests/gut/test_variant_codec.gd`：

```gdscript
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
```

- [ ] **Step 2: 跑 GUT 确认失败**（subagent）——预期：preload 失败 / 类不存在。

- [ ] **Step 3: 实现 `variant_codec.gd`**

```gdscript
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


## 顶层编码：返回 {"value": <json-safe>}，复合类型 / StringName / NodePath /
## Object 额外带 "type" 字段。
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
static func encode_value(v: Variant) -> Variant:
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
				arr_out.append(encode_value(item))
			return arr_out
		TYPE_DICTIONARY:
			var dict_out: Dictionary = {}
			var dict_in: Dictionary = v as Dictionary
			for key: Variant in dict_in:
				dict_out[str(key)] = encode_value(dict_in[key])
			return dict_out
		TYPE_PACKED_BYTE_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_INT64_ARRAY, TYPE_PACKED_STRING_ARRAY:
			return Array(v)
		TYPE_PACKED_FLOAT32_ARRAY, TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_VECTOR2_ARRAY, TYPE_PACKED_VECTOR3_ARRAY, TYPE_PACKED_COLOR_ARRAY, TYPE_PACKED_VECTOR4_ARRAY:
			var packed_out: Array = []
			for item: Variant in v:
				packed_out.append(encode_value(item))
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
```

> 实现注意：PackedFloat32Array 的 float 元素和 Color 分量是 32 位精度，
> 测试比较时若遇 0.1 这类无法精确表示的值，断言写法以构造同款 Variant 取分量
> 为期望（如上 Color case），**不要硬编码十进制字面量**。

- [ ] **Step 4: 跑 GUT 确认通过**（subagent）。冷启动若报 `Class 'CliControlVariantCodec' not found`，先跑一次 `godot --editor --quit --path .` 重建 global class cache（见 low_level_api.gd 头注释）。

- [ ] **Step 5: Commit**

```bash
git add addons/godot_cli_control/bridge/variant_codec.gd addons/godot_cli_control/tests/gut/test_variant_codec.gd
git commit -m "feat(bridge): CliControlVariantCodec —— Variant→JSON 编码，与 set 侧 schema 对称（#99）"
```

### Task 2.2: handle_get_property 改造 + sub-path 读（TDD）

- [ ] **Step 1: 写失败 GUT 测试**（`test_low_level_api.gd` 追加；fixture 模式对齐文件现状——先读文件确认 `_api` 怎么构造）

```gdscript
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
```

- [ ] **Step 2: 跑 GUT 确认失败**（subagent）——预期：value 是 Vector2 原对象 / sub-path 报 1002。

- [ ] **Step 3: 实现**

`low_level_api.gd`：替换 `handle_get_property`（约 114-123 行）并新增 `_read_property`：

```gdscript
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
## SKILL.md 已声明该边界；这里只校验 ":" 前的 top-level 名存在（1002 兜底 typo）。
func _read_property(node: Node, property: String) -> Dictionary:
	if property.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'property' parameter")
	var is_sub_path: bool = ":" in property
	var top_level: String = property.split(":", true, 1)[0] if is_sub_path else property
	if not _has_property(node, top_level):
		return _err(CliControlErrorCodes.PROPERTY_NOT_FOUND, "Property not found: %s" % top_level)
	if is_sub_path:
		return {"value": node.get_indexed(NodePath(property))}
	return {"value": node.get(property)}
```

- [ ] **Step 4: 跑 GUT 确认通过 + 原有 get 相关用例适配**（subagent）。原有断言 `{"value": <裸值>}` 的用例若只涉及基本类型应原样通过（基本类型编码不变形）；涉及复合类型的旧断言**改成新 shape**（这是 #99 的预期行为变更，不是回归）。

- [ ] **Step 5: Commit**

```bash
git add addons/godot_cli_control/bridge/low_level_api.gd addons/godot_cli_control/tests/gut/test_low_level_api.gd
git commit -m "feat(get): 复合 Variant 编码走 codec + sub-path 读（#99）"
```

### Task 2.3: get_properties RPC（TDD）

- [ ] **Step 1: 写失败 GUT 测试**（`test_low_level_api.gd` 追加）

```gdscript
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
```

- [ ] **Step 2: 跑 GUT 确认失败**（subagent）——`handle_get_properties` 不存在。

- [ ] **Step 3: 实现**（`low_level_api.gd` 追加 + `game_bridge.gd` 注册）

```gdscript
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
		var top_level: String = prop_name.split(":", true, 1)[0] if ":" in prop_name else prop_name
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
```

`game_bridge.gd` `_register_methods()` 在 `get_property` 行后加：

```gdscript
	_methods["get_properties"] = {"callable": _low_level_api.handle_get_properties, "kind": "sync"}
```

- [ ] **Step 4: 跑 GUT 确认通过**（subagent）。

- [ ] **Step 5: Commit**

```bash
git add addons/godot_cli_control/bridge/low_level_api.gd addons/godot_cli_control/bridge/game_bridge.gd addons/godot_cli_control/tests/gut/test_low_level_api.gd
git commit -m "feat(get): get_properties 多属性同帧原子读（#100）"
```

### Task 2.4: Python client + bridge

- [ ] **Step 1: 写失败 pytest**（`python/tests/test_client.py` 追加；mock 模式对齐文件现有 `get_property` 用例——先读文件）

测试意图（按现有 harness 改写）：
1. `get_properties` 发出 `{"method": "get_properties", "params": {"path": ..., "properties": [...]}}`；
2. 服务端回 `{"values": {"position": {"value": [1, 2], "type": "Vector2"}, "visible": {"value": true}}}` 时返回 `{"position": [1, 2], "visible": True}`（裸 value 映射）；
3. `get_property` 行为不变（已有用例守护，跑一遍确认）。

- [ ] **Step 2: 跑确认失败**（subagent）：`cd python && python -m pytest tests/test_client.py -k get_properties -v`

- [ ] **Step 3: 实现**

`client.py`（`get_property` 后追加；`get_property` 本身**零改动**——它已经返回 `result.get("value")`，新编码下自动变成裸 JSON-safe 值）：

```python
    async def get_properties(self, path: str, props: list[str]) -> dict[str, Any]:
        """同帧原子读多个属性（issue #100），返回 {prop: 裸 value} 映射。

        type 字段是给 CLI JSON 信封的（agent 消费）；Python 层要 type 时直接
        ``await client.request("get_properties", ...)`` 拿原始 result。
        """
        result = await self.request(
            "get_properties", {"path": path, "properties": list(props)}
        )
        return {
            k: (v.get("value") if isinstance(v, dict) else v)
            for k, v in result.get("values", {}).items()
        }
```

`bridge.py`（`get_property` 后追加）：

```python
    def get_properties(self, path: str, props: list[str]) -> dict[str, Any]:
        """同帧原子读多个属性（issue #100），返回 {prop: value} 映射。"""
        return self._run(self._client.get_properties(path, props))
```

- [ ] **Step 4: 跑确认通过**（subagent）：`cd python && python -m pytest tests/test_client.py tests/test_bridge.py -q`

- [ ] **Step 5: Commit**

```bash
git add python/godot_cli_control/client.py python/godot_cli_control/bridge.py python/tests/test_client.py
git commit -m "feat(client): get_properties 同帧原子读包装（#100）"
```

### Task 2.5: CLI get 多属性 + 信封 shape

- [ ] **Step 1: 写失败 pytest**（`python/tests/test_cli.py` 追加；先读文件中现有 `get` 子命令用例的 mock/调用模式，完全照搬其 harness）

测试意图：
1. `get /root/N position` 单属性 → 发 `get_property`，信封 `result == {"value": [1, 2], "type": "Vector2"}`（**透传 RPC result，含 type**）；
2. `get /root/N a b` 多属性 → 发 `get_properties`，信封 `result == {"values": {...}}`；
3. 文本模式：单属性输出 value 的 JSON（数组打印 `[1.5, -2.0]`）；多属性每行 `prop = value`；
4. 退出码：成功 0；RPC 错误（如 1002）→ 1。

- [ ] **Step 2: 跑确认失败**（subagent）。

- [ ] **Step 3: 实现**（`cli.py`）

`cmd_get` 替换（约 344 行）：

```python
async def cmd_get(client: GameClient, ns: argparse.Namespace) -> dict:
    """读节点属性（1 个或多个，支持 sub-path 如 position:x）。

    透传 RPC result（带 type 字段）——client.get_property 的裸 value 是给
    Python 脚本的便捷层；CLI 信封必须带 type 给 agent 消歧（issue #99/#100）。
    单属性 result={"value", "type"?}；多属性 result={"values": {...}}。
    """
    props: list[str] = ns.props
    if len(props) == 1:
        return await client.request(
            "get_property", {"path": ns.node_path, "property": props[0]}
        )
    return await client.request(
        "get_properties", {"path": ns.node_path, "properties": props}
    )
```

`_fmt_get_text` 旁新增（`_fmt_get_text` 保留给 `call`）：

```python
def _fmt_get_result_text(r: dict) -> str:
    """get 的文本渲染：单属性打印 value；多属性每行 ``prop = value``。"""
    if "values" in r:
        return "\n".join(
            f"{prop} = {_fmt_get_text(entry.get('value'))}"
            for prop, entry in r["values"].items()
        )
    return _fmt_get_text(r.get("value"))
```

`get` 的 RpcSpec 更新：

```python
    RpcSpec(
        name="get",
        handler=cmd_get,
        description=(
            "读节点属性（1 个或多个；多个时服务端同帧原子读，issue #100）。"
            "复合类型（Vector2 等）返回与 set 同 schema 的数组 + type 字段，"
            "可直接回灌 set（issue #99）。支持 sub-path：position:x。"
        ),
        positionals=(
            Positional("node_path", None, "绝对节点路径，如 /root/Main"),
            Positional("props", "+", "属性名，1 个或多个；支持 sub-path 如 position:x"),
        ),
        example="get /root/Player position visible",
        text_formatter=_fmt_get_result_text,
    ),
```

同步更新 `python/tests/test_e2e_input.py` 中 PR1 探针断言：
`assert got["result"] is True` → `assert got["result"]["value"] is True`（删掉旧注释行）。
全仓 grep 一次 `get_property` 的其他消费点（`pytest_plugin.py` / examples / e2e）确认没有第二处依赖旧 shape 的：

```bash
grep -rn "get_property\|\"get\"" python/godot_cli_control/ python/tests/ examples/ --include="*.py" --include="*.sh" | grep -v test_cli
```

- [ ] **Step 4: 跑确认通过**（subagent）：`cd python && python -m pytest tests/test_cli.py tests/test_e2e_input.py -q`（e2e 无 godot 则 skip，注明）

- [ ] **Step 5: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py python/tests/test_e2e_input.py
git commit -m "feat(cli): get 支持多属性原子读 + 编码信封透传（#99 #100）"
```

### Task 2.6: e2e 直连扩展

- [ ] **Step 1: `python/tests/test_e2e_client_direct.py` 追加用例**（对齐文件现有 fixture）：真 daemon 下 `client.get_properties("/root/Main", ["name", "process_mode"])` 返回 dict 且 key 齐全；`get` CLI 对 Node2D `position` 返回数组 + type（走 `_run_cli` 的项目用带 Node2D 的场景，或直接 `client.request("get_property", ...)` 断言 shape）。

- [ ] **Step 2: 跑通过**（subagent；无 godot 则 skip 注明）。

- [ ] **Step 3: Commit**：`git commit -m "test(e2e): get 编码 shape + get_properties 真连回归（#99 #100）"`

### Task 2.7: 文档 + 全量验证 + PR

- [ ] **Step 1: 文档**
  - SKILL.md 模板：get 命令说明（多属性 / sub-path / value+type 信封示例）、JSON 信封示例更新、pitfalls 加三条：① 复合类型返回数组+type，可直接回灌 set；② 嵌套复合类型无 type；③ Python `bridge.get_property` 只给裸 value，要 type 走 CLI。
  - addon README：命令表 + 行为变更说明。
  - CHANGELOG：**breaking 显著标注**——`get 复合 Variant 返回从 "(x, y)" 字符串改为 set-schema 数组 + type 字段；信封 result 从裸值变 {"value","type"} 对象（#99）`；另一条 feat get_properties（#100）。
  - 根 README「Recent changes」加一条行为变更。
- [ ] **Step 2: 渲染验证**（subagent）：同 Task 1.3 Step 3。
- [ ] **Step 3: 全量**（subagent）：GUT + `coverage run -m pytest` + ruff，预期全绿、覆盖率 ≥80%。
- [ ] **Step 4: Commit + push + PR**

```bash
git add -A && git commit -m "docs: get 编码/原子读行为变更同步 SKILL.md / README / CHANGELOG（#99 #100）"
git push -u origin feat/99-100-get-codec
env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  gh pr create --base feat/97-input-event-pipeline \
  --title "feat(get): Variant 编码对称化 + get_properties 同帧原子读 (#99, #100)" \
  --body "Closes #99, closes #100。<动机 / breaking 说明 / 验证证据>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

（若 PR1 已合并，`--base main` 并先 rebase。）

---

# Phase 3（PR3）：#96 条件等待原语

**Files:**
- Modify: `addons/godot_cli_control/bridge/error_codes.gd`（1007）
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd`（三个 wait handler + 比较器 + `_SignalCapture`）
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`（注册 ×3）
- Modify: `python/godot_cli_control/client.py`、`bridge.py`、`cli.py`
- Test: `addons/godot_cli_control/tests/gut/test_low_level_api.gd`、`python/tests/test_cli.py`、`python/tests/test_client.py`、`python/tests/test_e2e_client_direct.py`
- Docs: SKILL.md 模板（命令 + 1007 + pitfalls）、addon README、CHANGELOG

先建分支：`git checkout -b feat/96-wait-primitives`（基于 PR2 分支）。

### Task 3.1: 错误码 1007

- [ ] **Step 1:** `error_codes.gd` 在 `RESOURCE_UNAVAILABLE` 后追加：

```gdscript
# 信号不存在（wait_signal 的 schema 错，永久性——与 1003 method、1002 property 同族）
const SIGNAL_NOT_FOUND: int = 1007
```

- [ ] **Step 2: Commit**：`git commit -m "feat(bridge): 错误码 1007 SIGNAL_NOT_FOUND（#96）"`

### Task 3.2: wait_frames（TDD）

- [ ] **Step 1: 写失败 GUT 测试**（`test_low_level_api.gd` 追加）

```gdscript
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
```

- [ ] **Step 2: 跑确认失败**（subagent）。

- [ ] **Step 3: 实现**（`low_level_api.gd`；常量加在 `_MAX_WAIT_SECONDS` 旁）

```gdscript
# wait_frames 防呆上限：60fps 下 1 分钟。要等更久用 wait_game_time / wait_property。
const _MAX_WAIT_FRAMES: int = 3600
```

```gdscript
## issue #96：等 N 帧（确定性帧推进）。--physics 等 physics_frame。
func wait_frames_async(params: Dictionary) -> Dictionary:
	var frames_raw: Variant = params.get("frames", null)
	if not (frames_raw is int or frames_raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'frames' must be an integer")
	var frames: int = int(frames_raw)
	if frames < 1 or frames > _MAX_WAIT_FRAMES:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "frames must be 1..%d (got %d)" % [_MAX_WAIT_FRAMES, frames])
	var physics: bool = bool(params.get("physics", false))
	for _i in frames:
		if physics:
			await get_tree().physics_frame
		else:
			await get_tree().process_frame
	return {"success": true, "frames": frames}
```

注册（`game_bridge.gd` async 区）：

```gdscript
	_methods["wait_frames"] = {"callable": _low_level_api.wait_frames_async, "kind": "async"}
```

- [ ] **Step 4: 跑确认通过 → Step 5: Commit**：`git commit -m "feat(wait): wait_frames 确定性帧推进（#96）"`

### Task 3.3: wait_property + 比较器（TDD）

- [ ] **Step 1: 写失败 GUT 测试**

```gdscript
# ── issue #96：wait_property ──

func test_wait_property_matches_immediately() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(5.0, 0.0)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "position", "value": [5.0, 0.0], "timeout": 1.0,
	})
	assert_true(result["matched"])
	assert_eq(result["value"], [5.0, 0.0])


func test_wait_property_catches_later_change() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.visible = false
	get_tree().create_timer(0.05).timeout.connect(func() -> void: node.visible = true)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "visible", "value": true, "timeout": 2.0,
	})
	assert_true(result["matched"])


func test_wait_property_gt_on_sub_path() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(0.0, 0.0)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: node.position = Vector2(600.0, 0.0))
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "position:x", "value": 500, "op": "gt", "timeout": 2.0,
	})
	assert_true(result["matched"])


func test_wait_property_timeout_reports_reason_and_last_value() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.visible = false
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "visible", "value": true, "timeout": 0.1,
	})
	assert_false(result["matched"])
	assert_eq(result["reason"], "timeout")
	assert_eq(result["value"], false)


func test_wait_property_node_not_found_reason() -> void:
	var result: Dictionary = await _api.wait_property_async({
		"path": "/root/__nope__", "property": "visible", "value": true, "timeout": 0.1,
	})
	assert_false(result["matched"])
	assert_eq(result["reason"], "node_not_found")


func test_wait_property_tolerance_eq() -> void:
	var node := Node2D.new()
	add_child_autofree(node)
	node.position = Vector2(1.0001, 0.0)
	var result: Dictionary = await _api.wait_property_async({
		"path": str(node.get_path()), "property": "position:x", "value": 1.0,
		"tolerance": 0.001, "timeout": 0.5,
	})
	assert_true(result["matched"])


func test_wait_property_rejects_ordering_op_with_non_numeric_value() -> void:
	var result: Dictionary = await _api.wait_property_async({
		"path": "/root", "property": "name", "value": [1, 2], "op": "gt", "timeout": 0.1,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
```

- [ ] **Step 2: 跑确认失败**（subagent）。

- [ ] **Step 3: 实现**（`low_level_api.gd`）

```gdscript
const _WAIT_PROP_OPS: PackedStringArray = ["eq", "ne", "gt", "lt", "ge", "le"]


## issue #96：逐帧轮询等属性满足条件。超时不报错——返回 matched=false +
## reason（timeout / node_not_found / property_not_found，按最后一次轮询状态）
## + value（最后一次读到的编码值），容忍节点/属性中途出现，typo 靠 reason 诊断。
func wait_property_async(params: Dictionary) -> Dictionary:
	var path: String = params.get("path", "") as String
	var property: String = params.get("property", "") as String
	if property.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'property' parameter")
	var op: String = params.get("op", "eq") as String
	if not op in _WAIT_PROP_OPS:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "op must be one of: %s" % ", ".join(_WAIT_PROP_OPS))
	var timeout: float = params.get("timeout", 5.0) as float
	if timeout < 0.0 or timeout > _MAX_WAIT_SECONDS:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "timeout must be 0..%s" % _MAX_WAIT_SECONDS)
	var tolerance: float = params.get("tolerance", 0.0) as float
	if tolerance < 0.0:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "tolerance must be >= 0")
	var expected: Variant = params.get("value", null)
	if not (expected is int or expected is float) and not op in ["eq", "ne"]:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "op '%s' requires a numeric value; compound/string values only support eq/ne" % op)
	var start_ms: int = Time.get_ticks_msec()
	var reason: String = "timeout"
	var last_value: Variant = null
	while true:
		var node: Node = get_tree().root.get_node_or_null(path)
		if node == null:
			reason = "node_not_found"
			last_value = null
		else:
			var read: Dictionary = _read_property(node, property)
			if read.has("error"):
				reason = "property_not_found"
				last_value = null
			else:
				reason = "timeout"
				last_value = CliControlVariantCodec.encode_value(read["value"])
				if _wait_compare(last_value, expected, op, tolerance):
					return {
						"matched": true,
						"value": last_value,
						"waited": float(Time.get_ticks_msec() - start_ms) / 1000.0,
					}
		if float(Time.get_ticks_msec() - start_ms) / 1000.0 >= timeout:
			break
		await get_tree().process_frame
	return {"matched": false, "reason": reason, "value": last_value}


## 编码后值比较。数值走 6 op（eq/ne 带 tolerance）；其余类型仅 eq/ne 深比较
## （数组逐元素，数值元素同样吃 tolerance）。ordering op + 非数值 actual →
## false（继续轮询直到 timeout，reason 诊断）。
func _wait_compare(actual: Variant, expected: Variant, op: String, tolerance: float) -> bool:
	if (actual is int or actual is float) and (expected is int or expected is float):
		var a: float = float(actual)
		var b: float = float(expected)
		match op:
			"eq": return absf(a - b) <= tolerance
			"ne": return absf(a - b) > tolerance
			"gt": return a > b
			"lt": return a < b
			"ge": return a >= b
			"le": return a <= b
		return false
	var equal: bool = _deep_equal(actual, expected, tolerance)
	if op == "eq":
		return equal
	if op == "ne":
		return not equal
	return false


func _deep_equal(a: Variant, b: Variant, tolerance: float) -> bool:
	if (a is int or a is float) and (b is int or b is float):
		return absf(float(a) - float(b)) <= tolerance
	if a is Array and b is Array:
		var aa: Array = a as Array
		var ba: Array = b as Array
		if aa.size() != ba.size():
			return false
		for i in aa.size():
			if not _deep_equal(aa[i], ba[i], tolerance):
				return false
		return true
	return a == b  # String / bool / null / Dictionary（引擎深比较）
```

注册：`_methods["wait_property"] = {"callable": _low_level_api.wait_property_async, "kind": "async"}`

- [ ] **Step 4: 跑确认通过 → Step 5: Commit**：`git commit -m "feat(wait): wait_property 条件等待 + 比较器（#96）"`

### Task 3.4: wait_signal（TDD）

- [ ] **Step 1: 写失败 GUT 测试**

```gdscript
# ── issue #96：wait_signal ──

func test_wait_signal_captures_emission_and_args() -> void:
	var emitter := Node.new()
	emitter.add_user_signal("e2e_sig", [{"name": "v", "type": TYPE_INT}])
	add_child_autofree(emitter)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: emitter.emit_signal("e2e_sig", 42))
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "e2e_sig", "timeout": 2.0,
	})
	assert_true(result["emitted"])
	assert_eq(result["args"], [{"value": 42}])


func test_wait_signal_zero_arg_signal() -> void:
	var emitter := Node.new()
	add_child_autofree(emitter)
	get_tree().create_timer(0.05).timeout.connect(func() -> void: emitter.emit_signal("renamed"))
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "renamed", "timeout": 2.0,
	})
	assert_true(result["emitted"])
	assert_eq(result["args"], [])


func test_wait_signal_timeout_cleans_up_connection() -> void:
	var emitter := Node.new()
	emitter.add_user_signal("never_fires")
	add_child_autofree(emitter)
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "never_fires", "timeout": 0.1,
	})
	assert_false(result["emitted"])
	assert_eq(emitter.get_signal_connection_list("never_fires").size(), 0, "超时后连接必须清理")


func test_wait_signal_node_missing_is_1001() -> void:
	var result: Dictionary = await _api.wait_signal_async({
		"path": "/root/__nope__", "signal": "x", "timeout": 0.1,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)


func test_wait_signal_unknown_signal_is_1007() -> void:
	var emitter := Node.new()
	add_child_autofree(emitter)
	var result: Dictionary = await _api.wait_signal_async({
		"path": str(emitter.get_path()), "signal": "__nope__", "timeout": 0.1,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1007)
```

- [ ] **Step 2: 跑确认失败**（subagent）。

- [ ] **Step 3: 实现**（`low_level_api.gd`：文件级 inner class + handler）

```gdscript
## wait_signal 的参数捕获器（issue #96）。GDScript 无变参 Callable，
## 按信号声明 argc（get_signal_list）从 0..MAX_ARGS 选对应 cap 函数。
class _SignalCapture:
	extends RefCounted

	const MAX_ARGS: int = 8

	var fired: bool = false
	var args: Array = []

	func cap0() -> void: _hit([])
	func cap1(a1: Variant) -> void: _hit([a1])
	func cap2(a1: Variant, a2: Variant) -> void: _hit([a1, a2])
	func cap3(a1: Variant, a2: Variant, a3: Variant) -> void: _hit([a1, a2, a3])
	func cap4(a1: Variant, a2: Variant, a3: Variant, a4: Variant) -> void: _hit([a1, a2, a3, a4])
	func cap5(a1: Variant, a2: Variant, a3: Variant, a4: Variant, a5: Variant) -> void: _hit([a1, a2, a3, a4, a5])
	func cap6(a1: Variant, a2: Variant, a3: Variant, a4: Variant, a5: Variant, a6: Variant) -> void: _hit([a1, a2, a3, a4, a5, a6])
	func cap7(a1: Variant, a2: Variant, a3: Variant, a4: Variant, a5: Variant, a6: Variant, a7: Variant) -> void: _hit([a1, a2, a3, a4, a5, a6, a7])
	func cap8(a1: Variant, a2: Variant, a3: Variant, a4: Variant, a5: Variant, a6: Variant, a7: Variant, a8: Variant) -> void: _hit([a1, a2, a3, a4, a5, a6, a7, a8])

	func callable_for(argc: int) -> Callable:
		match argc:
			0: return cap0
			1: return cap1
			2: return cap2
			3: return cap3
			4: return cap4
			5: return cap5
			6: return cap6
			7: return cap7
			8: return cap8
		return Callable()

	func _hit(captured: Array) -> void:
		if fired:
			return
		fired = true
		args = captured
```

```gdscript
## issue #96：等信号发射（带超时）。竞态注意（SKILL.md pitfall）：必须先挂
## 等待再触发动作——信号在 connect 之前发射不会被捕获。
func wait_signal_async(params: Dictionary) -> Dictionary:
	var path: String = params.get("path", "") as String
	var node: Node = get_tree().root.get_node_or_null(path)
	if node == null:
		return _node_not_found(path)
	var signal_name: String = params.get("signal", "") as String
	if signal_name.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'signal' parameter")
	if not node.has_signal(signal_name):
		return _err(CliControlErrorCodes.SIGNAL_NOT_FOUND, "Signal not found: %s" % signal_name)
	var timeout: float = params.get("timeout", 5.0) as float
	if timeout < 0.0 or timeout > _MAX_WAIT_SECONDS:
		return _err(CliControlErrorCodes.INVALID_PARAMS, "timeout must be 0..%s" % _MAX_WAIT_SECONDS)
	var argc: int = 0
	for sig: Dictionary in node.get_signal_list():
		if sig["name"] == signal_name:
			argc = (sig["args"] as Array).size()
			break
	if argc > _SignalCapture.MAX_ARGS:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"signal '%s' has %d args (max %d supported)" % [signal_name, argc, _SignalCapture.MAX_ARGS],
		)
	var capture: _SignalCapture = _SignalCapture.new()
	var cb: Callable = capture.callable_for(argc)
	node.connect(signal_name, cb, CONNECT_ONE_SHOT)
	var start_ms: int = Time.get_ticks_msec()
	while not capture.fired:
		if float(Time.get_ticks_msec() - start_ms) / 1000.0 >= timeout:
			break
		await get_tree().process_frame
		if not is_instance_valid(node):
			break  # 节点被释放：连接随之失效，按未命中处理
	if not capture.fired:
		# one-shot 未触发时连接仍挂着，必须显式清理（防悬挂 Callable 泄漏）
		if is_instance_valid(node) and node.is_connected(signal_name, cb):
			node.disconnect(signal_name, cb)
		return {"emitted": false}
	var encoded_args: Array = []
	for arg: Variant in capture.args:
		encoded_args.append(CliControlVariantCodec.encode(arg))
	return {"emitted": true, "args": encoded_args}
```

注册：`_methods["wait_signal"] = {"callable": _low_level_api.wait_signal_async, "kind": "async"}`

- [ ] **Step 4: 跑确认通过 + GUT 全量**（subagent）→ **Step 5: Commit**：`git commit -m "feat(wait): wait_signal 信号等待 + 参数捕获（#96）"`

### Task 3.5: Python client + bridge

- [ ] **Step 1: 失败测试**（`test_client.py`：三个方法各发对应 method/params、透传 result；mock 模式对齐现状）

- [ ] **Step 2: 实现**（`client.py` 在 `wait_game_time` 后追加）

```python
    async def wait_property(
        self,
        path: str,
        prop: str,
        value: Any,
        op: str = "eq",
        timeout: float = 5.0,
        tolerance: float = 0.0,
    ) -> dict:
        """等属性满足条件（issue #96）。返回 {"matched": bool, ...}，超时不抛。"""
        return await self.request(
            "wait_property",
            {
                "path": path, "property": prop, "value": value,
                "op": op, "timeout": timeout, "tolerance": tolerance,
            },
            timeout=timeout + 5.0,
        )

    async def wait_signal(self, path: str, signal: str, timeout: float = 5.0) -> dict:
        """等信号发射（issue #96）。返回 {"emitted": bool, "args": [...]}，超时不抛。"""
        return await self.request(
            "wait_signal",
            {"path": path, "signal": signal, "timeout": timeout},
            timeout=timeout + 5.0,
        )

    async def wait_frames(self, frames: int, physics: bool = False) -> dict:
        """等 N 帧（issue #96）。client wall-time 按最低 10fps 估算 + 10s grace。"""
        return await self.request(
            "wait_frames",
            {"frames": frames, "physics": physics},
            timeout=max(30.0, frames / 10.0 + 10.0),
        )
```

`bridge.py`「等待」区追加同名同步包装（`return self._run(self._client.wait_property(...))` 等三个，docstring 一行）。

- [ ] **Step 3: 跑通过**（subagent）→ **Step 4: Commit**：`git commit -m "feat(client): wait_property / wait_signal / wait_frames 包装（#96）"`

### Task 3.6: CLI 三个子命令

- [ ] **Step 1: 失败测试**（`test_cli.py`，对齐现有 RPC 子命令用例模式）

覆盖矩阵：
1. `wait-prop /root/N visible true` 默认 op=eq/timeout=5/tolerance=0，matched=true → exit 0；matched=false → exit 1；
2. preflight：`--timeout abc` / `--timeout 9999` / `--tolerance -1` / `--op gt` + value `'[1,2]'` / `--op gt` + value `"str"` / `--op gt` + value `true` → 全部 -1003 + exit 64（**注意 bool 不算数值**）；
3. `wait-signal /root/N sig` emitted → 0，timeout → 1；`wait-frames 3` 成功 0；`wait-frames 0` / `wait-frames abc` / `wait-frames 9999` → 64；
4. 文本模式输出与 formatter 一致。

- [ ] **Step 2: 实现**（`cli.py`）

handlers：

```python
async def cmd_wait_prop(client: GameClient, ns: argparse.Namespace) -> dict:
    expected = _parse_json_arg(ns.value)
    return await client.wait_property(
        ns.node_path, ns.prop, expected,
        op=ns.op, timeout=float(ns.timeout), tolerance=float(ns.tolerance),
    )


async def cmd_wait_signal(client: GameClient, ns: argparse.Namespace) -> dict:
    timeout = float(ns.timeout) if ns.timeout else 5.0
    return await client.wait_signal(ns.node_path, ns.signal_name, timeout=timeout)


async def cmd_wait_frames(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.wait_frames(int(ns.frames), physics=ns.physics)
```

preflight（放 `_preflight_combo` 附近）：

```python
_WAIT_PROP_OPS = ("eq", "ne", "gt", "lt", "ge", "le")


def _require_float(raw: Any, cmd: str, field: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{cmd}: {field} 必须是数字，收到 {raw!r}")


def _preflight_wait_prop(ns: argparse.Namespace) -> None:
    timeout = _require_float(ns.timeout, "wait-prop", "timeout")
    if not 0 <= timeout <= 3600:
        raise ValueError(f"wait-prop: timeout 必须在 0..3600 秒，收到 {timeout}")
    tolerance = _require_float(ns.tolerance, "wait-prop", "tolerance")
    if tolerance < 0:
        raise ValueError(f"wait-prop: tolerance 必须 >= 0，收到 {tolerance}")
    expected = _parse_json_arg(ns.value)
    is_numeric = isinstance(expected, (int, float)) and not isinstance(expected, bool)
    if ns.op not in ("eq", "ne") and not is_numeric:
        raise ValueError(
            f"wait-prop: --op {ns.op} 只支持数值比较；复合/字符串/bool 值只能用 eq/ne"
        )


def _preflight_wait_signal(ns: argparse.Namespace) -> None:
    if ns.timeout is None:
        return
    timeout = _require_float(ns.timeout, "wait-signal", "timeout")
    if not 0 <= timeout <= 3600:
        raise ValueError(f"wait-signal: timeout 必须在 0..3600 秒，收到 {timeout}")


def _preflight_wait_frames(ns: argparse.Namespace) -> None:
    try:
        frames = int(ns.frames)
    except (TypeError, ValueError):
        raise ValueError(f"wait-frames: frames 必须是整数，收到 {ns.frames!r}")
    if not 1 <= frames <= 3600:
        raise ValueError(f"wait-frames: frames 必须在 1..3600，收到 {frames}")
```

formatter / exit / extra_args：

```python
def _fmt_wait_prop_text(r: dict) -> str:
    if r.get("matched"):
        return f"matched (waited {r.get('waited', 0.0):.3f}s)"
    return (
        f"timeout (reason={r.get('reason', 'timeout')}, "
        f"last={json.dumps(r.get('value'), ensure_ascii=False)})"
    )


def _fmt_wait_signal_text(r: dict) -> str:
    return "emitted" if r.get("emitted") else "timeout"


def _exit_from_wait_prop(r: dict) -> int:
    return EXIT_OK if r.get("matched") else EXIT_RPC_ERROR


def _exit_from_wait_signal(r: dict) -> int:
    return EXIT_OK if r.get("emitted") else EXIT_RPC_ERROR


def _register_wait_prop_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("node_path", help="绝对节点路径")
    p.add_argument("prop", help="属性名（支持 sub-path 如 position:x）")
    p.add_argument("value", help="期望值（JSON-or-string，同 set 的 value 规则）")
    p.add_argument("--op", choices=_WAIT_PROP_OPS, default="eq",
                   help="比较运算符，默认 eq；gt/lt/ge/le 仅数值")
    p.add_argument("--timeout", default="5", help="超时秒（0..3600，默认 5）")
    p.add_argument("--tolerance", default="0", help="float eq/ne 容差（默认 0=精确比较）")


def _register_wait_frames_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("frames", help="等待帧数（1..3600）")
    p.add_argument("--physics", action="store_true", help="等 physics_frame（默认 process_frame）")
```

RpcSpec ×3（插在 `wait-time` 之后）：

```python
    RpcSpec(
        name="wait-prop",
        handler=cmd_wait_prop,
        description=(
            "逐帧轮询直到属性满足条件（或 timeout）。退出码：0=命中, 1=超时, "
            "2=infra error。超时返回 reason（timeout/node_not_found/property_not_found）"
            "+ 最后读到的值，便于诊断 typo。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="wait-prop /root/Player position:x 500 --op gt --timeout 3",
        extra_args=_register_wait_prop_args,
        preflight=_preflight_wait_prop,
        text_formatter=_fmt_wait_prop_text,
        exit_code_from=_exit_from_wait_prop,
    ),
    RpcSpec(
        name="wait-signal",
        handler=cmd_wait_signal,
        description=(
            "等信号发射（或 timeout），命中带回编码后的信号参数。退出码：0=命中, "
            "1=超时, 2=infra error。注意：必须先挂等待再触发动作（见 SKILL.md pitfall）。"
        ),
        positionals=(
            Positional("node_path", None, "绝对节点路径"),
            Positional("signal_name", None, "信号名，如 body_entered"),
            Positional("timeout", "?", "超时秒（0..3600，默认 5）"),
        ),
        example="wait-signal /root/Area door_opened 3",
        preflight=_preflight_wait_signal,
        text_formatter=_fmt_wait_signal_text,
        exit_code_from=_exit_from_wait_signal,
    ),
    RpcSpec(
        name="wait-frames",
        handler=cmd_wait_frames,
        description="等 N 个 process 帧（--physics 等物理帧）。确定性帧推进，替代短 sleep。",
        positionals=(),  # 由 extra_args 注册
        example="wait-frames 3 --physics",
        extra_args=_register_wait_frames_args,
        preflight=_preflight_wait_frames,
        text_formatter=lambda r: f"waited {r.get('frames')} frames",
    ),
```

- [ ] **Step 3: 跑通过**（subagent）→ **Step 4: Commit**：`git commit -m "feat(cli): wait-prop / wait-signal / wait-frames 子命令（#96）"`

### Task 3.7: e2e 直连 + 文档 + 全量 + PR

- [ ] **Step 1: e2e**（`test_e2e_client_direct.py` 追加）：真 daemon 下 `wait_frames(2)` 成功；`wait_property` 等一个 `set_property` 改出来的值（两连接：先起 wait 再 set 不好做单连接——直连测试里可先 set 后 wait eq 立即命中 + 一个 timeout 路径）；`wait_signal` timeout 路径（emitted=false、exit 语义在 CLI 层已测）。

- [ ] **Step 2: 文档**
  - SKILL.md 模板：三个命令进命令表 + 示例；错误码表加 1007；退出码表加 wait-prop/wait-signal 超时=1；pitfalls 加两条：① **wait-signal 先挂后触发**（`godot-cli-control wait-signal ... & godot-cli-control tap jump; wait` 或 `run` 脚本单连接）；② 消灭 magic sleep 的迁移建议（`wait-time 0.3` → `wait-prop`/`wait-frames`）。
  - addon README：命令表 + 1007。
  - CHANGELOG：feat 条目（#96）。

- [ ] **Step 3: 渲染验证 + 全量**（subagent）：GUT + `coverage run -m pytest` + ruff + `format_full_help` + `test_skills_install.py`，全绿、覆盖率 ≥80%。

- [ ] **Step 4: Commit + push + PR**

```bash
git add -A && git commit -m "docs+test: wait 原语文档同步 + e2e（#96）"
git push -u origin feat/96-wait-primitives
env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  gh pr create --base feat/99-100-get-codec \
  --title "feat(wait): wait-prop / wait-signal / wait-frames 条件等待原语 (#96)" \
  --body "Closes #96。<动机 / 设计要点 / 验证证据 / 竞态 pitfall 说明>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## 收尾（全部 PR 合并后）

- [ ] 项目 CLAUDE.md「已知遗留 issue」段补一行本批 land 记录（沿用现有格式）。
- [ ] 按全局规则盘点遗留问题开 issue（候选：wait-prop 的 Dictionary 深比较 tolerance 不递归 dict 值、wait-signal 单连接竞态的 `run` 脚本示例缺失、#102 time-scale 可以接着 #96 做）。
- [ ] 把三个 PR 链接贴给用户。
