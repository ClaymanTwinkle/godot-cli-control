# #157 item 4 — `emit-signal` 子命令 + `--allow-emit-signal`（PR B）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给被黑名单禁掉的 `emit_signal` 一个 debug+localhost+显式 opt-in 三重门下的专用逃生门：新 `emit-signal` 子命令 + `--allow-emit-signal` flag + 服务端门控（新业务码 1015）。

**Architecture:** 服务端先行（handler + 1015 + 注册 + GUT）→ CLI 子命令（client/bridge/cli + parse 测试）→ opt-in flag（_add_daemon_flags + daemon.start 传递 + pytest）→ 文档/渲染/验证。emit_signal **不**从 `_method_blacklist` 移除（`call` 面不动）；门控服务端判。

**Tech Stack:** Python 3.10+ argparse / async client / GDScript+GUT。规范见 `docs/superpowers/specs/2026-06-10-emit-signal-escape-hatch-design.md`。

**测试执行规则：** pytest / GUT / ruff 一律**委托 subagent（model sonnet）执行、禁 `run_in_background`**，主会话只收结论。venv=`.venv`，godot=`$HOME/.local/bin/godot`，GUT/e2e 本机真跑。

---

## File Structure

- `addons/godot_cli_control/bridge/error_codes.gd` — 加 `EMIT_SIGNAL_DISABLED = 1015`。
- `addons/godot_cli_control/bridge/low_level_api.gd` — `_emit_signal_allowed` 成员 + `_ready` 读 cmdline + `handle_emit_signal`。
- `addons/godot_cli_control/bridge/game_bridge.gd` — `_methods["emit_signal"]` 注册。
- `addons/godot_cli_control/tests/gut/test_low_level_api.gd` — GUT 测试。
- `python/godot_cli_control/client.py` — `emit_signal()` async。
- `python/godot_cli_control/bridge.py` — `emit_signal()` 同步包装。
- `python/godot_cli_control/cli.py` — `cmd_emit_signal` + `_register_emit_signal_args` + RpcSpec；`_add_daemon_flags` 加 `--allow-emit-signal`；`cmd_daemon_start`/`cmd_run` 传 `allow_emit_signal`。
- `python/godot_cli_control/daemon.py` — `start()` 加 `allow_emit_signal` 参数 + 追加 `--game-bridge-allow-emit-signal`。
- `python/tests/test_cli.py` / `python/tests/test_daemon.py` — 测试。
- `python/godot_cli_control/templates/skill/SKILL.md` / `addons/godot_cli_control/README.md` / `addons/godot_cli_control/CHANGELOG.md` / `CLAUDE.md` — 文档。
- `.claude/skills/godot-cli-control/SKILL.md` — Task 4 重渲染。

---

## Task 1: 服务端 handler + 1015 + 注册 + GUT

**Files:**
- Modify: `addons/godot_cli_control/bridge/error_codes.gd`（1014 之后）
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd`（成员 ~49-50；`_ready` 86-88；handler 加在 `handle_call_method` 404 附近）
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`（`_methods` 注册，~220 之后）
- Test: `addons/godot_cli_control/tests/gut/test_low_level_api.gd`

- [ ] **Step 1: 写失败 GUT 测试**

