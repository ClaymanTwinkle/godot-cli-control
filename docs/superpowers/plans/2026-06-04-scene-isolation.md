# 场景隔离（#98）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `scene_reload` / `scene_change` RPC（等新场景 ready 才返回）+ CLI `scene-reload` / `scene-change` + pytest `fresh_scene` fixture，给下游 e2e 提供真正的 per-test 场景隔离。

**Architecture:** GDScript 侧新建 `scene_api.gd`（对齐 #108 wait_api 拆分先例），逐帧轮询等新场景实例 ready；Python 侧走标准三层（client async → bridge sync → cli RpcSpec）；pytest plugin 加 function-scope `fresh_scene` fixture。新业务错误码 `SCENE_UNAVAILABLE = 1008`。

**Tech Stack:** Godot 4 GDScript + GUT 9、Python ≥3.10、websockets、pytest / pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-06-04-scene-isolation-design.md`（已批准，含全部设计决策）

**分支：** `feat/98-scene-isolation`（已建，基于 main）

**仓库铁律（执行者必读）：**
- 测试执行一律委托 subagent（`model: "sonnet"`），主会话只收精简结论（仓库 CLAUDE.md 全局规则）。subagent-driven 模式下 implementer 自己跑测试即满足。
- Python 覆盖率跑法：`coverage run -m pytest`，**不能** `pytest --cov`（pyproject.toml 有注释说明）。
- GUT 跑法：`GODOT_BIN=$HOME/.local/bin/godot ./addons/godot_cli_control/tests/run_gut.sh`（本机 godot 在 `~/.local/bin/godot`，必须真跑，不允许以缺 GODOT_BIN 为由跳过）。
- e2e（test_e2e_*.py）需要真实 Godot，本机必真跑。
- JSON 信封 / 错误码三段制 / 退出码语义等契约见仓库根 CLAUDE.md。

---

## File Structure

| 动作 | 文件 | 职责 |
|---|---|---|
| Create | `addons/godot_cli_control/bridge/scene_api.gd` | SceneApi 节点：scene_reload_async / scene_change_async |
| Create | `addons/godot_cli_control/tests/gut/test_scene_api.gd` | GUT 单测（错误分支） |
| Modify | `addons/godot_cli_control/bridge/error_codes.gd` | 加 `SCENE_UNAVAILABLE = 1008` |
| Modify | `addons/godot_cli_control/bridge/game_bridge.gd` | 实例化 SceneApi + 注册 2 个 async RPC |
| Modify | `addons/godot_cli_control/tests/gut/test_game_bridge.gd` | 注册表断言补 2 个方法 |
| Modify | `python/godot_cli_control/client.py` | `scene_reload` / `scene_change` async 方法 |
| Modify | `python/godot_cli_control/bridge.py` | 同步包装 ×2 |
| Modify | `python/godot_cli_control/cli.py` | 2 个 RpcSpec + handlers + preflight + text_formatter |
| Modify | `python/godot_cli_control/pytest_plugin.py` | `fresh_scene` fixture |
| Create | `examples/platformer-demo/second.tscn` | e2e 用第二场景 |
| Create | `python/tests/test_e2e_scene.py` | e2e：真 reload / change / 1008 / bridge 直连 |
| Modify | `python/tests/test_client.py`、`test_bridge.py`、`test_cli.py`、`test_pytest_plugin.py` | 三层单测 + fixture 单测 |
| Modify | `python/godot_cli_control/templates/skill/SKILL.md` | Scene 命令组 + 1008 + fresh_scene + pitfalls ×2 |
| Modify | `addons/godot_cli_control/README.md` | 命令表 / 错误码表同步 |

---

### Task 1: GDScript SceneApi（GUT 错误分支 TDD）

**Files:**
- Create: `addons/godot_cli_control/tests/gut/test_scene_api.gd`
- Create: `addons/godot_cli_control/bridge/scene_api.gd`
- Modify: `addons/godot_cli_control/bridge/error_codes.gd`

**背景知识：**
- GUT 测试模式参考 `tests/gut/test_low_level_api.gd`（`extends GutTest`、`before_each` 里 `XxxScript.new()` + `add_child_autofree`）。
- GUT cmdln（script mode）下 `get_tree().current_scene == null`，所以「无 current scene → 1008」「路径不存在 → 1008」「参数校验 → -32602」三类错误分支都可以单测；**真 reload/change 留给 e2e**（Task 6）。
- async handler 在 GUT 里直接 `await _api.scene_reload_async({...})`。
- 错误返回 helper 形如 `{"error": {"code": ..., "message": ...}}`，对齐 `wait_api.gd:258-259` 的 `_err`。

- [ ] **Step 1: 写失败测试**

```gdscript
## GUT 单元测试：SceneApi 错误分支（issue #98）
##
## GUT cmdln script-mode 下 current_scene 恒为 null，真 reload/change
## 由 python/tests/test_e2e_scene.py 兜底；这里测错误分支与参数校验。
extends GutTest

