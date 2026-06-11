# `_send_json` 发送失败 fail-loud（#160）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `game_bridge.gd:_send_json` 在响应超出站 WebSocket 缓冲发不出去时 fail-loud（stderr 留痕 + 补发小 `1016 RESPONSE_TOO_LARGE` 信封），替代当前「静默丢 → client 干等 30s 假 `-1002` 超时」。

**Architecture:** 改动集中在 `addons/godot_cli_control/bridge/game_bridge.gd`，把 `_send_json` 拆成「peer 状态门（`_can_transmit`）+ 发送（`_transmit`）+ 失败决策（`_oversize_fallback_for` 纯函数）+ 编排」四块。`_can_transmit`/`_transmit` 是 GUT 测试接缝（子类 override 绕真 socket、模拟发送失败）；`_oversize_fallback_for` 是纯函数（无副作用、可直接断言）。新增服务端错误码 `1016`。无 client/CLI 改动——`1016` 走既有信封链，`_run_rpc` 映射任意 RPC 失败为 exit 1。

**Tech Stack:** Godot 4 GDScript（addon + GUT 单测）；Python CLI 侧只动文档模板与渲染。

**关键背景（实现者必读）:**
- `_active_peer.outbound_buffer_size` 在 `accept_stream` 时设为 `_outbound_buffer_size`（默认 `10 * 1024 * 1024`，`godot_cli_control/outbound_buffer_mb` 可覆盖，下限 1MB）。单条消息超 buffer 时 `WebSocketPeer.send_text` 同步返回非 `OK`（`ERR_OUT_OF_MEMORY`）。
- issue body 写的码 `1014` **已过时**：`error_codes.gd` 现有 1001–1015（`1014=DRAG_IN_PROGRESS`、`1015=EMIT_SIGNAL_DISABLED`）。本计划用下一个空闲码 **`1016`**。
- GUT 不因 `push_error` 判测试失败（`test_diagnostics_api.gd` 故意调 `push_error` 并通过），只在输出留 ERROR 噪音。失败分支的集成测试照常触发即可。
- 现有 `test_game_bridge.gd` 的 `TestableGameBridge` 把 `_send_json` 整个 override 成捕获——它仍可用于**纯函数**测试（`_oversize_fallback_for` 是继承来的、不受 `_send_json` override 影响）；**集成**测试用新子类 `FailingTransmitBridge`（只 override `_can_transmit`/`_transmit`，保留真 `_send_json`）。
- 测试执行遵循全局规则：用 subagent（model sonnet）跑 GUT（`python addons/godot_cli_control/tests/run_gut.py`，需 `GODOT_BIN`），**禁后台**，主会话只收精简结论。

---

### Task 1: 新增错误码 `1016 RESPONSE_TOO_LARGE`

**Files:**
- Modify: `addons/godot_cli_control/bridge/error_codes.gd`（在 `EMIT_SIGNAL_DISABLED: int = 1015` 后追加）
- Test: `addons/godot_cli_control/tests/gut/test_game_bridge.gd`（追加一条常量断言）

- [ ] **Step 1: 写失败测试**

在 `test_game_bridge.gd` 末尾追加（先红——常量尚不存在）：

```gdscript
# ── #160: _send_json 发送失败 fail-loud ──────────────────────────────

func test_response_too_large_code_is_1016() -> void:
	# 防回归：码值锁死 1016，三段制内不撞 1001-1015
	assert_eq(CliControlErrorCodes.RESPONSE_TOO_LARGE, 1016)
```

- [ ] **Step 2: 跑测试确认失败**

委托 subagent（sonnet）跑：`GODOT_BIN=<godot> python addons/godot_cli_control/tests/run_gut.py`
Expected: 编译/运行错——`RESPONSE_TOO_LARGE` 不是 `CliControlErrorCodes` 的成员。

- [ ] **Step 3: 加常量**

在 `error_codes.gd` 的 `const EMIT_SIGNAL_DISABLED: int = 1015` 这一段之后、`const INVALID_PARAMS: int = -32602` 之前，插入：

