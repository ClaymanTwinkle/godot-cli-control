# 设计：`tree [path] [depth]` 子树查询 + 文档对齐（issue #150）

- 日期：2026-06-08
- 关联 issue：#150（bug/docs）
- 状态：已通过 brainstorming 评审，待写实施计划

## 背景与问题

issue #150 指出 `tree` 命令两处脱节：

1. **文档承诺的 `tree <subpath>` 不存在。** `SKILL.md` 错误码 1005 的指引写着
   “Pass `--max-nodes` or query a subtree with `children` / `tree <subpath>`”，
   但 `python/godot_cli_control/cli.py` 的 `_register_tree_args` 只注册了 `depth`
   位置参数和 `--max-nodes`，没有路径参数。按文档敲 `tree /root/GameUI 3` 直接被
   argparse 拒绝。
2. **dump 根是 `current_scene`，autoload 兄弟子树不可见。**
   `addons/godot_cli_control/bridge/low_level_api.gd:427` 的 `handle_get_scene_tree`
   取 `get_tree().current_scene` 作根。autoload 单例挂到 `/root` 下的兄弟子树
   （下游 XGame 的 `World`、`GameUI` 都是这种结构）在 `tree` 输出里完全看不到。

而 `children` / `get` / `set` 等命令全部走 `_get_node_or_error` →
`get_tree().root.get_node_or_null(path)`（`low_level_api.gd:538`），即「绝对路径以
`/root` 开头」的统一世界观。`tree` 是唯一以 `current_scene` 为根、且不接受 path 的
例外。

## 已定决策（brainstorming 结论）

- **决策 A — 位置参数设计：启发式位置参数。**
  第一个位置参数以 `/` 开头时当 path，否则当 depth。`tree /root/GameUI 2` 与
  `tree 3` 都成立，完全向后兼容，契合「绝对路径以 `/root` 开头」世界观。
  （备选「`--path` flag」零歧义但与 issue 想要的位置形式及 `children` 的位置 path
  不一致；备选「纯位置 `tree [path] [depth]`」会破坏 `tree 3`，均被否决。）
- **决策 B — 默认根：维持 `current_scene`，只新增 path 参数。**
  path 参数本身已解掉 autoload 盲区（显式 `tree /root` 即可看兄弟子树），不必再叠加
  「默认根改 `/root`」这一行为变更的破坏风险（会让现有 `tree` 输出多一层 + 带上所有
  autoload，可能撑大输出并打破现有 GUT/e2e 测试与下游脚本对输出形状的依赖）。

## 设计

### 1. CLI 参数（`python/godot_cli_control/cli.py`）

`_register_tree_args` 把单个 `depth` 位置参数改为两个可选位置参数 `arg1` `arg2`
（命名仅内部用），保留 `--max-nodes`：

```
tree                  # path=None, depth=3   （现状不变）
tree 2                # path=None, depth=2   （现状不变，兼容）
tree /root/GameUI     # path=/root/GameUI, depth=3
tree /root/GameUI 2   # path=/root/GameUI, depth=2
```

新增 `_preflight_tree(ns)`（连 daemon 前跑，与 `_preflight_step_frames` 等同款，
抛 `ValueError` → `EXIT_USAGE`（64 / `-1003`））：

1. **消歧**：`arg1` 以 `/` 开头 → `path = arg1`，depth token = `arg2`；
   否则 `path = None`，depth token = `arg1`，且若 `arg2` 非空 → 抛 `ValueError`
   （depth-only 形式不该有尾随 token）。
2. **depth 校验**：depth token 非空时校验可解析为非负 int，失败抛 `ValueError`，
   错误信息提示「节点路径须以 `/` 开头，如 `tree /root/GameUI`」——这样
   `tree GameUI`（漏斜杠）会 fail-loud 当成「非法 depth」而非静默被当 depth 吞掉。
3. **stash**：把结果写到 `ns._tree_path` / `ns._tree_depth`，供 handler 复用，
   避免运行期二次解析。

`cmd_tree` 改为读 `ns._tree_path` / `ns._tree_depth`，调用
`client.get_scene_tree(depth=ns._tree_depth, max_nodes=ns.max_nodes, path=ns._tree_path)`。
`RpcSpec` 的 `example` 更新为 `tree /root/GameUI 2`，`preflight=_preflight_tree`。

