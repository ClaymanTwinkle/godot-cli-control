# #160 — `_send_json` 发送失败 fail-loud(响应超 outbound buffer)设计

> 来源 issue：#160。`game_bridge.gd:_send_json` 调 `send_text` 不查返回值，单条响应超
> `outbound_buffer_size` 时静默丢，client await 挂到 30s 报 `-1002 Timeout`——「误导性错误」家族
> （同 #149），真实根因（响应过大）完全不可见。

## 目标

让「响应超出站 buffer 发不出去」从**静默丢 → 30s 假超时**变成**即时、可定位的小错误信封**：

1. `_send_json` 检查 `send_text` 返回值，失败时 `push_error`/`printerr` 留痕（带 payload 字节数 + buffer 上限 + err）。
2. 若失败的是**响应**（非 error 信封）且带非空 `id`，补发一条小错误信封 `1016 RESPONSE_TOO_LARGE`，message 指路两条解法。失败的大包不占 buffer，小信封可正常送达。
3. 防递归：error 信封自身发送失败时只留痕、不再补发。

## 非目标

- 不做响应分片 / 流式传输（YAGNI；触发面已被 #149 收窄到 bytes-API 巨图）。
- 不自动调大 buffer（用户显式 `outbound_buffer_mb` 才改）。
- 不改 client/CLI 侧——server 端补发的 `1016` 走既有信封链，client 当普通 RPC 错收（exit 1）。

## 背景：当前触发面

`_send_json`（`addons/godot_cli_control/bridge/game_bridge.gd:370`）当前：

```gdscript
func _send_json(data: Dictionary) -> void:
	if _active_peer == null or _active_peer.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	var json_str: String = JSON.stringify(data)
	_active_peer.send_text(json_str)        # ← 返回值（Error）被丢弃
```

- `_active_peer.outbound_buffer_size` 在 `accept_stream` 时设为 `_outbound_buffer_size`
  （`DEFAULT_OUTBOUND_BUFFER_MB=10` × 1MB，`godot_cli_control/outbound_buffer_mb` 可覆盖，下限 `max(1, mb)`MB）。
- 单条消息 > buffer 时 `WebSocketPeer.send_text` 同步返回 `ERR_OUT_OF_MEMORY`（非 `OK`）。
- #149 落地 daemon 直写截图后，CLI `screenshot` 已不触发；**剩余触发面**：`bridge.screenshot()` /
  `client.screenshot()` **不传 path** 的 bytes API 截 >10MB base64 巨图。概率低、命中排查贵。

## 错误码：`1016 RESPONSE_TOO_LARGE`

- issue body 写的 `1014` 已过时：`error_codes.gd` 现有 1001–1015（`1014=DRAG_IN_PROGRESS`、
  `1015=EMIT_SIGNAL_DISABLED`）。下一个空闲码是 **`1016`**，三段制不撞码（CLAUDE.md 原则 2）。
- 语义归类：**资源/容量类**——响应体积超过单帧出站缓冲。永久错（同一响应重试必再超），
  agent 应改用 path 落盘或调大 buffer，盲目重试无意义。
- 同步登记 `error_codes.gd` 常量 + SKILL.md 错误码表 + addon README 错误码表。

## 组件设计

改动集中在 `game_bridge.gd`，拆成「peer 状态门 + 发送 + 失败处理」三段，发送与决策各留一个可测接缝。

### 1. `_send_json`（编排）

```gdscript
func _send_json(data: Dictionary) -> void:
	if not _can_transmit():
		return
	var json_str: String = JSON.stringify(data)
	var err: Error = _transmit(json_str)
	if err == OK:
		return
	# 发送失败——OPEN 态下几乎只可能是单帧超 outbound_buffer。
	var byte_len: int = json_str.to_utf8_buffer().size()
	var detail: String = (
		"[Godot CLI Control] _send_json failed (err=%d): payload %d bytes exceeds outbound buffer %d bytes"
		% [err, byte_len, _outbound_buffer_size]
	)
	push_error(detail)
	printerr(detail)
	var fallback: Dictionary = _oversize_fallback_for(data)
	if fallback.is_empty():
		return
	var fb_err: Error = _transmit(JSON.stringify(fallback))
	if fb_err != OK:
		printerr("[Godot CLI Control] oversize fallback envelope also failed (err=%d); dropping" % fb_err)
```