```gdscript
# 响应超出站 WebSocket 缓冲（issue #160）：单条响应 JSON 超过 outbound_buffer_size
# （默认 10MB，godot_cli_control/outbound_buffer_mb 可调）时 send_text 失败。
# 容量/资源类永久错——同一响应重试必再超；agent 应改用 path 落盘（screenshot）
# 或调大 buffer。daemon 用它替换发不出去的大响应，避免 client 干等到 -1002 假超时。
const RESPONSE_TOO_LARGE: int = 1016
```

- [ ] **Step 4: 跑测试确认通过**

委托 subagent（sonnet）跑同上命令。
Expected: `test_response_too_large_code_is_1016` PASS。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/bridge/error_codes.gd addons/godot_cli_control/tests/gut/test_game_bridge.gd
git commit -m "feat(160): 新增错误码 1016 RESPONSE_TOO_LARGE

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `_oversize_fallback_for` 纯函数 + 纯函数单测

**Files:**
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`（在 `_send_json` 附近新增纯函数）
- Test: `addons/godot_cli_control/tests/gut/test_game_bridge.gd`

- [ ] **Step 1: 写失败测试**

在 Task 1 追加的注释段下方继续追加。`_bridge` 是 `before_each` 建好的 `TestableGameBridge`（继承 `_oversize_fallback_for`，纯函数不受其 `_send_json` override 影响）：

```gdscript
func test_oversize_fallback_skips_error_envelope() -> void:
	# 递归守卫：失败的本就是 error 信封 → 不补发
	var fb: Dictionary = _bridge._oversize_fallback_for(
		{"id": "x", "error": {"code": 1001, "message": "n"}}
	)
	assert_true(fb.is_empty(), "error 信封发送失败不应补发（防递归）")


func test_oversize_fallback_skips_empty_or_missing_id() -> void:
	# fire-and-forget：client 不 await，补也没人收
	var fb_empty: Dictionary = _bridge._oversize_fallback_for({"id": "", "result": {"big": "x"}})
	assert_true(fb_empty.is_empty(), "空 id 响应不应补发")
	var fb_missing: Dictionary = _bridge._oversize_fallback_for({"result": {"big": "x"}})
	assert_true(fb_missing.is_empty(), "无 id 响应不应补发")


func test_oversize_fallback_builds_1016_for_response() -> void:
	var fb: Dictionary = _bridge._oversize_fallback_for({"id": "abc", "result": {"big": "x"}})
	assert_false(fb.is_empty(), "带 id 的正常响应失败应补发")
	assert_eq(str(fb.get("id")), "abc", "补发信封须沿用原 id")
	assert_has(fb, "error")
	assert_eq(int(fb.error.code), CliControlErrorCodes.RESPONSE_TOO_LARGE, "补发码须为 1016")
	assert_string_contains(str(fb.error.message), "outbound buffer")
```

- [ ] **Step 2: 跑测试确认失败**

委托 subagent（sonnet）跑 GUT。
Expected: 三条新测试 FAIL/报错——`_oversize_fallback_for` 不存在。

- [ ] **Step 3: 加纯函数**

在 `game_bridge.gd` 的 `_send_json`（当前 `func _send_json(data: Dictionary) -> void:`）**之前**插入：

```gdscript
# 纯函数（issue #160）：给定一条发送失败的出站帧 data，决定补发什么。
# 返回 {} = 不补发。抽成纯函数以便单测三条分支（递归守卫 / 空 id / 正常响应），
# 无副作用、不碰 peer。
func _oversize_fallback_for(data: Dictionary) -> Dictionary:
	if data.has("error"):
		# 失败的本就是 error 信封 → 不补发（递归终止根因：补发的也是 error 信封，
		# 若它再失败会带 error，再次落到这里返回 {}）。
		return {}
	var id_raw: Variant = data.get("id", "")
	if not (id_raw is String):
		return {}
	var id: String = id_raw
	if id.is_empty():
		# fire-and-forget：client 不 await，补发没人收。
		return {}
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

- [ ] **Step 4: 跑测试确认通过**

