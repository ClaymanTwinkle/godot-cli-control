# wait-signal `--trigger`：同连接「先挂后触发」消灭 shell 后台与竞态（#155）

## 背景

每次 CLI 调用是独立连接，`wait-signal` 必须**先挂等待再触发动作**——信号在
`connect` 之前发射不会被捕获。shell 里只能写三步后台模板：

```bash
godot-cli-control wait-signal /root/Area door_opened &
godot-cli-control tap interact
wait $!
```

问题：① 三步模板（`&` 后台 + 触发 + `wait`，还要自己接背景任务 exit code）；
② 竞态心智负担——后台进程「连上并完成 arm」与前台触发之间没有同步点，触发太快
仍可能漏，目前只能靠 `sleep` 拍脑袋；③ agent / 脚本生成场景里这是最易写错的
shell 形态之一。SKILL.md 已把它写成 pitfall，但 pitfall 本身就是体验债。

## 目标 / 非目标

**目标**：`wait-signal` 加 `--trigger '<subcommand>'`，**同一连接内**完成
`arm → 执行触发子命令 → 等信号/超时`，竞态从协议层根除；触发子命令复用本 CLI
解析器在**同进程内**执行（非 shell 透传）。

**非目标**：不改普通 `wait-signal`（不带 `--trigger`）的任何行为（零回归）；不做
多个 `--trigger` 串联（多步触发用 `combo` 作为单条子命令覆盖）；不引入服务端跨
RPC 的持久状态。

## 架构决策：方案 A（arm-ack 中间帧）

### 现状洞察

- `wait_signal` 当前是**单请求-单响应**：服务端 handler 内同步 `connect`（arm）
  → `while` 等待 `capture.fired` / timeout → 返回。client 拿不到「arm 完成」时刻，
  这就是竞态根源（`bridge/wait_api.gd:wait_signal_async`）。
- 框架**已有 `async_with_id` kind**（`input_combo` 在用）：handler 收 `(params, id)`，
  自行决定何时调 `_on_async_response(id, result)`，底层 `_send_json` 可在 `await`
  期间任意发帧（`bridge/game_bridge.gd`）。Godot 单线程 + 协程 `await process_frame`
  让出 → 等信号期间主循环仍能 poll WebSocket、处理同连接上的 trigger RPC。

### 选 A 不选 C 的理由

| | 方案 A：arm-ack 中间帧 | 方案 C：arm/collect 两 RPC |
|---|---|---|
| 协议 | `wait_signal` 仍单 RPC（一个 id，多一条进度帧） | 两个标准单请求-单响应 RPC |
| 服务端状态 | **无跨调用状态** | armed token 表 + 超时 GC（新泄漏面） |
| client 改动 | 需识别「中间帧 vs 终帧」 | 不碰 client 核心 |
| 复杂度落点 | 一条额外进度帧 | 服务端持久状态 + GC |

选 **A**：把复杂度收在「一条额外进度帧」，而不是「服务端持久状态 + GC」——后者
正是契约 #6 警惕的『新增没想清清理策略的状态』；且 `async_with_id` 已被 combo
验证可行。

## CLI 表面

```
wait-signal <node_path> <signal> [timeout] --trigger '<一条 RPC 子命令>'
```

- `--trigger` 收一个字符串 = **一条 RPC 子命令**（位置形式）：
  `'tap interact'` / `'click /root/Btn'` / `'emit-signal /root/A sig 1'`。
- 多步触发用 `combo` 当这一条：`--trigger 'combo [...]'`。
- 不传 `--trigger` → 完全现状（单 RPC、无 `arm_ack`），**零回归**。

## 协议（方案 A）

- `wait_signal` 升级为 `async_with_id`；请求新增可选 `arm_ack: true`（仅 `--trigger`
  路径传）。