要点：
- 补发走 `_transmit` **直发**，不回 `_send_json`——无递归路径；fallback 信封 ~百字节，buffer 下限 1MB，必能装下。
- fallback 自身再失败（理论上不可能）只 `printerr` 留痕，不再补发——满足「防递归：error 响应自身失败只留痕」。

### 2. `_can_transmit` / `_transmit`（发送接缝①）

```gdscript
# peer 状态门接缝：默认判活跃 OPEN peer；GUT 子类 override 成 true 以绕过真 socket 依赖。
func _can_transmit() -> bool:
	return _active_peer != null and _active_peer.get_ready_state() == WebSocketPeer.STATE_OPEN

# 出站发送接缝：默认真发；GUT 子类 override 成「返回预置 err + 捕获」以测失败分支。
func _transmit(text: String) -> Error:
	return _active_peer.send_text(text)
```

- `_can_transmit` 是原 peer 门的等价抽取；`_transmit` 默认就是原来那行 `send_text`。
  抽这两个接缝唯一目的：让 GUT 在不起真 socket 的前提下模拟「peer 在线 + 发送失败」。
- `_send_json` 过 `_can_transmit()` 门后才调 `_transmit`，`_transmit` 内不再判空。

### 3. `_oversize_fallback_for`（决策接缝②，纯函数）

```gdscript
# 纯函数：给定发送失败的 data，决定补发什么。返回 {} = 不补发。
# 抽成纯函数以便单测递归守卫 / 空 id / 正常响应三条分支，无需真 peer。
func _oversize_fallback_for(data: Dictionary) -> Dictionary:
	if data.has("error"):
		return {}                      # 失败的本就是 error 信封 → 不补发（防递归根因）
	var id_raw: Variant = data.get("id", "")
	var id: String = id_raw if id_raw is String else ""
	if id.is_empty():
		return {}                      # fire-and-forget：client 不 await，补也没人收
	return {
		"id": id,
		"error": {
			"code": CliControlErrorCodes.RESPONSE_TOO_LARGE,
			"message": (
				"response too large for outbound buffer; "
				+ "for screenshot pass a file path (daemon writes directly), "
				+ "or raise godot_cli_control/outbound_buffer_mb"
			),
		},
	}
```

- 三段判定纯凭 `data` 内容，无副作用、不碰 peer，可直接 `assert_eq` 单测。
- `data.has("error")` 是递归终止根因：`_send_response` 出的帧有 `result`、`_send_error` 出的帧有 `error`；
  只有响应帧（有 `result`）会触发补发，error 帧一律 `{}`。

## 错误处理 / 信封 / 退出码

- 成功：不变。
- 补发的 `1016`：走既有 `{"id": id, "error": {"code": 1016, "message": ...}}` 信封，client 当普通 RPC 错收，
  `_run_rpc` 落 `-1002` 之外的真错码 → **exit 1**（1xxx 语义，CLAUDE.md 原则 3）。无新退出码。
- 留痕（`push_error`/`printerr`）走 stderr 给人看，不污染 stdout 单行 JSON 契约（原则 1）。

## 测试

### GUT（`addons/godot_cli_control/tests/gut/test_game_bridge.gd`）

复用现有夹具风格。新增一个**专测失败分支的子类**（现有 `TestableGameBridge` override 了 `_send_json`，
测不到真实发送；新子类只 override `_transmit`，保留真 `_send_json` 逻辑）：

```gdscript
class FailingTransmitBridge:
	extends GameBridge
	var transmit_calls: Array = []   # [text, ...]
	var fail_first: bool = true      # 第一发失败，后续（fallback）成功
	func _can_transmit() -> bool:
		return true                  # 绕过真 peer 依赖（GUT 无真 socket）
	func _transmit(text: String) -> Error:
		transmit_calls.append(text)
		if fail_first and transmit_calls.size() == 1:
			return ERR_OUT_OF_MEMORY
		return OK
```

