# 一条命令广播多实例（`--instance all`）设计

日期：2026-06-07
状态：已与用户对齐（表面=`--instance all` + 保留名；退出码=全 0→0 否则 3；执行=asyncio.gather 并发；路径冲突=`{instance}` 占位符 + preflight 守卫）
关联：issue #145；前置 spec `2026-06-07-multi-instance-cli-design.md`（其非目标段的「编排层」方向）

## 背景 / 动机

PR #142 后每条 CLI 命令精确选靶单实例。对「同时给 4 个 client 截图」「全部实例跑
同一断言」这类编排需求，agent 只能 shell 循环逐实例调用——串行、啰嗦、截图时间点
不一致。需要一条命令逐实例执行并聚合输出。

先例：`daemon stop --all` 已确立逐目标聚合的信封形状
（`{"ok":true,"result":{"stopped":[...],"rc":0|3}}`）与 partial-failure 退出码 3
（`EXIT_PARTIAL`，`cli.py`）。本设计沿用并推广该先例。

## 目标

1. `--instance all` 让任一 RPC 子命令对当前项目全部活实例并发执行、聚合输出。
2. 聚合信封逐实例 entry 复刻单命令信封，agent 复用同一套解析。
3. 退出码语义单一可记：全过 0，否则 3；用法错仍 preflight 拦在连接前（64）。
4. 现有单实例工作流（`--instance <name>` / 不传）行为不变。
5. SKILL.md / CLAUDE.md 退出码表同步。

## 非目标

- GDScript 侧任何改动（纯 Python 客户端编排）。
- `run` 脚本与 `daemon` 子命令的广播（`daemon stop` 已有 `--all`；其余无场景）。
- 跨项目广播（全局注册表维度）；目标只在 cwd 项目内。
- legacy 平铺布局 daemon 作为广播目标（见「选靶」）。

## 设计

### 1. 表面形式与保留名

- `--instance all` 触发广播，仅对 RPC 子命令生效。复用现有标志，零新增表面。
- `all`（精确小写，大小写敏感）成为保留实例名：
  - `daemon start --name all` / `daemon run --name all` → `-1003` + exit 64，
    message 注明 "'all' is reserved for broadcast"。
  - 其余 `daemon <sub> --name all`（status/logs/stop）同样拒绝；`daemon stop`
    的报错提示改用现成 `--all`。
  - 校验点：`--name` 侧校验器拒绝 `all`；顶层 `--instance` 校验器放行（作为
    广播哨兵）。`_merge_instance_flags` 合并后流入 daemon 子命令的 `all` 一律拒绝。
- 迁移：#142 数日前刚落地，存量叫 `all` 的实例视为不存在；CHANGELOG 注明保留名。

### 2. 选靶

- 目标 = `list_live_instances(cwd)`（与 `daemon stop --all --project` 同一枚举
  路径：扫 `instances/*/`，PID 探活，已排序）。
- 0 个活实例 → `-1006` + exit 2（与「daemon 没起」同语义类）。message 提示
  legacy 平铺布局 daemon 不是广播目标（`instances/` 不存在时枚举为空；要广播请
  用命名实例重启）。注：`instances/default/` 目录存在但空 + legacy PID 存活时，
  `list_live_instances` 本就把 default 算活（#142 Task 1 语义），此时 default
  会进广播目标——不需要额外处理。
- 1 个活实例 → 仍走广播信封（数组长度 1），形状确定性优先。

### 3. JSON 信封与退出码

```json
{"ok": true, "result": {"instances": [
  {"instance": "client1", "ok": true, "result": true, "rc": 0},
  {"instance": "server", "ok": false, "error": {"code": 1002, "message": "..."}, "rc": 1}
], "rc": 3}}
```

- 顶层 `ok` 保持 `true`（同 `daemon stop --all`：广播本身执行了就算 ok，聚合码
  在 `result.rc`）。顶层 `ok:false` 只出现在广播自身的 preflight / infra 错。
- 逐实例 entry = 单命令信封（`ok` + `result`|`error`）+ `instance` + `rc`。
- 逐实例 `rc` 按原命令语义算：`exit_code_from` 有定义则用之（`exists`=false →
  1）、RPC 错 → 1、连接/IO 错 → 2。
