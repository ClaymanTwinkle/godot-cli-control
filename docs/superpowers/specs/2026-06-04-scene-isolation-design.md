# 场景隔离设计：scene-reload / scene-change RPC + pytest fresh_scene（issue #98）

日期：2026-06-04
状态：已批准（brainstorming 四节逐节确认）

## 问题

daemon 跨整个 pytest session 存活，场景状态跨用例累积，没有重置手段。下游 e2e
被迫为「跨用例残留」写防御代码（XGame `enter_world` 幂等复用、每用例基准动作
消残留），且只能缓解不能根治——该项目真实出过一次 e2e 顺序依赖 bug。

## 目标

1. RPC 级原语：`scene_reload`（重载当前场景）+ `scene_change <res-path>`（切换场景），
   两者都**等新场景 ready 才返回**，调用方不需要再自己 wait-node。
2. pytest 级封装：function-scope `fresh_scene` fixture，声明它的用例开始时
   拿到刚 reload 的干净场景。

## 已决策的设计选择

| 决策点 | 选择 | 备注 |
|---|---|---|
| pytest API 形态 | 仅 fixture，不加 marker | YAGNI；与现有 `bridge` fixture 同风格 |
| fresh_scene 还原目标 | reload 当前场景 | 只保证「本用例拿到干净场景」；跨场景用例自己先 scene_change |
| CLI 形态 | 平铺 `scene-reload` / `scene-change` | 对齐 wait-node 先例，直接走 RpcSpec |
| GDScript 放置 | 独立 `scene_api.gd` | 对齐 #108 把 wait_api 拆出去的先例 |
| 等 ready 超时语义 | 算错误（1008 + exit 1） | wait 系列超时是合法业务结果；scene 超时意味着场景加载坏了 |
| 错误码 | 单码 `SCENE_UNAVAILABLE = 1008` | 三种失败共用，message 区分细节 |

## 组件设计

### 1. GDScript：`addons/godot_cli_control/bridge/scene_api.gd`

新文件，`extends Node`，命名 `SceneApi`。GameBridge `_ready` 时实例化并
`add_child`（对齐 `_wait_api` 先例，game_bridge.gd:58-61），注册：

```
_methods["scene_reload"] = {"callable": _scene_api.scene_reload_async, "kind": "async"}
_methods["scene_change"] = {"callable": _scene_api.scene_change_async, "kind": "async"}
```

**`scene_reload_async(params: Dictionary) -> Dictionary`**

1. `timeout` 可选 float，默认 10.0；非法类型 → `-32602`
2. `old := get_tree().current_scene`；为 null → `1008`（"no current scene to reload"）
3. `get_tree().reload_current_scene()` 返回非 OK → `1008`（带 Error 码）
4. 逐帧 `await get_tree().process_frame`，直到 `current_scene != null` 且
   **实例不同于 old**（`get_instance_id()` 比对，reload 后 scene_file_path 不变，
   只能比实例）且 `current_scene.is_node_ready()`
5. 超时 → `1008`（"timeout waiting for new scene ready"）
6. 成功返回 `{"scene_path": current_scene.scene_file_path, "name": String(current_scene.name)}`

**`scene_change_async(params: Dictionary) -> Dictionary`**

1. `path` 必填 String，缺失/型错 → `-32602`；`timeout` 同上
2. `ResourceLoader.exists(path, "PackedScene")` 为 false → `1008`
   （不碰当前场景就失败，避免半切换状态）
3. `get_tree().change_scene_to_file(path)` 返回非 OK → `1008`
4. 等 ready 逻辑与 reload 共用（私有 helper `_await_new_scene_ready(old, timeout)`）
5. 成功返回同 reload

### 2. 错误码

`error_codes.gd` 新增 `SCENE_UNAVAILABLE = 1008`，覆盖：

- reload 时无 current scene
- change 的 res 路径不存在 / 加载失败 / change_scene_to_file 返回 Error
- 等新场景 ready 超时

### 3. Python 三层

- **client.py**：`async def scene_reload(self, timeout: float = 10.0) -> dict`、
  `async def scene_change(self, path: str, timeout: float = 10.0) -> dict`；
  RPC 网络超时 = 业务 timeout + 5.0（对齐 wait_signal 先例）
- **bridge.py**：两个同步包装
- **cli.py**：两个 `RpcSpec`：
  - `scene-reload [--timeout N]`
  - `scene-change <res-path> [--timeout N]`
  - preflight（仅 scene-change）：path 必须以 `res://` 或 `uid://` 开头，
    否则连 daemon 前 `-1003` + exit 64（契约 5）
  - text_formatter：`scene reloaded: res://main.tscn (root: Main)` /
    `scene changed: res://second.tscn (root: Second)`
  - 退出码走默认语义（0 成功 / 1 RPC 错 / 2 连接错 / 64 用法错），
    不需要 `exit_code_from`

### 4. pytest plugin

```python
@pytest.fixture
def fresh_scene(bridge: GameBridge) -> Iterator[GameBridge]:
    bridge.scene_reload()
    yield bridge
```

- setup 时 reload 并等 ready；teardown 不动作
  （语义：「本用例开始时场景是干净的」，下个要干净的用例自己声明）
- 放在 `pytest_plugin.py` 现有 `bridge` fixture 之后

## 测试策略

| 层 | 文件 | 覆盖 |
|---|---|---|
| GUT | `tests/gut/test_scene_api.gd`（新） | 参数校验 -32602；无 current_scene → 1008；路径不存在 → 1008。真 reload/change 受 GUT script-mode runner 环境限制，由 e2e 兜底 |
| Python 单测 | test_cli.py / test_bridge.py / test_client.py | preflight 前缀校验、text_formatter、RpcSpec 注册完整性、bridge/client 包装（mock transport） |
| e2e | `test_e2e_scene.py`（新） | platformer-demo 加 `second.tscn`；真跑：改属性 → scene-reload → 属性归位；scene-change → tree 验证新根节点；fresh_scene fixture 全链路 |

## 文档同步（契约 7）

- SKILL.md 模板：Command catalogue 加 **Scene 分组**（scene-reload / scene-change）、
  错误码表加 1008、pytest plugin 段加 fresh_scene、Common pitfalls 加
  「场景切换后旧节点路径全部失效，需重新 wait-node 定位」
- addon README：命令表 / 错误码表同步
- 跑 `python -c "from godot_cli_control import cli; print(cli.format_full_help())"` 验证渲染

## 不做的事（YAGNI）

- 不加 `@pytest.mark.fresh_scene` marker
- 不做「恢复到主场景 / 可配置目标场景」
- 不做 `change_scene_to_packed` / 场景栈管理
- 不动 method blacklist（scene 操作走专用 RPC，不经 `call` 通道）