const SceneApiScript := preload("res://addons/godot_cli_control/bridge/scene_api.gd")

var _api: Node


func before_each() -> void:
	_api = SceneApiScript.new()
	_api.name = "SceneApi"
	add_child_autofree(_api)


func test_scene_reload_without_current_scene_returns_1008() -> void:
	var result: Dictionary = await _api.scene_reload_async({})
	assert_has(result, "error", "script-mode 无 current_scene，reload 应报错")
	assert_eq(int(result.error.code), 1008)
	assert_string_contains(str(result.error.message), "no current scene")


func test_scene_reload_invalid_timeout_returns_minus_32602() -> void:
	var result: Dictionary = await _api.scene_reload_async({"timeout": "abc"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_scene_change_missing_path_returns_minus_32602() -> void:
	var result: Dictionary = await _api.scene_change_async({})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_scene_change_nonexistent_scene_returns_1008() -> void:
	var result: Dictionary = await _api.scene_change_async({
		"path": "res://__definitely_missing__.tscn",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1008)
	assert_string_contains(str(result.error.message), "scene not found")


func test_scene_change_timeout_out_of_range_returns_minus_32602() -> void:
	var result: Dictionary = await _api.scene_change_async({
		"path": "res://anything.tscn", "timeout": -1.0,
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
```

- [ ] **Step 2: 跑 GUT 确认失败**

Run: `GODOT_BIN=$HOME/.local/bin/godot ./addons/godot_cli_control/tests/run_gut.sh`
Expected: test_scene_api.gd 因 `scene_api.gd` 不存在而 preload 失败（脚本加载错误）。

- [ ] **Step 3: error_codes.gd 加 1008**

在 `SIGNAL_NOT_FOUND` 之后追加：

```gdscript
# 场景不可用（issue #98 scene_reload/scene_change）：reload 时无 current
# scene、change 的 res 路径不存在/加载失败、等新场景 ready 超时。
# 与 1006 拆开：1006 是「短重试可能成功」的 transient；1008 三种情形里
# 路径不存在是永久错，超时大概率是场景加载本身坏了——agent 应停下排查。
const SCENE_UNAVAILABLE: int = 1008
```

- [ ] **Step 4: 写 scene_api.gd 最小实现**

```gdscript
class_name SceneApi
extends Node
## 场景生命周期 API：scene_reload / scene_change（issue #98）
##
## 两者都等新场景 ready 才返回（逐帧轮询，模式对齐 wait_api.gd），
## 调用方不需要再 wait-node。错误码常量来自 error_codes.gd。

# 等新场景 ready 的超时上限 / 默认值（秒）
const _MAX_SCENE_TIMEOUT: float = 3600.0
const _DEFAULT_TIMEOUT: float = 10.0


func scene_reload_async(params: Dictionary) -> Dictionary:
	var timeout_or_err: Variant = _parse_timeout(params)
	if timeout_or_err is Dictionary:
		return timeout_or_err as Dictionary
	var timeout: float = timeout_or_err as float
	var old: Node = get_tree().current_scene
	if old == null:
		return _err(CliControlErrorCodes.SCENE_UNAVAILABLE, "no current scene to reload")
	var err: int = get_tree().reload_current_scene()
	if err != OK:
		return _err(
			CliControlErrorCodes.SCENE_UNAVAILABLE,
			"reload_current_scene failed: %s" % error_string(err),
		)
	return await _await_new_scene_ready(old, timeout)


func scene_change_async(params: Dictionary) -> Dictionary:
	var path_raw: Variant = params.get("path", null)
	if not (path_raw is String) or (path_raw as String).is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'path' parameter")
	var path: String = path_raw as String
	var timeout_or_err: Variant = _parse_timeout(params)
	if timeout_or_err is Dictionary:
		return timeout_or_err as Dictionary
	var timeout: float = timeout_or_err as float
	if not ResourceLoader.exists(path, "PackedScene"):
		return _err(CliControlErrorCodes.SCENE_UNAVAILABLE, "scene not found: %s" % path)
	# old 在 change 之前捕获，可为 null（script-mode / 启动早期）——
	# 此时 current_scene != null 本身即构成新实例判定，不会死循环。
	var old: Node = get_tree().current_scene
	var err: int = get_tree().change_scene_to_file(path)
	if err != OK:
		return _err(
			CliControlErrorCodes.SCENE_UNAVAILABLE,
			"change_scene_to_file failed: %s" % error_string(err),
		)
	return await _await_new_scene_ready(old, timeout)


## 逐帧等到 current_scene 是「不同于 old 的新实例」且 ready，返回新场景信息。
## reload 后 scene_file_path 不变，只能比实例 id。
func _await_new_scene_ready(old: Node, timeout: float) -> Dictionary:
	var old_id: int = old.get_instance_id() if old != null else 0
	var start_ms: int = Time.get_ticks_msec()
	while true:
		var cur: Node = get_tree().current_scene
		if cur != null and cur.get_instance_id() != old_id and cur.is_node_ready():
			return {"scene_path": cur.scene_file_path, "name": String(cur.name)}
		if float(Time.get_ticks_msec() - start_ms) / 1000.0 >= timeout:
			break
		await get_tree().process_frame
	return _err(
		CliControlErrorCodes.SCENE_UNAVAILABLE, "timeout waiting for new scene ready"
	)


## timeout 参数校验：合法返回 float，非法返回 error Dictionary（调用方透传）。
func _parse_timeout(params: Dictionary) -> Variant:
	var raw: Variant = params.get("timeout", _DEFAULT_TIMEOUT)
	if not (raw is int or raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'timeout' must be a number")
	var t: float = float(raw)
	if t < 0.0 or t > _MAX_SCENE_TIMEOUT:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"timeout must be 0..%s" % _MAX_SCENE_TIMEOUT,
		)
	return t


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
```

注意：新建 `.gd` 后 Godot 会生成 `.uid` 文件（参考同目录其它 `.gd.uid`），GUT 跑完后把它一并 `git add`。

- [ ] **Step 5: 跑 GUT 确认通过**

Run: `GODOT_BIN=$HOME/.local/bin/godot ./addons/godot_cli_control/tests/run_gut.sh`
Expected: 全绿（含新 5 个用例）。若 `Class 'CliControlErrorCodes' not found`，见 wait_api.gd 头注释（先 import 一次）。

- [ ] **Step 6: Commit**

```bash
git add addons/godot_cli_control/bridge/scene_api.gd* addons/godot_cli_control/bridge/error_codes.gd addons/godot_cli_control/tests/gut/test_scene_api.gd*
git commit -m "feat(scene): SceneApi —— scene_reload/scene_change async handler + 1008（#98）"
```

---

### Task 2: GameBridge 注册（GUT 注册表 TDD）

**Files:**
- Modify: `addons/godot_cli_control/tests/gut/test_game_bridge.gd`
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd:28-35`（成员）、`:58-61` 附近（_ready 实例化）、`:192-198`（注册表）

- [ ] **Step 1: 写失败测试** —— 在 test_game_bridge.gd 里找到现有「注册表完整性」断言（搜 `wait_signal` 或 `_methods`），对齐其写法补：

```gdscript
func test_registry_has_scene_methods() -> void:
	# 对齐文件内现有注册表断言的获取方式（直接读 bridge._methods）
	assert_true(_bridge._methods.has("scene_reload"), "scene_reload 应已注册")
	assert_eq(str(_bridge._methods["scene_reload"]["kind"]), "async")
	assert_true(_bridge._methods.has("scene_change"), "scene_change 应已注册")
	assert_eq(str(_bridge._methods["scene_change"]["kind"]), "async")
```

（若文件内注册表断言的 setup 方式不同——比如绕过 `_should_activate`——照抄现有用例的 setup，别发明新写法。）

- [ ] **Step 2: 跑 GUT 确认失败**（`_methods` 无 scene_reload）

- [ ] **Step 3: 实现注册** —— game_bridge.gd 三处：

```gdscript
# ① 成员声明（_wait_api 下面）：
var _scene_api: SceneApi = null

# ② _ready 里 _wait_api.setup(...) 之后：
_scene_api = SceneApi.new()
_scene_api.name = "SceneApi"
add_child(_scene_api)

# ③ _register_methods 的 Wait API 段后面：
# Scene API（异步，issue #98）
_methods["scene_reload"] = {"callable": _scene_api.scene_reload_async, "kind": "async"}
_methods["scene_change"] = {"callable": _scene_api.scene_change_async, "kind": "async"}
```

- [ ] **Step 4: 跑 GUT 确认全绿**

- [ ] **Step 5: Commit** —— `git add` 两文件，`feat(scene): GameBridge 注册 scene_reload/scene_change（#98）`

---

### Task 3: Python client.py + bridge.py（单测 TDD）

**Files:**
- Modify: `python/tests/test_client.py`、`python/tests/test_bridge.py`（先看各自现有 wait_signal 用例的 mock 模式，照抄）
- Modify: `python/godot_cli_control/client.py:355-361` 后、`python/godot_cli_control/bridge.py:93-99` 后

- [ ] **Step 1: 写失败测试**（对齐两文件现有 wait_signal 测试的 transport mock / `_run` stub 写法）：

test_client.py 断言要点：
- `scene_reload()` 发出 method=`"scene_reload"`、params=`{"timeout": 10.0}`、RPC timeout=15.0
- `scene_change("res://a.tscn", timeout=3.0)` 发出 method=`"scene_change"`、params=`{"path": "res://a.tscn", "timeout": 3.0}`、RPC timeout=8.0

test_bridge.py 断言要点：
- `GameBridge.scene_reload()` / `.scene_change(path)` 正确委托到 client 同名方法并返回 dict

- [ ] **Step 2: 跑这两个测试文件确认失败**（`AttributeError: scene_reload`）

Run: `.venv/bin/python -m pytest python/tests/test_client.py python/tests/test_bridge.py -q`

- [ ] **Step 3: 实现**

client.py（`wait_signal` 之后）：

```python
    async def scene_reload(self, timeout: float = 10.0) -> dict:
        """重载当前场景并等新场景 ready（issue #98）。

        成功返回 {"scene_path": ..., "name": ...}；无 current scene /
        等 ready 超时走 RPC error（1008 SCENE_UNAVAILABLE），会抛异常。
        """
        return await self.request(
            "scene_reload", {"timeout": timeout}, timeout=timeout + 5.0
        )

    async def scene_change(self, path: str, timeout: float = 10.0) -> dict:
        """切换到指定场景并等新场景 ready（issue #98）。

        path 须为 res:// 或 uid:// 资源路径；不存在/加载失败/超时 → 1008。
        """
        return await self.request(
            "scene_change", {"path": path, "timeout": timeout}, timeout=timeout + 5.0
        )
```

bridge.py（`wait_signal` 之后）：

```python
    def scene_reload(self, timeout: float = 10.0) -> dict:
        """重载当前场景并等新场景 ready（issue #98）。返回 {"scene_path": ..., "name": ...}。"""
        return self._run(self._client.scene_reload(timeout=timeout))

    def scene_change(self, path: str, timeout: float = 10.0) -> dict:
        """切换场景并等新场景 ready（issue #98）。path 须为 res:// 或 uid://。"""
        return self._run(self._client.scene_change(path, timeout=timeout))
```

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: Commit** —— `feat(scene): client/bridge 两层 scene_reload/scene_change（#98）`

---

### Task 4: CLI RpcSpec（单测 TDD）

**Files:**
- Modify: `python/tests/test_cli.py`
- Modify: `python/godot_cli_control/cli.py`

- [ ] **Step 1: 写失败测试**（对齐 test_cli.py 现有 preflight / spec 测试写法）：

```python
def test_scene_change_preflight_rejects_bad_prefix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """scene-change 非 res://、uid:// 路径：连 daemon 前 -1003 + exit 64。

    main() 不接参数（读 sys.argv、以 sys.exit 退出）——必须用
    test_cli.py:1116/:1146 现有 preflight 测试的 monkeypatch+SystemExit 模式。
    """
    import json as _json
    import sys as _sys

    from godot_cli_control.cli import EXIT_USAGE, main

    monkeypatch.setattr(
        _sys, "argv", ["godot-cli-control", "scene-change", "second.tscn"]
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == EXIT_USAGE  # 64
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003
    assert "res://" in payload["error"]["message"]


def test_scene_specs_registered() -> None:
    from godot_cli_control.cli import RPC_BY_NAME

    for name in ("scene-reload", "scene-change"):
        spec = RPC_BY_NAME[name]
        assert spec.preflight is not None
        assert spec.text_formatter({"scene_path": "res://m.tscn", "name": "M"})


def test_scene_text_formatters() -> None:
    from godot_cli_control.cli import RPC_BY_NAME

    r = {"scene_path": "res://main.tscn", "name": "Main"}
    assert RPC_BY_NAME["scene-reload"].text_formatter(r) == \
        "scene reloaded: res://main.tscn (root: Main)"
    assert RPC_BY_NAME["scene-change"].text_formatter(r) == \
        "scene changed: res://main.tscn (root: Main)"
```

（preflight 校验超时参数的负例也补一条：`scene-reload --timeout -1` → 64，同 monkeypatch 模式。注：`test_scene_specs_registered` 断言两个 spec 都有 preflight——本计划给 scene-reload 也配了 timeout preflight（Task 4 Step 3 ①），与该断言自洽；这是对 spec「仅 scene-change 做前缀校验」的轻微超出，理由是契约 5（用法错必须在连接前报）对 timeout 同样适用，且对齐 wait-prop 先例。）

- [ ] **Step 2: 跑 test_cli.py 新用例确认失败**（KeyError: 'scene-reload'）

- [ ] **Step 3: 实现** —— cli.py 四处：

```python
# ① preflight（_preflight_wait_frames 之后）：
def _preflight_scene_reload(ns: argparse.Namespace) -> None:
    timeout = _require_float(ns.timeout, "scene-reload", "timeout")
    if not 0 <= timeout <= 3600:
        raise ValueError(f"scene-reload: timeout 必须在 0..3600 秒，收到 {timeout}")


def _preflight_scene_change(ns: argparse.Namespace) -> None:
    if not ns.scene_path.startswith(("res://", "uid://")):
        raise ValueError(
            f"scene-change: 场景路径必须以 res:// 或 uid:// 开头，收到 {ns.scene_path!r}"
        )
    timeout = _require_float(ns.timeout, "scene-change", "timeout")
    if not 0 <= timeout <= 3600:
        raise ValueError(f"scene-change: timeout 必须在 0..3600 秒，收到 {timeout}")


# ② handler（cmd_wait_frames 之后）：
async def cmd_scene_reload(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.scene_reload(timeout=float(ns.timeout))


async def cmd_scene_change(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.scene_change(ns.scene_path, timeout=float(ns.timeout))


# ③ extra_args（_register_wait_frames_args 之后）：
def _register_scene_reload_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--timeout", default="10",
                   help="等新场景 ready 的超时秒（0..3600，默认 10）")


def _register_scene_change_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("scene_path", help="目标场景资源路径（res:// 或 uid://）")
    p.add_argument("--timeout", default="10",
                   help="等新场景 ready 的超时秒（0..3600，默认 10）")


# ④ RPC_SPECS（wait-frames 条目之后插入）：
    RpcSpec(
        name="scene-reload",
        handler=cmd_scene_reload,
        description=(
            "重载当前场景并阻塞到新场景 ready（per-test 隔离原语）。"
            "失败（无 current scene / 超时）报 1008，exit 1。"
            "注意：返回后此前缓存的所有节点路径/引用全部失效。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="scene-reload",
        extra_args=_register_scene_reload_args,
        preflight=_preflight_scene_reload,
        text_formatter=lambda r: f"scene reloaded: {r.get('scene_path')} (root: {r.get('name')})",
    ),
    RpcSpec(
        name="scene-change",
        handler=cmd_scene_change,
        description=(
            "切换到指定场景并阻塞到新场景 ready。路径不存在/加载失败/超时 "
            "报 1008，exit 1。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="scene-change res://levels/level2.tscn",
        extra_args=_register_scene_change_args,
        preflight=_preflight_scene_change,
        text_formatter=lambda r: f"scene changed: {r.get('scene_path')} (root: {r.get('name')})",
    ),
```

- [ ] **Step 4: 跑 test_cli.py 确认通过**

- [ ] **Step 5: Commit** —— `feat(scene): CLI scene-reload/scene-change + preflight（#98）`

---

### Task 5: pytest plugin `fresh_scene` fixture（单测 TDD）

**Files:**
- Modify: `python/tests/test_pytest_plugin.py`（先读现有用例：该文件怎么 stub daemon/bridge——pytester 还是 monkeypatch，照抄）
- Modify: `python/godot_cli_control/pytest_plugin.py`

- [ ] **Step 1: 写失败测试** —— 断言要点：
  1. `fresh_scene` 在 yield 前调了 `bridge.scene_reload()` 恰好一次
  2. yield 出来的就是同一个 bridge 对象
  3. teardown 不再调 scene_reload（总调用次数仍为 1）

实现思路（若现有文件用 pytester）：内联 conftest override `bridge` fixture 为 Mock(spec=GameBridge)，测试体里 `assert bridge.scene_reload.call_count == 1`。若现有文件直接调 fixture 函数，则照那个模式。

- [ ] **Step 2: 跑确认失败**（fixture 'fresh_scene' not found）

- [ ] **Step 3: 实现** —— pytest_plugin.py 的 `bridge` fixture 之后：

```python
@pytest.fixture
def fresh_scene(bridge: GameBridge) -> Iterator[GameBridge]:
    """Function-scoped：setup 时 reload 当前场景并等新场景 ready（issue #98）。

    语义是「本用例开始时场景是干净的」：teardown 不做事，下一个需要干净
    场景的用例自己声明 fresh_scene。reload 后此前缓存的节点路径全部失效。

        def test_jump(godot_daemon, fresh_scene):
            fresh_scene.click("/root/Game/Start")   # fresh_scene 即 bridge
    """
    bridge.scene_reload()
    yield bridge
```

同时更新模块 docstring 的 fixtures 列表行（第 1 行附近 `godot_daemon (session) + bridge (function)` 处补 fresh_scene）。

- [ ] **Step 4: 跑 test_pytest_plugin.py 确认通过**

- [ ] **Step 5: Commit** —— `feat(scene): pytest fresh_scene fixture（#98）`

---

### Task 6: e2e（真 Godot 全链路）

**Files:**
- Create: `examples/platformer-demo/second.tscn`
- Create: `python/tests/test_e2e_scene.py`（harness 整体照抄 `test_e2e_example.py:1-99`——`find_godot_binary` skipif、`_run_cli`、`demo_project`(module) + `daemon` fixtures）

- [ ] **Step 1: 加 second.tscn**

```
[gd_scene format=3]

[node name="Second" type="Node2D"]

[node name="Marker" type="Label" parent="."]
text = "second scene"
```

- [ ] **Step 2: 写 e2e 测试**（先写完整文件再一次跑——e2e 起 daemon 成本高，不逐用例 RED/GREEN）

`demo_project` 拷贝列表在 example 的基础上加 `"second.tscn"`。用例：

```python
def test_scene_reload_resets_mutated_state(daemon: Any) -> None:
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main/UI/StartButton")["result"]["found"]
    # 污染场景状态
    assert _run_cli(project, "set", "/root/Main/UI/StartButton", "text", '"DIRTY"')["ok"]
    dirty = _run_cli(project, "get", "/root/Main/UI/StartButton", "text")
    assert dirty["result"]["value"] == "DIRTY"
    # reload → 状态归位
    r = _run_cli(project, "scene-reload")
    assert r["ok"] is True, r
    assert r["result"]["name"] == "Main"
    assert r["result"]["scene_path"] == "res://main.tscn"
    clean = _run_cli(project, "get", "/root/Main/UI/StartButton", "text")
    assert clean["result"]["value"] == "Start", "reload 后属性应回到场景文件初值"


def test_scene_change_switches_to_second(daemon: Any) -> None:
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main")["result"]["found"]
    r = _run_cli(project, "scene-change", "res://second.tscn")
    assert r["ok"] is True, r
    assert r["result"]["name"] == "Second"
    assert _run_cli(project, "exists", "/root/Second")["result"] is True
    # 旧场景根应已不在
    assert _run_cli(project, "exists", "/root/Main")["result"] is False


def test_scene_change_missing_scene_returns_1008(daemon: Any) -> None:
    project = daemon
    r = _run_cli(project, "scene-change", "res://__missing__.tscn")
    assert r["ok"] is False
    assert r["error"]["code"] == 1008


def test_bridge_scene_reload_roundtrip(daemon: Any) -> None:
    """fresh_scene fixture 的核心调用（bridge.scene_reload）真链路验证。"""
    from godot_cli_control.bridge import GameBridge

    project = daemon
    port = int((project / ".cli_control" / "port").read_text().strip())
    b = GameBridge(port=port)
    try:
        result = b.scene_reload()
        assert result["name"] == "Main"
    finally:
        b.close()
```

（`exists` 的信封 shape 与端口文件路径以现有 e2e/代码为准——动手前各 grep 一眼：`grep -rn "exists" python/tests/test_e2e_example.py`、`grep -rn "cli_control.*port" python/godot_cli_control/daemon.py`。不符就按实际调整。）

- [ ] **Step 3: 跑 e2e 确认通过**

Run: `.venv/bin/python -m pytest python/tests/test_e2e_scene.py -q`（真 Godot，必跑）
Expected: 4 passed。常见坑：忘了把 second.tscn 加进拷贝列表 → 1008。

- [ ] **Step 4: Commit** —— `test(scene): e2e 真场景 reload/change/1008/bridge 直连（#98）`

---

### Task 7: 文档同步（契约 7）

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`
- Modify: `addons/godot_cli_control/README.md`

- [ ] **Step 1: SKILL.md 模板四处**
  1. `## Command catalogue` 的 **Wait** 组后加 **Scene** 组：
     ```markdown
     **Scene:**
     - `scene-reload [--timeout N]` — reload the current scene and block until the new instance is ready (per-test isolation primitive). All previously cached node paths become stale after it returns.
     - `scene-change <res://path.tscn> [--timeout N]` — switch to another scene and block until ready. Path must start with `res://` or `uid://` (checked before connecting).
     ```
  2. `## Error code reference` 表 1007 行后加：
     ```markdown
     | `1008` | Scene unavailable (`scene-reload` / `scene-change`): no current scene, scene file missing / failed to load, or timed out waiting for the new scene to become ready. Missing file is permanent — fix the path; timeout usually means the scene itself fails to load — inspect the daemon log. |
     ```
  3. `## pytest plugin` 段示例处补 `fresh_scene` fixture 一句 + 示例改用 `fresh_scene`
  4. `## Common pitfalls` 加两条：场景切换后旧节点路径全部失效需重新 `wait-node`；`scene-reload` 返回后原场景已被释放，不要复用此前缓存的节点引用/路径
- [ ] **Step 2: addon README** —— 命令表 / 错误码表加同样条目（结构以该文件现状为准）
- [ ] **Step 3: 渲染检查**

Run: `.venv/bin/python -c "from godot_cli_control import cli; print(cli.format_full_help())" | head -50`
Expected: 不崩，help 含 scene-reload / scene-change。

- [ ] **Step 4: 跑 `python/tests/test_skills_install.py`**（init 注入回归）
- [ ] **Step 5: Commit** —— `docs(scene): SKILL.md/README 同步 Scene 命令组 + 1008 + fresh_scene（#98）`

---

### Task 8: 全量验证 + PR

- [ ] **Step 1: Python 全量** —— `.venv/bin/coverage run -m pytest python/tests/ -q` + `coverage report --fail-under=80`（委托 subagent）
- [ ] **Step 2: GUT 全量** —— `run_gut.sh`（委托 subagent）
- [ ] **Step 3: push + PR**

```bash
git push -u origin feat/98-scene-isolation
# PR 标题：feat(scene): scene-reload/scene-change + pytest fresh_scene（#98）
# body 含 Fixes #98 + 测试结论；gh pr merge --auto --squash
```

注意：PR #120（#118/#119 快修）可能已先合，若 main 有更新先 rebase。

- [ ] **Step 4: 收尾盘点** —— 按仓库 CLAUDE.md「实施收尾必开 Issue」规则盘点遗留（如有），CLAUDE.md 已知遗留段更新留给 PR 合并后。
