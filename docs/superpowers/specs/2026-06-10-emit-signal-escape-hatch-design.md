# #157 item 4 — `emit-signal` 子命令 + `--allow-emit-signal` 逃生门（PR B）设计

> 来源 issue：#157 item 4。本 spec 只覆盖 item 4；items 1/2/3/5 已由 PR A（#167）落地。
> 触安全网契约（CLAUDE.md 原则 8：method/property blacklist 是防 RCE 最后一道），故独立成 PR。

**目标**：给被黑名单一刀切禁掉的 `emit_signal` 一个**显式 opt-in、debug-build + localhost 三重门**下的逃生门，解决「信号是测试唯一接缝」的场景（典型：`ItemList.select()` 不发 `item_selected`，想驱动选择只能 `grab_focus`+`tap ui_down` 绕键盘导航）。

**非目标**：不放开 `call`/`callv`/`set`/`connect`/`queue_free` 等真 RCE 向量；不动 property 黑名单；不提供放开任意黑名单方法的通用开关。

## 安全姿态（为什么这样切是安全的）

- `emit_signal` 是 `_METHOD_BLACKLIST` 14 项里**最不像 RCE** 的：它只触发**已连接的** handler（参数可控），不像 `call`/`callv`（任意 callable 派发）、`connect`（注入回调）、`set`/`set_indexed`（绕 property 黑名单注入 script/Resource）那样等价 RCE 入口。
- 本设计**不放开通用 `call` 面**：`emit_signal` 仍留在 `_method_blacklist`，`call <node> emit_signal …` 依旧被拒（-32602）。只有**专用、被门控的** `emit-signal` 子命令能发信号——攻击面 = 一个目的明确、可审计的入口，而非整张反射表。
- **三重门**：① GameBridge 仅 debug-build 自动激活、release 自动禁用（`OS.is_debug_build()`）；② TCPServer 硬绑 `127.0.0.1`；③ 本设计新增的**显式 opt-in**（daemon 启动时带 flag）。三者全开才放行。
- **门控必须服务端判**：daemon 可能是别人/别处起的，CLI 不知其 cmdline，所以「未 opt-in」的拒绝由**服务端 handler 返回**（新业务码 1015），不是客户端 preflight。

## 组件设计

### 1. 新 RPC 子命令 `emit-signal`（标准五步）

`godot-cli-control emit-signal <path> <signal> [args-json]`
- `<path>`：节点路径（位置参数，必填）。
- `<signal>`：信号名（位置参数，必填）。
- `[args-json]`：可选位置参数，JSON **数组**字面量（如 `'[0]'` / `'["ok", 3]'`），缺省 = 无参。与 `call` 的 args 一致——**直接透传 JSON-decoded 值，不走 codec**（信号参数绝大多数是 int/string/bool/对象引用；复合 Variant 参数是 YAGNI 边界，不支持）。
- argparse 层：`args-json` 非法 JSON / 非数组 → preflight `-1003` / exit 64（连 daemon 前报，沿 `combo --steps-json` 先例）。

落地五步：
1. `client.py`：`async def emit_signal(self, path, signal, args=None)`。
2. `bridge.py`：同步包装 `emit_signal(...)`。
3. `cli.py`：`RpcSpec`（name=`emit-signal`，两位置参 + 可选 args-json，`preflight` 校验 args-json，`text_formatter` 输出如 `emitted`，`exit_code_from` 不设 → 成功恒 0）+ handler。
4. `low_level_api.gd`：`handle_emit_signal(params)`。
5. 文档：SKILL.md（模板）+ addon README 错误码 / 命令表。

### 2. opt-in flag `--allow-emit-signal`

- 加进 `_add_daemon_flags`（`daemon start` + `run` 共用，自动两边都有，对称同 item 5；与 #156 `--no-always-on-top` 同一处）。`action="store_true"`, `default=False`, `dest="allow_emit_signal"`。
- `daemon.start()` 加参数 `allow_emit_signal: bool = False`；为真时 `args.append("--game-bridge-allow-emit-signal")`（紧随 #156 `--game-bridge-always-on-top` 的拼接风格）。
- `cmd_daemon_start` / `cmd_run` 用 `getattr(ns, "allow_emit_signal", False)` 传入（防御写法，规避 test_cli 手构造 Namespace mock 缺字段坑，见 PR A 教训）。
- bridge 侧：`game_bridge.gd` 或 `low_level_api.gd` 在 `_ready()` 读 `OS.get_cmdline_args().has("--game-bridge-allow-emit-signal")`（或复用 `_has_cli_flag`），置实例变量 `_emit_signal_allowed: bool`。`handle_emit_signal` 读它判门。
  - 实现锚点细化（plan 阶段定）：`_method_blacklist` / handler 都在 `low_level_api.gd`，故 `_emit_signal_allowed` 也落在 `low_level_api.gd`，`_ready()` 里自查 cmdline（low_level_api 能直接调 `OS.get_cmdline_args()`）。

### 3. 服务端 `handle_emit_signal(params)` 校验链（顺序固定）