在 `test_low_level_api.gd` 顶部 fixture 区（与 `_CoerceFixture` 等并列）加：
```gdscript
class _EmitSignalFixture extends Node:
	signal pinged(value)
	var received: Array = []
	func _on_pinged(v) -> void:
		received.append(v)
```
末尾加测试：
```gdscript
# ── emit-signal opt-in 逃生门（#157 item4）──────────────────────

func test_emit_signal_disabled_by_default_returns_1015() -> void:
	var node := Node.new()
	node.name = "EmitTarget"
	add_child_autofree(node)
	var result: Dictionary = _api.handle_emit_signal({
		"path": str(node.get_path()), "signal": "ready", "args": [],
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.EMIT_SIGNAL_DISABLED)


func test_emit_signal_allowed_emits_with_args() -> void:
	var node := _EmitSignalFixture.new()
	node.name = "EmitFixture"
	node.pinged.connect(node._on_pinged)
	add_child_autofree(node)
	_api._emit_signal_allowed = true
	var result: Dictionary = _api.handle_emit_signal({
		"path": str(node.get_path()), "signal": "pinged", "args": [42],
	})
	assert_does_not_have(result, "error")
	assert_eq(result.get("emitted"), true)
	assert_eq(node.received, [42])


func test_emit_signal_allowed_unknown_signal_returns_1007() -> void:
	var node := Node.new()
	node.name = "EmitTarget2"
	add_child_autofree(node)
	_api._emit_signal_allowed = true
	var result: Dictionary = _api.handle_emit_signal({
		"path": str(node.get_path()), "signal": "no_such_signal", "args": [],
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.SIGNAL_NOT_FOUND)


func test_call_emit_signal_still_blocked_when_opted_in() -> void:
	# 回归守卫：opt-in 只放开 emit-signal 子命令；通用 call 面 emit_signal 仍黑。
	var node := Node.new()
	node.name = "EmitTarget3"
	add_child_autofree(node)
	_api._emit_signal_allowed = true
	var result: Dictionary = _api.handle_call_method({
		"path": str(node.get_path()), "method": "emit_signal", "args": ["ready"],
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.INVALID_PARAMS)
```

- [ ] **Step 2: 跑确认红**

委托 subagent：`GODOT_BIN=$HOME/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py`
预期：`handle_emit_signal` 不存在 / `EMIT_SIGNAL_DISABLED` 未定义 → 编译或断言失败。

- [ ] **Step 3: 实现**

3a. `error_codes.gd`（`DRAG_IN_PROGRESS: int = 1014` 之后）：
```gdscript
# emit-signal 逃生门未开（issue #157 item4）：daemon 未带 --allow-emit-signal 启动时调
# emit-signal 子命令。前置条件错（与 1009 NOT_PAUSED 同族）——agent 应重启 daemon 加该
# flag（debug-build + localhost 之上第三重显式门）。emit_signal 默认仍在方法黑名单里，
# call <node> emit_signal 始终被拒。
const EMIT_SIGNAL_DISABLED: int = 1015
```

3b. `low_level_api.gd` 成员（`var _method_blacklist: ...` 那行附近，~50）：
```gdscript
# emit-signal opt-in（#157 item4）：daemon 带 --game-bridge-allow-emit-signal 启动时置 true。
var _emit_signal_allowed: bool = false
```

3c. `low_level_api.gd` `_ready()`（在两行 _merge_extra 之后）：
```gdscript
	_emit_signal_allowed = OS.get_cmdline_args().has("--game-bridge-allow-emit-signal")
```

3d. `low_level_api.gd` 加 handler（放在 `handle_call_method` 之后）：
```gdscript
## emit-signal 逃生门（#157 item4）：默认禁（1015），daemon 带 --allow-emit-signal 才放行。
## 门控最先短路（功能没开不解析节点/信号、不泄露存在性）。emit_signal 仍在方法黑名单里，
## 本 handler 是唯一的、被门控的发信号入口；通用 call 面 emit_signal 始终被拒。
func handle_emit_signal(params: Dictionary) -> Dictionary:
	if not _emit_signal_allowed:
		return _err(
			CliControlErrorCodes.EMIT_SIGNAL_DISABLED,
			"emit-signal disabled; restart daemon with --allow-emit-signal (debug-build + localhost gated)"
		)
	var node: Node = _get_node_or_error(params)
	if node == null:
		return _node_not_found(params.get("path", "") as String)
	var signal_name: String = params.get("signal", "") as String
	if signal_name.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'signal' parameter")
	if not node.has_signal(signal_name):
		return _err(CliControlErrorCodes.SIGNAL_NOT_FOUND, "Signal not found: %s" % signal_name)
	var args: Array = params.get("args", []) as Array
	var call_args: Array = [signal_name]
	call_args.append_array(args)
	node.callv("emit_signal", call_args)
	return {"emitted": true}
```

3e. `game_bridge.gd` `_methods` 注册（`_methods["call_method"] = ...` 那行 ~220 之后）：
```gdscript
	# emit-signal 逃生门（#157 item4，sync）：默认禁，daemon --allow-emit-signal 放开。
	_methods["emit_signal"] = {"callable": _low_level_api.handle_emit_signal, "kind": "sync"}
```