委托 subagent（sonnet）跑 GUT。
Expected: 三条纯函数测试全 PASS。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/bridge/game_bridge.gd addons/godot_cli_control/tests/gut/test_game_bridge.gd
git commit -m "feat(160): _oversize_fallback_for 纯函数（补发决策 + 递归守卫）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `_can_transmit` / `_transmit` 接缝 + `_send_json` 改写 + 集成测试

**Files:**
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`（改写 `_send_json`，新增 `_can_transmit` / `_transmit`）
- Test: `addons/godot_cli_control/tests/gut/test_game_bridge.gd`（新增 `FailingTransmitBridge` 子类 + 3 条集成测试）

- [ ] **Step 1: 写失败测试**

先在 `test_game_bridge.gd` 顶部的内部类区（紧跟现有 `class TestableGameBridge` 之后）新增子类：

```gdscript
# ── 子类：模拟「peer 在线 + 发送失败」以测 _send_json 失败分支（issue #160）──
# 只 override 两个接缝，保留真 _send_json 编排逻辑（区别于 TestableGameBridge）。

class FailingTransmitBridge:
	extends GameBridge
	var transmit_calls: Array = []   # 每次 _transmit 的入参文本
	var fail_first: bool = true      # true: 第一发返回 ERR_OUT_OF_MEMORY，后续（补发）成功

	func _can_transmit() -> bool:
		return true                  # 绕过真 peer：GUT 无真 socket

	func _transmit(text: String) -> Error:
		transmit_calls.append(text)
		if fail_first and transmit_calls.size() == 1:
			return ERR_OUT_OF_MEMORY
		return OK
```

再在文件末尾的 `# ── #160 ...` 测试区追加三条集成测试：

```gdscript
func test_send_failure_emits_1016_fallback() -> void:
	# 集成：带 id 的大响应首发失败 → 补发 1016 信封（共 2 次 transmit）。
	# 注：本测试会触发真实失败路径的 push_error（GUT 输出留 ERROR 噪音，不判失败）。
	var fb := FailingTransmitBridge.new()
	autofree(fb)
	fb._send_json({"id": "big1", "result": {"payload": "x"}})
	assert_eq(fb.transmit_calls.size(), 2, "首发失败应触发补发（共 2 次 transmit）")
	var parsed: Variant = JSON.parse_string(fb.transmit_calls[1])
	assert_true(parsed is Dictionary, "补发帧应是合法 JSON 对象")
	var frame: Dictionary = parsed
	assert_eq(str(frame.get("id")), "big1", "补发信封须沿用原 id")
	assert_eq(int((frame["error"] as Dictionary)["code"]), 1016)


func test_send_failure_on_error_frame_does_not_refeed() -> void:
	# 集成：失败的是 error 信封 → 不补发（只 1 次 transmit），杜绝递归。
	var fb := FailingTransmitBridge.new()
	autofree(fb)
	fb._send_json({"id": "x", "error": {"code": 1001, "message": "n"}})
	assert_eq(fb.transmit_calls.size(), 1, "error 信封失败不补发")


func test_send_success_does_not_fallback() -> void:
	# 集成：发送成功不补发（只 1 次 transmit）。
	var fb := FailingTransmitBridge.new()
	autofree(fb)
	fb.fail_first = false
	fb._send_json({"id": "ok1", "result": {"a": 1}})
	assert_eq(fb.transmit_calls.size(), 1, "发送成功不应补发")
```

- [ ] **Step 2: 跑测试确认失败**

委托 subagent（sonnet）跑 GUT。
Expected: `test_send_failure_emits_1016_fallback` FAIL（当前 `_send_json` 不查返回值、不补发，`transmit_calls` 只 1 条）。另两条可能因当前 `_send_json` 仍调 `_active_peer.send_text`（而非 `_transmit`）而报错或不捕获——都属预期红。

- [ ] **Step 3: 改写 `_send_json` + 加接缝**

把 `game_bridge.gd` 当前的 `_send_json`：

```gdscript
func _send_json(data: Dictionary) -> void:
	if _active_peer == null or _active_peer.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	var json_str: String = JSON.stringify(data)
	_active_peer.send_text(json_str)
```

