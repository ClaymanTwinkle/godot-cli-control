# daemon stop 优雅退出 flush AVI 尾帧 —— 设计（#156 子问题 A）

- 日期：2026-06-10
- Issue：[#156](https://github.com/ClaymanTwinkle/godot-cli-control/issues/156)（聚合 issue，本 spec **仅覆盖子问题 A**）
- 范围：`daemon stop` 在 SIGTERM 之前先经 RPC 优雅退出，让 Godot Movie Maker 在正常 quit 路径上 flush AVI 写缓冲，消除尾帧丢失。

## 背景与问题

`daemon.py` 的 `stop()` 走 `_terminate(pid)`：SIGTERM → wait 10s → SIGKILL。SIGTERM 下 Godot Movie Maker 的 AVI 写缓冲**不 flush**，实测丢 4-6 秒尾帧。下游 XGame 录 demo 多次踩中，现行 workaround 是脚本末尾垫 `bridge.wait(4.0)` 牺牲段——把成本全压在用户侧。

Movie Maker 只在引擎**正常退出路径**（`SceneTree.quit()` → `_finalize` → 写入器 flush）才会落盘尾帧。因此根治办法是：stop 时先请 daemon 自己优雅退出，而不是直接发信号。

## 目标 / 非目标

**目标**

- `daemon stop` 默认先尝试 RPC `quit` 优雅退出；成功则 AVI 尾帧完整。
- RPC 不通（daemon 挂死 / 连不上 / 超时）时**无缝降级**到现有 `_terminate()`，stop 永不因优雅退出失败而卡住或报错。
- 不改变 `daemon stop` 的退出码语义与对外契约。

**非目标（明确不在本 spec）**

- 子问题 B（macOS 窗口遮挡冻帧 / 截图 stale 帧、`always_on_top`、`force_draw`、`stale_suspect`）——留作 #156 的后续独立 PR。
- `graceful_timeout` 的可配置化（本期定为常量，未来可加 env / flag）。
- `daemon stop --force`（跳过优雅退出）逃生口——有超时降级兜底，YAGNI，未来按需再加。

## 已定设计决策（已与用户确认）

1. **触发范围：所有 `daemon stop` 都走优雅退出**，不区分录制 / 非录制。语义一致、实现单一分支；让游戏 `_exit_tree` / `WM_CLOSE_REQUEST` 正常跑本身是好行为。代价是每次 stop 多一个亚秒级 RPC 往返，有超时降级兜底。
2. **Python 端发 quit 用「方案 c」**：`GameClient` 新增 `quit()`，把「收到响应」**和**「响应前连接被 daemon 关闭」都视为成功（连接关 = quit 已生效）；超时 / 连不上返回失败。复用现有 ws 栈，容忍 daemon 退出竞态。
3. **`quit` 不暴露为独立 CLI 子命令**，仅作 `stop` 内部 RPC。这是对 CLAUDE.md 契约 4（每个 RPC 都该有 CLI 子命令）的**有意例外**：`quit` 的用户语义 = `stop`，已有 canonical 子命令；再加 `quit` 子命令与 `stop` 重叠、反而误导 AI。在代码注释与本 spec 显式记录该例外理由。

## 架构与数据流

```
daemon stop
  read_pid / 校验(alive / is_godot)        ← 不变
  ┌─────────────────────────────────────────┐
  │ _graceful_quit(pid, port)               │  ← 新增
  │   read port (self.port_file)            │
  │   asyncio.run(client.quit(timeout))     │  → bridge "quit" RPC
  │   poll _reap_if_dead(pid) 直到进程退出     │     (Movie Maker flush AVI)
  │   成功 → True / 超时·连不上·异常 → False   │
  └─────────────────────────────────────────┘
  if not graceful: _terminate(pid)          ← 降级（SIGTERM→10s→SIGKILL，不变）
  _cleanup_state_files()                    ← 不变
  转码 movie_path → mp4                       ← 不变
```

最坏耗时 `graceful_timeout(5s)` + `_terminate(10s)` = 15s（仅当 daemon 半死、优雅退出超时再被 SIGTERM 兜）。常态优雅退出亚秒级完成。

## 组件设计

### 1. bridge 端：`quit` RPC（`game_bridge.gd` + 新退出注入点）

- `_register_methods()` 增：`_methods["quit"] = {"callable": _handle_quit, "kind": "sync"}`。
- `_handle_quit(params: Dictionary) -> Dictionary`：返回 `{"ok": true}` 给 `_dispatch_result` 正常发响应，**随后**触发引擎退出。
- **退出动作可注入**：把「退出」抽成一个成员 `Callable`（默认绑定 `get_tree().quit`），`_handle_quit` 调它而非直接 `get_tree().quit()`。
  - 理由：GUT 测试里 `get_tree().quit()` 会把整个测试进程带走，无法断言。注入点让测试替换成 spy，验证「handler 发了响应且请求了退出」而不真退。
  - 退出用 `call_deferred` 语义（`SceneTree.quit()` 本就延迟到帧末），保证 `_send_response` 的 `send_text` 先排进出站、给 ws 一次 poll 机会再退；即便响应没送达，client 端也按「连接关 = 成功」处理，不依赖响应必达。

### 2. client 端：`GameClient.quit()`（`client.py`）

- 新增 `async def quit(self) -> None`：成功**正常返回**，失败（超时 / 协议错）**抛异常**，由上层 `_graceful_quit` 统一捕获转 False。
- 行为：发送 `{"method": "quit", "id": <id>, "params": {}}`，等待以下任一**成功**信号（正常返回）：
  - 收到对应 `id` 的响应；
  - 连接在收到响应前被关闭（`ConnectionClosed`）——daemon 已退出，`_listen` 的 finally 会清 pending future。
- 不复用严格 await 响应的 `request()`（daemon quit 后大概率收不到响应会误判超时）。

### 3. daemon 端：`_graceful_quit` + `stop()` 改造（`daemon.py`）

- 新增 `_graceful_quit(self, pid: int, graceful_timeout: float = 5.0) -> bool`：
  1. `port = self._read_int(self.port_file)`；读不到 → return False（降级）。
  2. `asyncio.run(_quit_via_rpc(port, ...))`：`GameClient(port=port)` 用**短**连接参数（小 `retries` / 短 `open_timeout` / `total_timeout`），connect → `quit()` → disconnect。任何连接 / 协议异常 → return False。
  3. RPC 发出后，轮询 `_reap_if_dead(pid)`（复用现有，正确回收 zombie 子进程）直到进程退出或 `graceful_timeout`；退出→True，超时→False。
- `stop()`：在现有校验之后、`_terminate` 之前插入
  ```
  if not self._graceful_quit(pid):
      self._terminate(pid)
  ```
  其余（`_cleanup_state_files` / 转码 / 返回码）完全不变。
- **绝不阻断 stop**：`_graceful_quit` 内所有异常就地吞掉转 False，stop 始终能完成。

## 错误处理与退出码

- **不引入新错误码 / 退出码**。优雅退出成功 = 正常 stop（0；转码失败仍为既有的 2）。降级 SIGTERM 也是既有正常路径。
- `quit` RPC 发送失败 / 超时**不报错**，只静默降级——daemon 此时可能已半死，冒出一条 error 反而误导用户。降级路径与今天的 `stop` 行为完全一致。
- port 文件缺失（极老 daemon / 状态被手删）→ 跳过优雅退出直接 `_terminate`，行为兼容。

## 测试策略

- **GDScript GUT**：`quit` method 已注册；`_handle_quit` 返回 `{ok: true}` 且调用了注入的退出 spy（替换默认 `get_tree().quit`，不真退）。
- **pytest**：
  - `_graceful_quit` 三分支：进程及时退出→True；RPC 成功但进程超时不退→False；连不上 / port 缺失→False（mock client / 进程探活）。
  - `stop()`：优雅成功时**不**调 `_terminate`；优雅失败时降级调 `_terminate`；退出码与转码逻辑不受影响。
  - `GameClient.quit()`：收到响应=成功；响应前 `ConnectionClosed`=成功；超时=失败。
- **本机真 e2e**（遵循本仓「e2e 本机必真跑」）：用 `godot_daemon` / `bridge` fixture 录一段短片 → `daemon stop` → 断言产出 mp4 **时长不缺尾段**（与旧 SIGTERM 路径会缺 4-6s 对比）。GODOT_BIN 走本机 `~/.local/bin`。
- 测试执行遵循全局规则：委托 subagent（`model: sonnet`）跑，主会话只收精简结论；覆盖率用 `coverage run -m pytest`，门槛 80%。

## 文档同步

- **CHANGELOG**（`addons/godot_cli_control/CHANGELOG.md` `[Unreleased]`）：记「`daemon stop` 现在先经 RPC 优雅退出，录制（Movie Maker）AVI 尾帧不再丢失；RPC 不通自动降级 SIGTERM」。
- **SKILL.md 模板**（`python/godot_cli_control/templates/skill/SKILL.md`）：录制章节去掉 / 弱化「末尾垫 `wait(4.0)` 牺牲段」的 workaround 说明，改述 stop 已优雅退出。改完跑 `from godot_cli_control import cli; cli.format_full_help()` 不崩 + `pytest python/tests/test_skills_install.py`。
- 仓内 `.claude/skills` 渲染版按需用 `skills_install.render_skill` 重渲染（`COLUMNS=80` + Python 3.12）。

## CLAUDE.md 契约符合性自检

1. **JSON 信封**：`quit` 走既有 `_dispatch_result` / `_send_response`，信封不变。✅
2. **错误码三段制**：不新增码。✅
3. **退出码语义**：不变（0 / 2 / 降级路径一致）。✅
4. **shell canonical surface**：`quit` 不上 CLI——**有意例外**，理由见「已定设计决策 3」，代码注释记录。⚠️（已知并记录）
5. **preflight 优先**：stop 无新增用户参数，无 preflight 影响。✅
6. **大 payload**：`quit` 响应是极小 `{ok}`。✅
7. **SKILL.md 同步**：见「文档同步」。✅
8. **localhost-only / blacklist**：`quit` 复用现有 localhost-only ws；不碰 method/property blacklist；`quit` 仅触发自身进程退出，无 RCE 面。✅

## 风险与未来项

- **风险：游戏自身退出逻辑卡住**（如退出确认对话框）导致优雅退出不在 `graceful_timeout` 内完成 → 由超时降级 SIGTERM 兜住，最坏多等 5s。自动化测试态通常无此类逻辑。
- **未来项**：`graceful_timeout` 可配（env / flag）；`daemon stop --force` 跳过优雅退出；以上 YAGNI，按需再开。
- **后续 PR**：#156 子问题 B（macOS 冻帧 / stale 帧防护）单独 spec + plan + PR，串行 base main。