```
1. if not _emit_signal_allowed → 1015 EMIT_SIGNAL_DISABLED
   （message: "emit-signal disabled; restart daemon with --allow-emit-signal
     (debug-build + localhost gated)"）
2. node = _get_node_or_error(params)；node==null → 1001 NODE_NOT_FOUND
3. signal = params["signal"]；空 → -32602 INVALID_PARAMS
4. if not node.has_signal(signal) → 1007 SIGNAL_NOT_FOUND（复用既有码，与
   wait_signal 同族）
5. args = params.get("args", [])（JSON-decoded 数组，直接透传）
6. var call_args := [signal]; call_args.append_array(args)
   node.callv("emit_signal", call_args)
7. return {"emitted": true}
```
- 门控（步 1）**最先短路**：功能没 opt-in 就不做任何节点/信号解析，第一时间让 agent 看到「要加 --allow-emit-signal」，也不泄露 opt-in 关闭时的节点/信号存在性。

> 错误码三段制核对：`1015` 在 `error_codes.gd` 现有 1001-1014 之后，空闲不撞码（CLAUDE.md 原则 2）。新增需同步 `error_codes.gd` + SKILL.md 错误码表 + addon README。`-32602`/`1001`/`1007` 复用既有。

### 4. 退出码 / 信封

- 成功：`{"ok": true, "result": {"emitted": true}}`，exit 0。
- 各错误落标准信封，exit 由 `_rpc_failure_envelope` / RPC 错语义决定（1xxx → exit 1）。
- 无新退出码（不同于 PR A 的 exit 4）。

## 测试

- **GUT**（`test_low_level_api.gd`，镜像 630-658 的黑名单测试）：
  - opt-in 关（`_emit_signal_allowed=false`）→ `handle_emit_signal` 返回 1015。
  - opt-in 开 → 真发信号，连了 handler 的 spy 收到（参数正确）；返回 `{"emitted": true}`。
  - opt-in 开 + 信号不存在 → 1007。
  - opt-in 开 + 节点不存在 → 1001。
  - 构造方式：测试直接 set `_api._emit_signal_allowed = true/false`（GUT 可写实例变量），不依赖真实 cmdline。
  - **回归守卫**：`call <node> emit_signal …` 经 `handle_call_method` **仍返回 -32602**（emit_signal 未从 `_method_blacklist` 移除）——加一条断言钉死。
- **pytest**（`test_cli.py` / `test_daemon.py`）：
  - `build_parser().parse_args(["emit-signal","/root/X","item_selected","[0]"])` 解析 + args-json 非法 → exit 64 preflight。
  - `daemon start --allow-emit-signal` / `run ... --allow-emit-signal` → `daemon.start` 收到 `allow_emit_signal=True` → args 含 `--game-bridge-allow-emit-signal`（mock daemon 捕获 / 捕获 Popen args）。
  - 不带 flag → args **不含** 该 flag（默认安全）。
  - client/bridge `emit_signal` 方法存在且签名正确。
- **e2e（可选，本机真跑）**：起 daemon `--allow-emit-signal`，对一个挂了 handler 的节点 `emit-signal`，断言副作用发生；不带 flag 时 `emit-signal` 返回 1015。

## 文档同步

- **SKILL.md 模板**：新增 `emit-signal` 命令条目（命令表）；错误码表加 `1015`；`daemon start` / `run` 选项加 `--allow-emit-signal`（带安全说明：三重门、emit_signal 对 `call` 仍黑）；common pitfalls 可加「想发信号驱动 UI 走 emit-signal + --allow-emit-signal，不要试图 call emit_signal」。渲染版 `COLUMNS=80 python3.12` 重渲染。
- **addon README**：错误码表加 1015；命令表加 emit-signal。
- **CLAUDE.md 原则 8**：注一句——emit_signal 的 opt-in 逃生门走 `--allow-emit-signal`（debug+localhost+显式三重门），属白名单式放开单点，不松动整张黑名单。
- **CHANGELOG**（`addons/godot_cli_control/CHANGELOG.md` 的 `[Unreleased]`，注意是 addon 那个）：记 emit-signal + --allow-emit-signal（#157）。
- **`init` 同步**：新增 RPC（`emit_signal`）——老 addon 项目需跑一次 `init` 同步；未同步时 `emit-signal` 报 `-32601`（沿 find/drag 等新 RPC 的先例描述）。

## 跨项约束

- 覆盖率 ≥ 80（`coverage run -m pytest`）；测试 subagent（sonnet）跑，禁后台。
- 串行 base main，`gh pr merge --merge --auto`（main required check = `ci-ok`）。
- 合并后 **#157 可关闭**（items 1/2/3/5 由 #167、item 4 由本 PR，全覆盖）——本 PR body 用 `Closes #157`。
- 收尾分诊：item 3 非 vector 类型的 follow-up（已在 #157 评论跟踪）随 #157 关闭转为独立 issue 或留评论，PR 前定。

## 实施顺序建议（plan 细化）

服务端先行（handle_emit_signal + 1015 + GUT）→ CLI/client/bridge（emit-signal 子命令 + parse 测试）→ opt-in flag（_add_daemon_flags + daemon.start + 传递，pytest）→ 文档 + 渲染 + 全套验证。每项独立 commit，TDD。