整段替换为（`_oversize_fallback_for` 已在 Task 2 加好，位于本段之前）：

```gdscript
# peer 状态门接缝（issue #160）：默认判活跃 OPEN peer；GUT 子类 override 成 true 绕真 socket。
func _can_transmit() -> bool:
	return _active_peer != null and _active_peer.get_ready_state() == WebSocketPeer.STATE_OPEN


# 出站发送接缝（issue #160）：默认真发；GUT 子类 override 成「返回预置 err + 捕获」测失败分支。
func _transmit(text: String) -> Error:
	return _active_peer.send_text(text)


func _send_json(data: Dictionary) -> void:
	if not _can_transmit():
		return
	var json_str: String = JSON.stringify(data)
	var err: Error = _transmit(json_str)
	if err == OK:
		return
	# 发送失败——OPEN 态下几乎只可能是单帧超 outbound_buffer。stderr 留痕（不污染
	# stdout 单行 JSON 契约），并对带 id 的响应补发小错误信封，避免 client 干等到 -1002。
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
	# 补发走 _transmit 直发（不回 _send_json）→ 无递归路径；信封 ~百字节，buffer 下限 1MB 必能装下。
	var fb_err: Error = _transmit(JSON.stringify(fallback))
	if fb_err != OK:
		printerr(
			"[Godot CLI Control] oversize fallback envelope also failed (err=%d); dropping" % fb_err
		)
```

- [ ] **Step 4: 跑测试确认通过**

委托 subagent（sonnet）跑 GUT 全量。
Expected: Task 1/2/3 全部新测试 PASS；现有 `test_game_bridge.gd` 其余测试不回归（`TestableGameBridge` 仍 override `_send_json`，不受影响）。会看到两条失败路径测试触发的 `_send_json failed` ERROR 噪音——正常。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/bridge/game_bridge.gd addons/godot_cli_control/tests/gut/test_game_bridge.gd
git commit -m "fix(160): _send_json 检查发送结果，失败 fail-loud + 补发 1016 信封

send_text 返回值此前被丢弃，单条响应超 outbound buffer 时静默丢，
client await 挂到 30s 假报 -1002。现检查结果、stderr 留痕，并对带 id
的响应补发小 1016 RESPONSE_TOO_LARGE 信封指路。

Closes #160

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 文档同步（SKILL 模板 + 渲染 + addon README + CHANGELOG）

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（错误码表，1015 行后）
- Modify: `.claude/skills/godot-cli-control/SKILL.md`（重渲染产物，**不手改**）
- Modify: `addons/godot_cli_control/README.md`（错误码表，1015 行后）
- Modify: `addons/godot_cli_control/CHANGELOG.md`（`[Unreleased]` 的 `### Fixed`）

- [ ] **Step 1: SKILL 模板加错误码行**

在 `python/godot_cli_control/templates/skill/SKILL.md` 的 `1015` 错误码表行（`| `1015` | EMIT_SIGNAL_DISABLED: ...`）**之后**插入一行：

```
| `1016` | RESPONSE_TOO_LARGE: a single response exceeded the daemon's outbound WebSocket buffer (default 10 MB, set via `godot_cli_control/outbound_buffer_mb`) and couldn't be sent, so the daemon replaced it with this small error instead of letting the call hang to a `-1002` timeout. Almost always a `screenshot` taken via the **bytes API** (no path) on a hiDPI/4K frame — pass a file path so the daemon writes the PNG to disk directly, or raise the buffer. Permanent for that response — retrying re-overflows. |
```

- [ ] **Step 2: addon README 加错误码行**

在 `addons/godot_cli_control/README.md` 的 `1015` 错误码表行（`| `1015` | server | EMIT_SIGNAL_DISABLED: ...`）**之后**插入：

```
| `1016` | server | RESPONSE_TOO_LARGE: a single response exceeded the outbound WebSocket buffer (default 10 MB, `godot_cli_control/outbound_buffer_mb`) and was replaced with this error instead of hanging the call to a `-1002` timeout. Usually a bytes-API `screenshot` of a hiDPI frame — pass a path (daemon writes to disk) or raise the buffer. |
```