- [ ] **Step 4: 跑测试看绿 + 全 GUT 回归**

委托 subagent：`GODOT_BIN=$HOME/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py`
要求：4 条新测试全过；既有黑名单 / call / wait_signal 等 GUT 无回归。

- [ ] **Step 5: 提交**
```bash
git add addons/godot_cli_control/bridge/error_codes.gd addons/godot_cli_control/bridge/low_level_api.gd addons/godot_cli_control/bridge/game_bridge.gd addons/godot_cli_control/tests/gut/test_low_level_api.gd
git commit -m "feat(bridge): emit-signal 逃生门 handler + 1015 门控（#157 item4）"
```

---

## Task 2: CLI `emit-signal` 子命令（client/bridge/cli）

**Files:**
- Modify: `python/godot_cli_control/client.py`（`call_method` ~329 之后）
- Modify: `python/godot_cli_control/bridge.py`（`call_method` ~285 之后）
- Modify: `python/godot_cli_control/cli.py`（`cmd_emit_signal` 加在 `cmd_call` 666 附近；`_register_emit_signal_args` 加在 `_register_call_args` 1178 附近；RpcSpec 加进 `RPC_SPECS`）
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: 写失败测试**
```python
def test_emit_signal_parses_positionals_and_args():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["emit-signal", "/root/X", "item_selected", "0", "ok"])
    assert ns.node_path == "/root/X"
    assert ns.signal == "item_selected"
    assert ns.args == ["0", "ok"]


def test_emit_signal_handler_passes_decoded_args():
    import asyncio
    from godot_cli_control import cli
    captured = {}

    class _FakeClient:
        async def emit_signal(self, path, signal, args=None):
            captured.update(path=path, signal=signal, args=args)
            return {"emitted": True}

    ns = cli.build_parser().parse_args(["emit-signal", "/root/X", "ping", "42", "hi"])
    asyncio.run(cli.cmd_emit_signal(_FakeClient(), ns))
    assert captured == {"path": "/root/X", "signal": "ping", "args": [42, "hi"]}
```

- [ ] **Step 2: 跑确认红**

委托 subagent：`cd python && ../.venv/bin/python -m pytest tests/test_cli.py -k emit_signal -q`（FAIL：emit-signal 子命令 / cmd_emit_signal 未定义）

- [ ] **Step 3: 实现**

3a. `client.py`（`call_method` 之后）：
```python
    async def emit_signal(
        self, path: str, signal: str, args: list | None = None
    ) -> dict:
        return await self.request(
            "emit_signal",
            {"path": path, "signal": signal, "args": args or []},
        )
```

3b. `bridge.py`（`call_method` 之后）：
```python
    def emit_signal(self, path: str, signal: str, args: list | None = None) -> dict:
        """发射节点信号（需 daemon 带 --allow-emit-signal 启动；否则服务端 1015）。"""
        return self._run(self._client.emit_signal(path, signal, args))
```

3c. `cli.py` handler（`cmd_call` 之后）：
```python
async def cmd_emit_signal(client: GameClient, ns: argparse.Namespace) -> Any:
    """发射节点信号（需 daemon --allow-emit-signal；否则服务端返回 1015）。
    每个 arg 同 call：JSON-or-string 解析（--text-value 强制字符串）。"""
    args = _resolve_args_for_call(ns)
    return await client.emit_signal(ns.node_path, ns.signal, args)
```

3d. `cli.py` arg 注册（`_register_call_args` 之后）：
```python
def _register_emit_signal_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("node_path", help="绝对节点路径，如 /root/Main")
    p.add_argument("signal", help="信号名，如 item_selected")
    p.add_argument(
        "args",
        nargs="*",
        help="信号参数；每个先按 JSON 解析失败 fallback 字符串（同 call）",
    )
    p.add_argument(
        "--text-value",
        action="store_true",
        help="把所有 args 当字面字符串，不走 JSON 解析",
    )
```