### 2. 客户端（`python/godot_cli_control/client.py` + `bridge.py`）

`GameClient.get_scene_tree(self, depth=5, max_nodes=None, path=None)`：`path` 非
`None` 时塞进 `params["path"]`。`GameBridge.tree(...)` 同步包装同步新增 `path` 形参。

### 3. 服务端（`addons/godot_cli_control/bridge/low_level_api.gd`）

`handle_get_scene_tree` 起始处的根解析改为：

```gdscript
var path: String = params.get("path", "") as String
var root: Node
if not path.is_empty():
    root = get_tree().root.get_node_or_null(path)
    if root == null:
        return _node_not_found(path)   # 复用 1001 NODE_NOT_FOUND，与 children 同
else:
    root = get_tree().current_scene
    if root == null:
        root = get_tree().root         # 现有 fallback 保留
```

其余逻辑（`max_nodes` clamp 到 `_BUILD_TREE_MAX_NODES`、`_build_tree` 递归、1005
硬墙、`truncated` / `total_nodes` 软信号）一律不动。**默认根维持 `current_scene`。**

### 4. 错误码 / 退出码（无新增码）

| 场景 | code | exit |
|------|------|------|
| `tree /root/typo`（路径不存在） | `1001` NODE_NOT_FOUND（复用，同 `children`） | 1 |
| `tree GameUI` / `tree 2 3` / `tree abc`（用法错） | `-1003`（preflight） | 64 |
| 子树仍超 5000 节点 | `1005`（不变） | 1 |

契约 #2 要求加码前查重：本设计**全部复用现有码**，不新增、不撞码。

### 5. 文档同步（契约 #7：改 CLI 必改 SKILL.md）

模板 `python/godot_cli_control/templates/skill/SKILL.md`：

- 命令表（约 L262）：`tree [depth] [--max-nodes N]` → `tree [path] [depth] [--max-nodes N]`，
  注明「省略 path 时根为当前场景（`current_scene`）；`tree /root` 可查 autoload 挂在
  `/root` 下的兄弟子树」。
- 错误码 1005 指引（约 L222）、subtree 相关段落（约 L382–396、L588）：把
  `tree <subpath>` 措辞坐实为 `tree <path>`，确保文档与实现一致。
- Python API 对照表（约 L472）：`get_scene_tree(depth, max_nodes=None)` →
  加 `path=None`。
- addon `README.md` 命令表同步。
- 重渲染仓内 `.claude/skills/.../SKILL.md`：`COLUMNS=80` + Python 3.12 跑
  `skills_install.render_skill`（CLAUDE.md pitfall：argparse usage 折行随 Python
  小版本变，CI skill-render-drift 以 3.12 为准）。

### 6. 测试（TDD）

- **Python（pytest）**：
  - `_preflight_tree` 消歧矩阵：`tree` / `tree 2` / `tree /root/X` / `tree /root/X 2`
    解析正确；`tree GameUI`（漏斜杠）、`tree 2 3`（depth-only 带尾随）、`tree abc`
    （非法 depth）均 → `EXIT_USAGE`（64）。
  - `cmd_tree` 把 path 正确透传给 client。
  - `client.get_scene_tree` 在 path 非 None 时把 `path` 放进 RPC params。
- **GDScript（GUT）**：
  - `handle_get_scene_tree` 传 path → 返回该子树根；
  - 传不存在的 path → 返回 1001；
  - 不传 path → 仍以 `current_scene` 为根（行为回归保护）；
  - 构造 autoload-on-`/root` 结构，验证 `tree /root` 能看到兄弟子树。
- 复跑 `python/tests/test_skills_install.py`，确认 `init` 注入渲染不崩。

## 影响面与兼容性

- 现有 `tree` / `tree <depth>` 调用行为**完全不变**（默认根、输出形状都不动）。
- 纯增量：新增可选 path 能力 + 把原本静默/报错不清的用法错变 fail-loud。
- 不触碰 localhost-only / blacklist 安全网，不触碰大 payload trim 策略。

## 非目标（YAGNI）

- 不改默认根为 `/root`（决策 B）。
- 不实现 issue #150 之外的 `find` 命令（另有独立 feature issue）。
- 不为 `tree` 增加 `type_filter` 等 `children` 才有的过滤能力。
- 不改 depth 的取值范围语义（沿用服务端现有 clamp）。