- `instances` 数组按实例名排序（`list_live_instances` 已排序，gather 后按名归位）。
- 进程退出码：全部 entry rc=0 → 0；任一非 0 → 3。`EXIT_PARTIAL`(3) 的语义从
  「`daemon stop --all` 部分失败专用」扩展为「聚合操作部分/全部失败」，
  CLAUDE.md 退出码第 3 条与 SKILL.md 退出码表同步改写。
  `if godot-cli-control --instance all exists /root/Foo; then …` 天然表达
  「所有实例都存在」。
- `--text` 模式：逐实例一行，`[<name>] ` 前缀 + 该命令 `text_formatter` 输出；
  错误行 `[<name>] error: <message>`。

### 4. 执行模型

- `asyncio.gather` 并发：每实例读 `instances/<name>/port`、独立 `GameClient`
  连接、跑同一 `RpcSpec.handler`。「同时截图」≈ 同一时刻；wait-* 总耗时 =
  最慢实例而非求和。
- 单实例异常不拖死其他（逐实例捕获，落 entry 的 `error`/`rc`）。
- 入口分流：`cli.py` `_run_rpc`（RPC 主路径）在 `ns.instance == "all"` 时走
  广播分支，否则原路径不动。

### 5. `{instance}` 占位符

- 广播模式下，对该 RPC 子命令解析后 namespace 的字符串型参数值（位置参数 +
  选项值）做逐实例 `{instance}` → 实例名替换；每实例深拷贝 namespace。
- 通用机制，不限 screenshot（set-property 的 JSON 字面量里写 `{instance}` 也会
  被替换——SKILL.md pitfall 注明，无转义口子，YAGNI）。
- 非广播模式零替换、零行为变化。
- `screenshot` 加 preflight 守卫：`ns.instance == "all"` 且 path 不含
  `{instance}` → `-1003` + exit 64（连接前拦截，防多实例互覆盖）。挂在现有
  `RpcSpec.preflight` 槽位（screenshot 当前无 preflight，新增）。

### 6. 实现锚点

| 改动 | 位置 |
|---|---|
| 广播分支（枚举/并发/聚合/信封） | `cli.py` `_run_rpc` 附近，新函数 `_run_rpc_broadcast` |
| `--name` 保留名拒绝 | `cli.py` `_add_instance_name_flag` 的校验器 + `_merge_instance_flags` 下游 |
| screenshot preflight | `cli.py` screenshot `RpcSpec` 加 `preflight` |
| `run` 拒绝广播 | `cli.py` `cmd_run` 入口 instance 解析处 |
| `{instance}` 替换 | `cli.py` 广播分支内，namespace 深拷贝工具函数 |
| 退出码常量 | 复用 `EXIT_PARTIAL`(3)，无新码 |

GDScript / `client.py` / `bridge.py` / `daemon.py` 零改动（枚举与端口读取均有
现成函数）。无新 RPC、无新错误码（复用 `-1003` / `-1006`）。

## 测试

- 单测（`python/tests/`）：
  - 保留名：`daemon start/run/status/logs/stop --name all` 全拒（64，stop 提示 `--all`）。
  - `--instance all run script.py` 拒绝（64）。
  - screenshot preflight：广播缺 `{instance}` → 64，连接前（不依赖 daemon）。
  - `{instance}` 替换：位置参数 / 选项值 / 非字符串字段不动 / 非广播零替换。
  - rc 聚合矩阵：全成→0、混合→3、全败→3、0 实例→2（-1006）。
  - 并发广播打假服务端（沿用现有 fake websocket server 测试设施）：信封形状、
    数组排序、单实例连接失败落 rc=2 不拖死其他。
- e2e（`GODOT_BIN`，本机真跑）：`godot_instances` 工厂起 2 实例，
  `--instance all exists` + `--instance all screenshot /tmp/s-{instance}.png`
  验证两文件落盘、聚合 rc。

## 文档同步

- SKILL.md 模板：Multi-instance 小节加 broadcast 段（示例 + 信封形状 + 退出码
  3 语义 + pitfalls：`{instance}` 全量替换、保留名 `all`、0 活实例报 -1006）；
  重渲染仓内 `.claude/skills` 副本（COLUMNS=80 + Python 3.12）。
- CLAUDE.md：退出码第 3 条从「stop --all 专用」改为「聚合操作（stop --all /
  --instance all 广播）部分/全部失败」。
- CHANGELOG `[Unreleased]`：feature + 保留名注记。
- addon README：不涉及（无新 RPC / 错误码）。