3e. `cli.py` RpcSpec 加进 `RPC_SPECS`（放在 `name="call"` 那条之后，或 `wait-signal` 附近）：
```python
    RpcSpec(
        name="emit-signal",
        handler=cmd_emit_signal,
        description=(
            "发射节点信号（驱动测试接缝，如 ItemList 选择不发 item_selected）。"
            "默认禁——需 daemon 以 --allow-emit-signal 启动（debug-build + "
            "localhost 之上第三重门），否则服务端返回 1015。注意：call <node> "
            "emit_signal 仍被方法黑名单拒，发信号只能走本子命令。"
        ),
        positionals=(),  # 由 extra_args 注册（args 是 nargs='*'）
        example="emit-signal /root/Main/List item_selected 0",
        extra_args=_register_emit_signal_args,
        text_formatter=lambda r: "emitted" if r.get("emitted") else json.dumps(r, ensure_ascii=False),
    ),
```

- [ ] **Step 4: 跑测试看绿 + 全套 test_cli**

委托 subagent：
1. `cd python && ../.venv/bin/python -m pytest tests/test_cli.py -k emit_signal -q` → PASS
2. **全套** `../.venv/bin/python -m pytest tests/test_cli.py -q` → 全绿（新 RpcSpec 不破坏既有 help/parse）。

- [ ] **Step 5: 提交**
```bash
git add python/godot_cli_control/client.py python/godot_cli_control/bridge.py python/godot_cli_control/cli.py python/tests/test_cli.py
git commit -m "feat(cli): emit-signal 子命令 + client/bridge 方法（#157 item4）"
```

---

## Task 3: opt-in flag `--allow-emit-signal`（daemon start / run 透传）

**Files:**
- Modify: `python/godot_cli_control/cli.py`（`_add_daemon_flags` 2641 `--no-always-on-top` 附近加 flag；`cmd_daemon_start` 1734、`cmd_run` 2101 的 `daemon.start(...)` 调用加 `allow_emit_signal=`）
- Modify: `python/godot_cli_control/daemon.py`（`start()` 签名 159 加参数；args 拼接 251 之后追加）
- Test: `python/tests/test_daemon.py`、`python/tests/test_cli.py`

- [ ] **Step 1: 写失败测试**

`test_cli.py`（parse 层）：
```python
def test_daemon_start_parses_allow_emit_signal():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["daemon", "start", "--allow-emit-signal"])
    assert ns.allow_emit_signal is True


def test_daemon_start_allow_emit_signal_default_false():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["daemon", "start"])
    assert ns.allow_emit_signal is False


def test_run_parses_allow_emit_signal():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["run", "s.py", "--allow-emit-signal"])
    assert ns.allow_emit_signal is True
```

`test_daemon.py`（透传层，**复用既有 `_capture_start_args` helper**——#156 always_on_top 引入的；先读 test_daemon.py 确认其签名/用法，按相同方式调）：
```python
def test_start_appends_allow_emit_signal_flag_when_opted_in(...):
    args = _capture_start_args(..., allow_emit_signal=True)
    assert "--game-bridge-allow-emit-signal" in args


def test_start_no_allow_emit_signal_flag_by_default(...):
    args = _capture_start_args(...)  # 不传 allow_emit_signal
    assert "--game-bridge-allow-emit-signal" not in args
```
（`_capture_start_args` 的确切参数/fixture 以 test_daemon.py 现状为准，镜像 `always_on_top` 的两个测试写法。）

- [ ] **Step 2: 跑确认红**

委托 subagent：`cd python && ../.venv/bin/python -m pytest tests/test_cli.py -k allow_emit_signal tests/test_daemon.py -k allow_emit_signal -q`（FAIL：flag/参数未定义）

- [ ] **Step 3: 实现**

3a. `cli.py` `_add_daemon_flags`（`--no-always-on-top` block 之后、`--fps` 之前）：
```python
    p.add_argument(
        "--allow-emit-signal",
        action="store_true",
        default=False,
        help="放开 emit-signal 子命令（默认禁）。emit_signal 默认在方法黑名单里禁止，"
        "本 flag 是测试态显式 opt-in（debug-build + localhost 之上第三重门）；"
        "call <node> emit_signal 仍被拒，只放开专用 emit-signal 子命令。",
    )
```