- [ ] **Step 3: CHANGELOG 加 Fixed 条目**

在 `addons/godot_cli_control/CHANGELOG.md` 的 `## [Unreleased]` → `### Fixed` 段，已有的 `- **#149 ...` 条目**之前**（即作为 Fixed 段第一条）插入：

```markdown
- **#160 `_send_json` 发送失败不再静默 → 假 `-1002` 超时**：单条响应超出站 WebSocket 缓冲（默认 10MB，`godot_cli_control/outbound_buffer_mb` 可调）时，`send_text` 返回值此前被丢弃、daemon 静默不响应，client await 挂到 30s 报 `-1002 Timeout`（真根因「响应过大」完全不可见，同 #149 的「误导性错误」家族）。现 `_send_json` 检查发送结果：失败时 stderr 留痕（payload 字节数 + buffer 上限），并对带 id 的响应补发一条小 `1016 RESPONSE_TOO_LARGE` 错误信封指路（screenshot 传 path 走 daemon 直写 / 调大 outbound_buffer_mb）；error 信封自身或无 id 的帧不补发（防递归）。触发面已被 #149 收窄到不传 path 的 bytes-API 巨图截图。新增服务端码 `1016`。改动在 addon（game_bridge.gd），老项目跑一次 `init` 同步即获此修复。
```

- [ ] **Step 4: 重渲染仓内 SKILL.md（必须 COLUMNS=80 + Python 3.12）**

在仓库根跑（与 CI `skill-render-drift` 同源命令）：

```bash
COLUMNS=80 python3.12 -c "from godot_cli_control import cli; from godot_cli_control.skills_install import render_skill; from godot_cli_control._version import version; open('.claude/skills/godot-cli-control/SKILL.md','w').write(render_skill(version, cli.format_full_help()))"
```

> 若本机无 `python3.12`，必须装一个或在 3.12 容器内跑——argparse usage 折行随 Python 小版本变，CI 以 3.12 为准；用别的版本渲染会引入 drift。

- [ ] **Step 5: 验证渲染版含 1016 且无意外 drift**

```bash
git diff --stat .claude/skills/godot-cli-control/SKILL.md
grep -n "1016" .claude/skills/godot-cli-control/SKILL.md
```
Expected: 渲染版出现 `1016` 行；diff 仅限错误码表新增行（若有大面积重排，说明渲染环境 Python 版本/COLUMNS 不对，回 Step 4 修正）。

- [ ] **Step 6: 验证 skill 模板渲染不崩**

```bash
python -c "from godot_cli_control import cli; print(cli.format_full_help())" >/dev/null && echo OK
```
Expected: `OK`（CLI 帮助渲染无异常）。

- [ ] **Step 7: 提交**

```bash
git add python/godot_cli_control/templates/skill/SKILL.md .claude/skills/godot-cli-control/SKILL.md addons/godot_cli_control/README.md addons/godot_cli_control/CHANGELOG.md
git commit -m "docs(160): 错误码表 + CHANGELOG 记 1016 RESPONSE_TOO_LARGE

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 最终验证（所有 task 完成后）

- [ ] **GUT 全量**（subagent / sonnet，禁后台）：`GODOT_BIN=<godot> python addons/godot_cli_control/tests/run_gut.py` —— 全绿，无回归。
- [ ] **pytest 全量 + 覆盖率**（subagent / sonnet，禁后台）：`coverage run -m pytest && coverage report`（**非** `pytest --cov`）—— 全绿、覆盖率 ≥ 80（本计划无 Python 逻辑改动，预期不掉）。
- [ ] **ruff**（开 PR 前）：`ruff check python/` —— 干净（本机 .venv 可能没装 ruff，缺则临时 `pip install ruff` 或在 subagent 内跑）。
- [ ] **skill-render-drift 本地比对**：确认 Step 4 渲染产物已提交、`git status` 干净。

完成后用 superpowers:finishing-a-development-branch 收尾（选项 2：push + 开 PR 挂 `--auto`，PR body 用 `Closes #160` 且**不放进反引号/加粗内**——否则 GitHub 关键字不触发，见 #157 教训）。