- 服务端 `wait_signal_async(params, id)`：
  1. 校验节点 / 信号 / timeout——**失败照旧发 error 帧**（client 据此不触发、直接报错）。
  2. 校验通过 + `connect(... CONNECT_ONE_SHOT)` 后：若 `arm_ack`，先
     `_send_json({"id": id, "armed": true})` 发进度帧。
  3. 进入现有 `while` 等待 `capture.fired` / timeout / node_freed。
  4. 最终 `_on_async_response(id, {"emitted":..., "args"/"reason":...})` 发终帧。
- 不传 `arm_ack` → 不发 armed 帧，等价现状（仍可保留 `async_with_id` 路径，只是
  跳过 armed 帧那一步）。

## client 编排（同一 GameClient 连接）

- `GameClient.wait_signal(..., on_armed: Awaitable | None = None)`：
  - 发 `wait_signal{arm_ack:true}` request[A]。
  - 响应匹配区分：**armed 中间帧**（有 `armed`、无 `result`/`error`）vs **终帧**。
    仅对注册了 `on_armed` 的 pending 识别 armed 帧——收到则 `await on_armed()`、**不**
    resolve A；收到终帧才 resolve。无 `on_armed` 的普通请求不会收到 armed 帧（服务端
    只在 `arm_ack` 时发），其匹配逻辑不变。
  - `on_armed` 内执行 trigger 子命令（另一 request[B] 走同连接），结果收进 `trigger_result`。
- `GameBridge.wait_signal(...)` 同步包装透传。
- `cmd_wait_signal`：有 `--trigger` 时 `shlex.split` 字符串 → 复用主 parser 解析成
  (RpcSpec, ns) → `on_armed` 内调该 spec 的 handler（同 client）→ 组装信封。

## 错误 / 退出码

- **arm 阶段失败**（节点 `1001` NODE_NOT_FOUND / 信号 `SIGNAL_NOT_FOUND` / timeout
  `-32602`）：armed 帧前发 error → client 不触发 → 报该 error，退出码按现状（RPC 错=1）。
- **trigger 解析 / preflight 失败**（非法子命令、缺参数、嵌套 `wait-*`、非 RPC 的
  `daemon`/`run`/`init`）：**连 daemon 前 preflight** 拒，`-1003` / **exit 64**（契约 #5、
  `-1003` 恒等 64）。
- **trigger RPC 运行期失败**：停止等信号，信封 `{"ok":false,"error":<trigger error>}`，
  退出码取 trigger 子命令的语义（RPC 错=1）。不再等信号。
- **触发成功 + 命中 / 超时**：
  `{"ok":true,"result":{"emitted":...,"args"/"reason":...,"trigger_result":<trigger result>}}`，
  退出码沿用 `_exit_from_wait_signal`（emitted=0 / 否则 1）。

## addon 同步

改动落在 addon（`game_bridge.gd` 的 `wait_signal` kind 改 `async_with_id`、
`wait_api.gd` 发 armed 帧）→ 老项目跑一次 `init` 同步即获此能力（未同步的老 addon
不认 `arm_ack`、不会发 armed 帧——`--trigger` 路径会等不到 armed 帧而超时，属预期的
「需新 RPC 能力」边界，与项目其它需 init 同步的特性一致）。

## 测试策略

- **python 单测**：client 编排（armed 帧 → 触发回调 → 终帧 resolve / trigger 失败短路）、
  cli preflight（非法 trigger、嵌套 wait-*、缺参）、信封与退出码各分支、bridge 委托。
- **GDScript GUT**：`wait_signal` 带 `arm_ack` 发 armed + 终帧两帧、不传 `arm_ack` 仍单帧
  （零回归）、arm 阶段校验失败先发 error 不发 armed。
- **e2e**（本机真跑）：真实 `wait-signal /root/X sig --trigger 'tap …'` 命中信号、
  trigger 失败短路。

## 文档同步

- SKILL.md：把 wait-signal「先后台挂再触发」pitfall 升级成 `--trigger` 一等公民用法，
  保留无 `--trigger` 时的竞态说明；命令表 / 信封示例同步。改模板后重渲染仓内
  `.claude/skills` 版（COLUMNS=80 + Python 3.12）。
- CHANGELOG `[Unreleased]` 记 Added。