3b. `daemon.py` `start()` 签名（`always_on_top: bool = True,` 之后，159 附近）：
```python
        allow_emit_signal: bool = False,
```

3c. `daemon.py` args 拼接（`if time_scale is not None: args.append(f"--cli-time-scale={time_scale}")` 之后、`if record:` 之前——**独立于 record**）：
```python
        if allow_emit_signal:
            args.append("--game-bridge-allow-emit-signal")
```

3d. `cli.py` `cmd_daemon_start` 的 `daemon.start(...)` 调用（1734 `always_on_top=ns.always_on_top,` 附近）加：
```python
            allow_emit_signal=getattr(ns, "allow_emit_signal", False),
```

3e. `cli.py` `cmd_run` 的 `daemon.start(...)` 调用（2101 `always_on_top=ns.always_on_top,` 附近）加：
```python
                    allow_emit_signal=getattr(ns, "allow_emit_signal", False),
```

- [ ] **Step 4: 跑测试看绿 + 全套回归**

委托 subagent：
1. `cd python && ../.venv/bin/python -m pytest tests/test_cli.py -k allow_emit_signal tests/test_daemon.py -k allow_emit_signal -q` → PASS
2. **全套** `../.venv/bin/python -m pytest tests/test_cli.py tests/test_daemon.py -q` → 全绿（注意 Namespace mock 坑：cmd_daemon_start/cmd_run 用 getattr 默认 False 已防御，但仍跑全套确认）。

- [ ] **Step 5: 提交**
```bash
git add python/godot_cli_control/cli.py python/godot_cli_control/daemon.py python/tests/test_cli.py python/tests/test_daemon.py
git commit -m "feat(cli): --allow-emit-signal opt-in flag（daemon start/run 透传，#157 item4）"
```

---

## Task 4: 文档 + 渲染 + 全套验证

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`、`addons/godot_cli_control/README.md`、`addons/godot_cli_control/CHANGELOG.md`、`CLAUDE.md`
- Regenerate: `.claude/skills/godot-cli-control/SKILL.md`

- [ ] **Step 1: SKILL.md 模板**

实现者 Read 后逐字匹配 Edit：
- **命令表**：加一条 `emit-signal <path> <signal> [args...]` —— 「发射节点信号（驱动测试接缝）。默认禁，需 daemon `--allow-emit-signal`（debug+localhost+显式三重门），否则 `1015`。`call <node> emit_signal` 仍被黑名单拒。args 同 `call`（逐个 JSON-or-string）。」放在 `call` 命令条目附近。
- **错误码表**：加 `1015` 行 —— 「emit-signal disabled —— daemon 未带 `--allow-emit-signal` 启动。重启 daemon 加该 flag（debug-build + localhost 之上的显式 opt-in）。emit_signal 默认仍在方法黑名单，`call <node> emit_signal` 始终被拒。」
- **`daemon start` / `run` 选项**：加 `--allow-emit-signal` 说明（三重门 + 只放开 emit-signal 子命令 + call 面仍黑）。
- **common pitfalls / 安全段**：加一句「想发信号驱动 UI（如 ItemList 选择不发 item_selected）：用 `emit-signal` + daemon `--allow-emit-signal`，别试图 `call <node> emit_signal`（黑名单拒）」。

- [ ] **Step 2: addon README**

`addons/godot_cli_control/README.md` 错误码表加 `1015`；命令表加 `emit-signal`（与 SKILL 同义，按 README 既有格式）。

- [ ] **Step 3: CHANGELOG（addon，注意路径）**

`addons/godot_cli_control/CHANGELOG.md` 的 `[Unreleased]` `### Added` 末尾加：
```
- **`emit-signal` 子命令 + `--allow-emit-signal` 逃生门**（#157 item4）：`godot-cli-control emit-signal <path> <signal> [args...]` 发射节点信号——测试里信号常是唯一接缝（如 `ItemList.select()` 不发 `item_selected`）。默认禁（服务端 `1015`），需 `daemon start` / `run` 带 `--allow-emit-signal` 显式 opt-in（debug-build + localhost 之上第三重门）。`emit_signal` 仍在方法黑名单里，`call <node> emit_signal` 始终被拒——只放开这一个目的明确、被门控的入口，不松动整张黑名单。args 同 `call`（逐个 JSON-or-string，`--text-value` 强制字符串）。需新 RPC `emit_signal`——老 addon 项目跑一次 `init` 同步（未同步时报 `-32601`）。配套 `GameClient.emit_signal()` / `GameBridge.emit_signal()`。
```

