# 设计：输入事件管线 + 条件等待原语 + get 编码/原子读

日期：2026-06-04
对应 issue：[#97](https://github.com/ClaymanTwinkle/godot-cli-control/issues/97)、[#96](https://github.com/ClaymanTwinkle/godot-cli-control/issues/96)、[#99](https://github.com/ClaymanTwinkle/godot-cli-control/issues/99)、[#100](https://github.com/ClaymanTwinkle/godot-cli-control/issues/100)
状态：已获 maintainer 口头批准（设计分节确认），待 spec review

## 背景与目标

三组下游（XGame 项目）实测反馈的能力缺口：

1. **#97**：`press`/`tap`/`hold`/`combo` 走 `Input.action_press()`，只翻轮询状态位，
   事件回调式（`_input()` / `_unhandled_input()`）的游戏输入**静默无效**。
2. **#99 + #100**：`get` 对复合 Variant（Vector2 等）返回未定义的 `"(x, y)"` 字符串编码，
   与 set 侧 array schema 不对称、无法 round-trip；且一次只能读一个属性，
   跨 RPC 读「一对相关状态」存在跨帧竞态。
3. **#96**：等待原语只有 `wait-node` / `wait-time`，下游 e2e 充斥 magic sleep
   （0.15~0.3s 经验值 ×10+ 处），慢且 flaky。

交付方式：**三个 PR 按依赖序**——PR1(#97) 独立最先；PR2(#99+#100)；PR3(#96，依赖 PR2 编码器)。

## PR1 — #97 输入注入走事件管线

### 改动

`addons/godot_cli_control/bridge/input_simulation_api.gd` 的 `_do_press` / `_do_release`
（现 273-280 行）从 `Input.action_press(action)` / `Input.action_release(action)` 改为：

```gdscript
func _do_press(action: String) -> void:
    if InputMap.has_action(action):
        var ev := InputEventAction.new()
        ev.action = action
        ev.pressed = true
        ev.strength = 1.0
        Input.parse_input_event(ev)
```

`_do_release` 同理（`pressed = false`）。**直接切换，无开关、无 legacy 路径**（已确认）。

press/release/tap/hold/combo 五条命令与断连 `release_all` 兜底共用这两个函数，全部自动受益。

### 行为论证

- `Input.parse_input_event(InputEventAction)` 同样更新 action 状态位 →
  轮询式 API（`is_action_pressed` / `get_vector`）**不回归**。
- 事件经 Viewport 输入管线分发 → `_input()` / `_unhandled_input()` 从「收不到」变「正常收到」。

### 已知边界（写入 SKILL.md）

1. `InputEventAction` 无坐标，依赖鼠标位置的 `_gui_input` 控件不受益——继续用 `click`。
2. 输入缓冲（input buffering）可能让状态在下一帧输入泵时才可见。
   GUT 测试在断言前调 `Input.flush_buffered_events()` 或等一帧消除时序差；
   对 agent 的实际影响为 0（RPC 往返远大于一帧）。

### Python 侧

零代码改动。SKILL.md 模板更新输入命令组说明：正面声明事件管线可见性
（替换原「事件式游戏收不到输入」的隐性陷阱），保留 `_gui_input` 边界提示。

## PR2 — #99 Variant 编码器 + #100 get_properties

### 新文件 `addons/godot_cli_control/bridge/variant_codec.gd`

`class_name CliControlVariantCodec`，static 函数，单一职责：Variant → JSON-safe 编码。
**全函数**——任何 Variant 都有确定输出、不报错，杜绝「响应永远不发出」类挂死。

编码规则（与 set 侧 `_coerce_array_to_declared_type` 的 array schema **完全对称**，
axis-vector 顺序，见 low_level_api.gd 178-184 行注释）：

| Variant 类型 | value 编码 | type 字段 |
|---|---|---|
| bool / int / float / String / null | 原样 | 无 |
| Array / Dictionary | 递归编码元素 | 无 |
| Packed*Array | 转普通数组 | 无 |
| Vector2/2i | `[x, y]` | `"Vector2"` / `"Vector2i"` |
| Vector3/3i、Vector4/4i | `[x, y, z(, w)]` | 对应类型名 |
| Rect2/2i | `[pos.x, pos.y, size.x, size.y]` | 对应类型名 |
| Color | `[r, g, b, a]`（恒 4 元） | `"Color"` |
| Plane | `[normal.x, normal.y, normal.z, d]` | `"Plane"` |
| Quaternion | `[x, y, z, w]` | `"Quaternion"` |
| AABB | `[pos.xyz, size.xyz]`（6） | `"AABB"` |
| Basis | 9 floats（axis-vector） | `"Basis"` |
| Transform2D | 6 floats | `"Transform2D"` |
| Transform3D | 12 floats（basis 9 + origin 3） | `"Transform3D"` |
| Projection | 16 floats | `"Projection"` |
| StringName / NodePath | `str()` | `"StringName"` / `"NodePath"` |
| Object（Node/Resource 引用） | `str(obj)` | `"Object"` |
| 非有限 float（inf/-inf/nan） | 字符串 `"inf"` / `"-inf"` / `"nan"` | 无 |

设计取舍（均已确认 / 文档声明）：

- **type 字段只在顶层出现**。嵌套在 Array/Dictionary 内的复合 Variant 递归编码为数组但
  不带嵌套 type——需要类型时用 sub-path 读 leaf。
- 非有限 float 编码为字符串：`JSON.stringify` 对 inf/nan 会产出非法 JSON 撑爆客户端解析，
  防御性转字符串并在 SKILL.md 声明。
- round-trip 闭环：`get` 输出的 value 数组可原样灌回 `set`（set 侧 #54 已覆盖全部复合类型）。
- Color 不对称提示：set 侧接受 3 或 4 元素，codec **恒输出 4 元素**——round-trip 仍成立
  （4 灌 4），GUT round-trip 测试按 4 元素断言。

### `get_property` 改造（#99）

- 返回 `{"value": <编码后>, "type": <仅复合类型>}`（原来是 `{"value": <裸 Variant>}`）。
- **顺带补 sub-path 读**：`get /root/Player position:x` 走 `node.get_indexed(NodePath(prop))`，
  与 set 侧 sub-path 写对称；校验方式：冒号前的 top-level 名必须在 property list 中（否则 1002）。
  这也是 PR3 wait-prop `position:x > 500` 场景的前置。

### `get_properties` RPC（#100）

- params：`{path, properties: ["a", "b", ...]}`；properties 必须是非空字符串数组（否则 -32602）。
- sync handler（无 await）天然同帧执行，满足原子快照语义。
- **原子失败**：任一属性缺失 → 1002 错误并在 message 中点名**全部**缺失项，不返回半截结果。
- 成功返回：`{"values": {"a": {"value": ..., "type": ...}, "b": {...}}}`（每项与单 get 编码一致）。
- 元素允许 sub-path。
- 在 `game_bridge.gd` `_register_methods()` 注册 `get_properties`（kind: "sync"）。

### Python / CLI 侧

- CLI：`get <path> <prop> [prop2 ...]`（nargs="+"）。单属性走老 `get_property`（向后兼容），
  ≥2 属性走 `get_properties`。文本模式格式化器对应更新。
- `client.py`：`get_property` 返回形状不变（裸 value，但 Vector2 从 `"(x, y)"` 字符串变
  `[x, y]` 数组）；新增 `async get_properties(path, props) -> dict[str, Any]`（裸 value 映射，
  type 信息走 CLI JSON 信封 / 原始 RPC 拿）。
  SKILL.md pitfalls 补一句：Python API（`bridge.get_property`）只暴露裸 value，
  需要 type 字段时走 CLI `get`（JSON 信封带 type）。
- `bridge.py`：同步包装跟上。

### 行为变更声明

`get` 对复合 Variant 的返回从 `"(x, y)"` 字符串变为数组 + type 字段，
信封 result 从裸值变为 `{"value", "type"}` 对象。属 breaking change：

- CHANGELOG 顶部显著标注 + README「Recent changes」加条目；
- SKILL.md JSON 信封示例、get 命令说明、common pitfalls 同步；
- 作为 minor 版本（0.x 阶段语义）发布。
- 下游 XGame 的双格式防御解析本来就有数组分支，自动受益。

## PR3 — #96 条件等待原语（依赖 PR2 编码器）

三个 async handler 落 `low_level_api.gd`，在 `game_bridge.gd` `_register_methods()`
注册为 `kind: "async"`（复用 `wait_for_node_async` 模式，`_in_flight` 计数自动保护
idle-timeout）。

### `wait-prop` → RPC `wait_property`

```
wait-prop <path> <prop> <value> [--op eq|ne|gt|lt|ge|le] [--timeout 5] [--tolerance 0]
```

- 逐帧轮询（`await get_tree().process_frame`），每帧读属性 →
  CliControlVariantCodec 编码 → 与期望值比较。
  **属性读取复用 PR2 落地的 sub-path 路径**（`get_indexed(NodePath(prop))` +
  top-level 名校验），不内联第二份实现。
- 比较语义：数值标量支持全部 6 个 op（int/float 统一数值比较）；
  复合类型（编码后为数组）仅 eq/ne，逐元素比较；字符串/bool 仅 eq/ne。
  `--tolerance <eps>` 给 float eq/ne 近似比较（默认 0 = 精确）。
- timeout 默认 5s，上限 3600（与 `_MAX_WAIT_SECONDS` 一致）。
- **超时不报错**：返回 `{"matched": false, "reason": "timeout|node_not_found|property_not_found",
  "value": <最后一次读到的编码值或 null>}`。节点/属性中途出现也能等到（容忍动态场景），
  typo 靠 reason 诊断。命中返回 `{"matched": true, "value": <编码值>, "waited": <秒>}`。
- 退出码与 wait-node 对齐：命中 0，超时 1（`exit_code_from`）。

### `wait-signal` → RPC `wait_signal`

```
wait-signal <path> <signal> [--timeout 5]
```

- 节点不存在 → 1001 立即报错（先 `wait-node`）；信号不存在 → **新错误码 1007 SIGNAL_NOT_FOUND**。
- 实现：CONNECT_ONE_SHOT 连接 + SceneTreeTimer 竞速，first-wins 标志防双发；
  超时路径必须显式断开连接（清理悬挂 Callable）。
- 信号参数 arity：GDScript 无变参 Callable，按 `get_signal_list()` 的 argc 用 0..8 参
  capture 函数分发；argc > 8 → -32602（病态场景，拒绝优于截断）。
  动态信号（`add_user_signal`）声明的参数同样进 `get_signal_list()`，按报告的 argc 处理；
  「声明 argc 与实际发射参数数不符」是游戏侧 bug（Godot emit 本身会报错），不在兜底范围。
- 命中：`{"emitted": true, "args": [<编码后参数>]}`（Object 参数降级为 `str()` + type）；
  超时：`{"emitted": false}`。退出码：命中 0，超时 1。
- **竞态 pitfall 写入 SKILL.md**：信号可能在 wait-signal 连接之前就发射。
  必须先挂等待再触发动作。每条 CLI 命令独立连接，shell 模式：
  `godot-cli-control wait-signal ... & godot-cli-control tap jump; wait`；
  需要严格单连接时用 `run` 脚本（`def run(bridge):`）。

### `wait-frames` → RPC `wait_frames`

```
wait-frames <n> [--physics]
```

- 等 n 个 `process_frame`（`--physics` 改 `physics_frame`）。
- n 必须为 1..3600 的整数（上限防呆，对应 60fps 下 1 分钟）；CLI preflight + 服务端双重校验。
- 返回 `{"success": true, "frames": n}`，退出码 0。

### CLI preflight（全部连接前拦截）

- op 不在白名单 / timeout、tolerance 非数字或超界 / n 非正整数或超界 →
  ValueError → -1003 + exit 64。
- 「复合 value（JSON 数组）+ 非 eq/ne op」组合在 preflight 拦截。

### 客户端超时

三个 wait 沿用 wait-node 的「server timeout + grace」客户端 wall-time 模式
（wait-frames 用 n/最低帧率估算 + grace），不触 LONG_OP 600s 生死线。

## 错误处理汇总

- 新增错误码：仅 **1007 SIGNAL_NOT_FOUND**（`error_codes.gd` 登记 + SKILL.md 错误码表 +
  addon README 同步）。其余复用 1001（节点）、1002（属性）、-32602（参数）。
- 编码器全函数无错误路径。
- 所有新 handler 错误都落 `{"error": {code, message}}` dict 返回，由 `_dispatch_result`
  统一发信封——不引入新的响应路径。

## 测试策略

### GUT（GDScript，`run_gut.sh` / `run_gut.py`，需 GODOT_BIN）

- PR1：test double 节点捕获 `_input` / `_unhandled_input`，断言 press/tap/hold/combo
  注入的 InputEventAction 可见；轮询路径（`is_action_pressed`）不回归；release_all 兜底。
- PR2：编码器逐类型表驱动测试（上表全覆盖）；round-trip 测试（get 出的数组灌回
  set 再 get 等值）；get_properties 原子语义 / 缺属性点名报错 / sub-path。
- PR3：wait_frames 帧数准确性；wait_prop 命中 / 超时 / 6 op 矩阵 / tolerance /
  reason 字段；wait_signal 命中 / 超时 / 参数编码 / one-shot 与超时双路径的连接清理。

### pytest（经 subagent 执行，覆盖率守 80%）

- RpcSpec 单测：preflight 矩阵、exit code、text formatter。
- client.py / bridge.py 新方法（mock 协议层 + test_e2e_client_direct.py 真连扩展）。
- headless e2e：#97 用「`_unhandled_input` 翻状态变量」的测试场景验证端到端；
  wait-prop 等移动节点属性；wait-frames 真实帧推进。

### 文档渲染验证

每个 PR 收尾跑 `python -c "from godot_cli_control import cli; print(cli.format_full_help())"`
确认 SKILL.md 模板渲染不崩；`pytest python/tests/test_skills_install.py` 验 init 注入。

## 文档与发布

- 每个 PR 同步：SKILL.md 模板（命令表 / 错误码表 / 退出码表 / pitfalls / 信封示例）、
  addon README、CHANGELOG。
- PR2 行为变更在 CHANGELOG 顶部显著标注，README Recent changes 加条目。
- 新增命令进 `RpcSpec` 自动入 `--help` / SKILL.md 的 `{{cli_help}}` 注入。

## 明确不做（YAGNI，已确认）

- #100 跨节点版本（`[{path, prop}, ...]`）：RPC 参数留扩展余地，真实需求出现再加。
- #97 legacy 开关 / ProjectSettings 退路：直接切换。
- wait-prop 字符串字典序 gt/lt：仅数值。
- #98（scene reload）、#101（sprite-info）、#102（time-scale）、#103（diag）不在本批。