测试子类同时 override `_can_transmit`（放行 peer 门）+ `_transmit`（模拟发送失败 + 捕获），
无需构造真 `WebSocketPeer`（GUT headless 下 `accept_stream` 后 peer 停在 `STATE_CONNECTING`，
拿不到 `STATE_OPEN`，故必须走接缝而非真 peer）。

用例：
1. **纯函数 · 递归守卫**：`_oversize_fallback_for({"id":"x","error":{...}})` → `{}`。
2. **纯函数 · 空 id**：`_oversize_fallback_for({"id":"","result":{...}})` → `{}`；`{"result":{...}}`（无 id）→ `{}`。
3. **纯函数 · 正常响应**：`_oversize_fallback_for({"id":"x","result":{...}})` → 带 `error.code==1016`、`id=="x"`。
4. **集成 · 发送失败补发**：`FailingTransmitBridge`，`_can_transmit→true`，发一条带 id 的 response →
   `transmit_calls` 有 2 条：第 1 条原 payload、第 2 条是 1016 fallback（解析回 dict 验 `error.code==1016` + `id` 一致）。
5. **集成 · error 帧失败不补发**：发一条 `{"id":"x","error":{...}}`，第一发失败 → `transmit_calls` **只 1 条**（不补发）。
6. **集成 · 成功不补发**：`fail_first=false`，发 response → `transmit_calls` 只 1 条、无 fallback。

> headless 下 `--dummy` 渲染、不起真 socket，全部用例毫秒级，无 flake 风险。

### pytest

- 无 client/CLI 改动，**不新增 pytest**。`1016` 是纯 server 码，client 通过既有信封链透传，
  现有 `_run_rpc` / `test_cli` 错误码透传测试已覆盖「任意 server 码 → exit 1」路径，无需补。
  （核对：client 侧无硬编码错误码白名单；若有，需补一条 1016 透传断言——plan 阶段确认。）

## 文档同步

- **SKILL.md 模板**（`python/godot_cli_control/templates/skill/SKILL.md`）：错误码表加 `1016 RESPONSE_TOO_LARGE`；
  common pitfalls 可加一句「screenshot 截巨图走 path 落盘，别用 bytes API 灌 stdout，否则可能撞 outbound buffer」。
- **渲染版**（`.claude/skills/godot-cli-control/SKILL.md`）：`COLUMNS=80` + Python 3.12 重渲染（`skills_install.render_skill`），
  对齐 CI `skill-render-drift`（3.12 为准）。
- **addon README**（`addons/godot_cli_control/README.md`）：错误码表加 1016。
- **CHANGELOG**（`addons/godot_cli_control/CHANGELOG.md` 的 `[Unreleased]`，注意是 addon 那个，非仓库根）：
  记 `_send_json` fail-loud + 1016（#160）。

## 跨项约束

- 覆盖率 ≥ 80（`coverage run -m pytest`，**非** `pytest --cov`）；GUT 走 `run_gut.py`（需 `GODOT_BIN`）。
- 测试 subagent（model sonnet）跑、禁 `run_in_background`，主会话只收精简结论。
- 串行 base main，`gh pr merge --merge --auto`（main required check = `ci-ok`）。
- PR body 用 `Closes #160`（不放进反引号/加粗内，否则 GitHub 关键字不触发——见 #157 教训）。
- 收尾分诊：本修复触碰 `_send_json`，无越界发现预期；若发现 buffer 配置/截图链相邻问题，按 CLAUDE.md 分诊门槛处理。

## 实施顺序建议（plan 细化）

1. `error_codes.gd` 加 `RESPONSE_TOO_LARGE = 1016`（+ GUT 断码值，可选）。
2. `_oversize_fallback_for` 纯函数 + 纯函数单测（TDD：先写 1/2/3 用例红 → 实现 → 绿）。
3. `_can_transmit` + `_transmit` 接缝抽取，`_send_json` 改写为编排 + 失败处理 + 集成单测 4/5/6。
4. 文档同步 + 渲染 + 全套验证（GUT + pytest + ruff + skill-render-drift 本地比对）。

每步独立 commit，TDD，frequent commits。