- [ ] **Step 4: CLAUDE.md 原则 8**

在原则 8（localhost-only / blacklist 安全网）那段末尾加一句：
```
emit_signal 的逃生门走 `daemon start --allow-emit-signal`（debug-build + localhost 之上的显式第三重门）+ 专用 `emit-signal` 子命令（服务端 1015 门控）：这是「单点白名单式放开」，emit_signal 仍在方法黑名单、`call` 面不动，整张黑名单不松。
```

- [ ] **Step 5: 重渲染 SKILL.md（COLUMNS=80 + python3.12）**

委托 subagent：`python3.12 --version` 确认可用（不可用 BLOCKED）→ 照 CI `.github/workflows/ci.yml` 的 skill-render-drift 确切命令重渲染 `.claude/skills/godot-cli-control/SKILL.md` → `git diff -- .claude/skills/godot-cli-control/SKILL.md` 应反映模板改动且与 CI 渲染一致（先 `cd python && ../.venv/bin/python -c "from godot_cli_control import cli; print(cli.format_full_help())"` 确认 help 不崩）。

- [ ] **Step 6: 全套验证（委托 subagent，禁后台）**

1. `cd python && coverage run -m pytest && coverage report`（≥80，报百分比）
2. `python addons/godot_cli_control/tests/run_gut.py`（`GODOT_BIN=$HOME/.local/bin/godot`）全绿
3. `ruff check python/`（没装用 `python3.12 -m ruff`）
4. `cd python && ../.venv/bin/python -m pytest tests/test_skills_install.py -q`
5. `git diff --stat` 仅预期文件

- [ ] **Step 7: 提交**
```bash
git add python/godot_cli_control/templates/skill/SKILL.md addons/godot_cli_control/README.md addons/godot_cli_control/CHANGELOG.md CLAUDE.md .claude/skills/godot-cli-control/SKILL.md
git commit -m "docs(157): emit-signal + --allow-emit-signal 文档同步 + 重渲染（item4）"
```

---

## 收尾分诊（PR 前）

- **#157 全覆盖**：items 1/2/3/5（#167）+ item 4（本 PR）→ PR body 用 `Closes #157`。
- **item 3 非 vector follow-up**（已在 #157 评论跟踪）：#157 关闭时，把该 follow-up 转成独立 issue（标题：sub-path leaf 校验扩展到 Color/Transform 等）或在关闭评论里点明，避免随 #157 关闭丢失。PR 前 `gh issue list --search "sub-path leaf"` 查重后定。
- 越界发现记清单，PR 前统一分诊。
- 串行 base main；`gh pr merge --merge --auto`（required check = `ci-ok`），挂完 `autoMergeRequest` 非 null 自检。

## Self-Review（已过）

- **Spec 覆盖**：emit-signal 子命令（Task 2）+ 服务端门控 1015（Task 1）+ opt-in flag（Task 3）+ 文档（Task 4）。✓
- **占位扫描**：`_capture_start_args`（Task3 Step1）与 CI 渲染命令（Task4 Step5）标注「读现状/CI 配置据实」——是「匹配现有代码」指令非逻辑占位。✓
- **类型一致**：`_emit_signal_allowed`（gd 成员）/ `EMIT_SIGNAL_DISABLED=1015` / `handle_emit_signal` / `cmd_emit_signal` / `emit_signal`（client/bridge）/ `allow_emit_signal`（daemon.start 参数 + ns 字段）/ `--game-bridge-allow-emit-signal`（godot arg）/ `--allow-emit-signal`（CLI flag）—— 命名前后贯通。✓
- **安全**：emit_signal 不从 `_method_blacklist` 移除（Task1 含回归守卫测试）；门控服务端最先短路。✓
