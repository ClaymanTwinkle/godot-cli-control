# 时间控制（#102）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `time_scale` / `pause` / `unpause` / `step_frames` RPC + CLI + `daemon start --time-scale` + pytest plugin 透传，给 e2e 提供整体倍速与确定性帧推进。

**Architecture:** GDScript 侧新建 `time_api.gd`（对齐 wait_api/scene_api 拆分先例）；`step_frames` 以「已 paused 前置（否则 1009）→ unpause → await N 帧 → 恒写回 paused」实现；启动倍速走 Godot cmdline arg `--cli-time-scale=<x>`（`_parse_port_from_args` 同构静态解析，第 0 帧生效）；Python 走标准三层 + daemon/plugin 两处透传。

**Tech Stack:** Godot 4 GDScript + GUT 9、Python ≥3.10、websockets、pytest / pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-06-04-time-control-design.md`（已批准）

**分支：** `feat/102-time-control`（已建，基于 #98 合并后的 main）

**仓库铁律（执行者必读，同 #98 计划）：**
- 测试执行委托 subagent（subagent-driven 模式下 implementer 自己跑即满足）。
- Python 覆盖率：`coverage run -m pytest`，**不能** `pytest --cov`。
- GUT：`GODOT_BIN=$HOME/.local/bin/godot ./addons/godot_cli_control/tests/run_gut.sh`（godot 在 `~/.local/bin/godot`，必须真跑）。
- e2e 需真实 Godot，本机必真跑。
- 服务端错误 message 用英文（对齐现有 handler）；CLI preflight message 用中文（对齐现有 preflight）。

---

## File Structure

| 动作 | 文件 | 职责 |
|---|---|---|
| Create | `addons/godot_cli_control/bridge/time_api.gd` | TimeApi：4 个 handler + 静态 cmdline 解析 |
| Create | `addons/godot_cli_control/tests/gut/test_time_api.gd` | GUT 单测 |
| Modify | `addons/godot_cli_control/bridge/error_codes.gd` | 加 `NOT_PAUSED = 1009` |
| Modify | `addons/godot_cli_control/bridge/game_bridge.gd` | 实例化 TimeApi + 启动倍速 + 注册 4 RPC |
| Modify | `addons/godot_cli_control/tests/gut/test_game_bridge.gd` | 注册表断言补 4 方法 |
| Modify | `python/godot_cli_control/client.py` / `bridge.py` | 4 方法 ×2 层 |
| Modify | `python/godot_cli_control/cli.py` | 4 个 RpcSpec + `daemon start --time-scale` |
| Modify | `python/godot_cli_control/daemon.py` | `start(time_scale=None)` → argv 追加 |
| Modify | `python/godot_cli_control/pytest_plugin.py` | `--godot-cli-time-scale` 选项透传 |
| Create | `python/tests/test_e2e_time.py` | e2e（4 用例真 Godot） |
| Modify | `python/tests/test_client.py` / `test_bridge.py` / `test_cli.py` / `test_daemon.py` / `test_pytest_plugin.py` | 各层单测 |
| Modify | `python/godot_cli_control/templates/skill/SKILL.md` + `addons/godot_cli_control/README.md` | Time 命令组 + 1009 + pitfalls ×3 |

---

### Task 1: GDScript TimeApi（GUT TDD）

**Files:**
- Create: `addons/godot_cli_control/tests/gut/test_time_api.gd`
- Create: `addons/godot_cli_control/bridge/time_api.gd`
- Modify: `addons/godot_cli_control/bridge/error_codes.gd`

**背景：** GUT 模式参考 `test_scene_api.gd`（#98 刚加的，同形态：错误分支 + async await）。**注意 GUT 环境里改 `Engine.time_scale` / `get_tree().paused` 会泄漏到其它测试文件——`after_each` 必须还原**。`await get_tree().process_frame` 是 SceneTree 信号、不依赖节点 process_mode，paused 下照常发射，所以 step_frames 可在 GUT 真测。

- [ ] **Step 1: 写失败测试**

```gdscript
## GUT 单元测试：TimeApi（issue #102）
##
## Engine.time_scale / get_tree().paused 是全局状态，after_each 必须还原，
## 否则污染同一进程里的其它测试文件。
extends GutTest

const TimeApiScript := preload("res://addons/godot_cli_control/bridge/time_api.gd")

var _api: Node


func before_each() -> void:
	_api = TimeApiScript.new()
	_api.name = "TimeApi"
	add_child_autofree(_api)


func after_each() -> void:
	Engine.time_scale = 1.0
	get_tree().paused = false


# ── time_scale ──

func test_time_scale_read_returns_current() -> void:
	var result: Dictionary = _api.handle_time_scale({})
	assert_does_not_have(result, "error")
	assert_eq(float(result.get("time_scale")), 1.0)


func test_time_scale_write_sets_engine_and_returns_new_value() -> void:
	var result: Dictionary = _api.handle_time_scale({"value": 2.5})
	assert_does_not_have(result, "error")
	assert_eq(float(result.get("time_scale")), 2.5)
	assert_eq(Engine.time_scale, 2.5)


func test_time_scale_non_number_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_time_scale({"value": "fast"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


func test_time_scale_zero_returns_minus_32602() -> void:
	## =0 冻死 wait-time 且与 pause 职责重叠，拒收
	var result: Dictionary = _api.handle_time_scale({"value": 0.0})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)
	assert_eq(Engine.time_scale, 1.0, "拒收时不得改 Engine.time_scale")


func test_time_scale_above_max_returns_minus_32602() -> void:
	var result: Dictionary = _api.handle_time_scale({"value": 100.5})
	assert_has(result, "error")
	assert_eq(int(result.error.code), -32602)


# ── pause / unpause ──

func test_pause_unpause_idempotent() -> void:
	var r1: Dictionary = _api.handle_pause({})
	assert_eq(r1.get("paused"), true)
	assert_true(get_tree().paused)
	var r2: Dictionary = _api.handle_pause({})
	assert_eq(r2.get("paused"), true, "重复 pause 幂等成功")
	var r3: Dictionary = _api.handle_unpause({})
	assert_eq(r3.get("paused"), false)
	assert_false(get_tree().paused)
	var r4: Dictionary = _api.handle_unpause({})
	assert_eq(r4.get("paused"), false, "重复 unpause 幂等成功")


# ── step_frames ──

func test_step_frames_not_paused_returns_1009() -> void:
	var result: Dictionary = await _api.step_frames_async({"frames": 2})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1009)
	assert_string_contains(str(result.error.message), "pause")


func test_step_frames_invalid_frames_returns_minus_32602() -> void:
	get_tree().paused = true
	for bad: Variant in [null, "abc", 0, 3601]:
		var result: Dictionary = await _api.step_frames_async({"frames": bad})
		assert_has(result, "error", "frames=%s 应报错" % [bad])
		assert_eq(int(result.error.code), -32602)


func test_step_frames_advances_and_ends_paused() -> void:
	get_tree().paused = true
	var result: Dictionary = await _api.step_frames_async({"frames": 2})
	assert_does_not_have(result, "error")
	assert_eq(int(result.get("stepped")), 2)
	assert_eq(result.get("paused"), true)
	assert_true(get_tree().paused, "step 结束必须停在 paused")


func test_step_frames_physics_advances() -> void:
	get_tree().paused = true
	var result: Dictionary = await _api.step_frames_async({"frames": 2, "physics": true})
	assert_does_not_have(result, "error")
	assert_eq(int(result.get("stepped")), 2)


# ── parse_cmdline_time_scale（静态纯函数）──

func test_parse_cmdline_time_scale_valid() -> void:
	var args := PackedStringArray(["--headless", "--cli-time-scale=2.5"])
	assert_eq(TimeApiScript.parse_cmdline_time_scale(args), 2.5)


func test_parse_cmdline_time_scale_absent_returns_minus_1() -> void:
	assert_eq(TimeApiScript.parse_cmdline_time_scale(PackedStringArray(["--headless"])), -1.0)


func test_parse_cmdline_time_scale_invalid_returns_minus_1() -> void:
	for bad: String in ["--cli-time-scale=abc", "--cli-time-scale=0", "--cli-time-scale=150", "--cli-time-scale="]:
		assert_eq(
			TimeApiScript.parse_cmdline_time_scale(PackedStringArray([bad])), -1.0,
			"%s 应返回 -1" % bad
		)
```

- [ ] **Step 2: 跑 GUT 确认失败**（preload 失败，Scripts 数不变）

Run: `GODOT_BIN=$HOME/.local/bin/godot ./addons/godot_cli_control/tests/run_gut.sh`

- [ ] **Step 3: error_codes.gd 加 1009**（`SCENE_UNAVAILABLE` 之后）

```gdscript
# step_frames 的状态前置错（issue #102）：tree 未 paused 时调 step_frames。
# 与 -32602 区分：参数本身没问题，是世界状态不满足前置——agent 应先 pause。
const NOT_PAUSED: int = 1009
```

- [ ] **Step 4: 写 time_api.gd**

```gdscript
class_name TimeApi
extends Node
## 时间控制 API：time_scale / pause / unpause / step_frames（issue #102）
##
## 挂在 GameBridge（PROCESS_MODE_ALWAYS）下，tree paused 时 RPC 照常工作——
## 这是 step_frames「paused 下推帧」的机制基础。错误码常量见 error_codes.gd。

# time_scale 合法域 (0, 100]：=0 冻死 wait-time 且与 pause 职责重叠，拒收；
# 上限防呆（误传 1e9 之类）。
const _MAX_TIME_SCALE: float = 100.0
# step_frames 防呆上限，对齐 wait_api._MAX_WAIT_FRAMES
const _MAX_STEP_FRAMES: int = 3600


## 解析 --cli-time-scale=<x> 启动参数（_parse_port_from_args 同构）。
## 无该参数返回 -1.0；非法值（非数字 / 越界）printerr 警告后也返回 -1.0
## ——不挡启动。静态纯查询：GameBridge 传 OS.get_cmdline_args()，GUT 传构造数组。
static func parse_cmdline_time_scale(args: PackedStringArray) -> float:
	for arg: String in args:
		if arg.begins_with("--cli-time-scale="):
			var parts: PackedStringArray = arg.split("=", false, 1)
			if parts.size() != 2 or not parts[1].is_valid_float():
				printerr("GameBridge: invalid %s ignored (must be a number in (0, %s])" % [arg, _MAX_TIME_SCALE])
				return -1.0
			var scale: float = parts[1].to_float()
			if scale <= 0.0 or scale > _MAX_TIME_SCALE:
				printerr("GameBridge: invalid %s ignored (must be in (0, %s])" % [arg, _MAX_TIME_SCALE])
				return -1.0
			return scale
	return -1.0


## value 缺省 = 读当前值（Engine 不是节点，get 够不着，这是唯一读通道）。
func handle_time_scale(params: Dictionary) -> Dictionary:
	if not params.has("value"):
		return {"time_scale": Engine.time_scale}
	var raw: Variant = params["value"]
	if not (raw is int or raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'value' must be a number")
	var scale: float = float(raw)
	if scale <= 0.0 or scale > _MAX_TIME_SCALE:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"value must be > 0 and <= %s (use pause to freeze the game, not time_scale 0)" % _MAX_TIME_SCALE,
		)
	Engine.time_scale = scale
	return {"time_scale": scale}


func handle_pause(_params: Dictionary) -> Dictionary:
	get_tree().paused = true
	return {"paused": true}


func handle_unpause(_params: Dictionary) -> Dictionary:
	get_tree().paused = false
	return {"paused": false}


## paused 前置下确定性推进 N 帧再停。未 paused → 1009（fail-loud：
## 教会 agent「pause → step → 断言」的正确模型，不隐式改全局状态）。
func step_frames_async(params: Dictionary) -> Dictionary:
	var frames_raw: Variant = params.get("frames", null)
	if not (frames_raw is int or frames_raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'frames' must be an integer")
	var frames: int = int(frames_raw)
	if frames < 1 or frames > _MAX_STEP_FRAMES:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"frames must be 1..%d (got %d)" % [_MAX_STEP_FRAMES, frames],
		)
	var physics: bool = bool(params.get("physics", false))
	if not get_tree().paused:
		return _err(
			CliControlErrorCodes.NOT_PAUSED,
			"step_frames requires the tree to be paused — call pause first",
		)
	get_tree().paused = false
	for _i in frames:
		if physics:
			await get_tree().physics_frame
		else:
			await get_tree().process_frame
	# 无条件恒写：即使推进期间外力改了 paused，返回时也保证停在 paused
	get_tree().paused = true
	return {"stepped": frames, "paused": true}


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
```

- [ ] **Step 5: 跑 GUT 确认全绿**（新增 13 个用例；基线数以执行时实跑为准——main 合入 #98 后已是 153）
- [ ] **Step 6: Commit** —— `feat(time): TimeApi —— time_scale/pause/unpause/step_frames + 1009（#102）`

---

### Task 2: GameBridge 接线 + 启动倍速（GUT TDD）

**Files:**
- Modify: `addons/godot_cli_control/tests/gut/test_game_bridge.gd`
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`

- [ ] **Step 1: 写失败测试** —— 对齐 #98 的 `test_registry_has_scene_methods` 写法（同 setup）：

```gdscript
func test_registry_has_time_methods() -> void:
	for m: String in ["time_scale", "pause", "unpause"]:
		assert_true(_bridge._methods.has(m), "%s 应已注册" % m)
		assert_eq(str(_bridge._methods[m]["kind"]), "sync")
	assert_true(_bridge._methods.has("step_frames"), "step_frames 应已注册")
	assert_eq(str(_bridge._methods["step_frames"]["kind"]), "async")
```

（before_each 需要照 `_scene` 的先例加 `_time` 实例并赋给 `_bridge._time_api`，否则 `_register_methods` 解引用 null。）

- [ ] **Step 2: 跑 GUT 确认失败**
- [ ] **Step 3: 实现** —— game_bridge.gd 四处：

```gdscript
# ① 成员（_scene_api 下面）：
var _time_api: TimeApi = null

# ② _ready 里 _scene_api 块之后（关键：必须在 await _wait_first_frame_ready() 之前，
#    保证「第 0 帧即倍速」——spec 明确要求）：
_time_api = TimeApi.new()
_time_api.name = "TimeApi"
add_child(_time_api)
# 启动倍速（issue #102）：非法值 parse 内已 printerr + 忽略，不挡启动
var startup_scale: float = TimeApi.parse_cmdline_time_scale(OS.get_cmdline_args())
if startup_scale > 0.0:
	Engine.time_scale = startup_scale
	print("GameBridge: Engine.time_scale = %s (from --cli-time-scale)" % startup_scale)

# ③ _register_methods 的 Scene API 段后面：
# Time API（issue #102）
_methods["time_scale"] = {"callable": _time_api.handle_time_scale, "kind": "sync"}
_methods["pause"] = {"callable": _time_api.handle_pause, "kind": "sync"}
_methods["unpause"] = {"callable": _time_api.handle_unpause, "kind": "sync"}
_methods["step_frames"] = {"callable": _time_api.step_frames_async, "kind": "async"}
```

- [ ] **Step 4: 跑 GUT 全绿**
- [ ] **Step 5: Commit** —— `feat(time): GameBridge 注册 time RPC + 启动倍速 cmdline（#102）`

---

### Task 3: client/bridge 两层（单测 TDD）

**Files:** `python/tests/test_client.py`、`python/tests/test_bridge.py`、`python/godot_cli_control/client.py`、`python/godot_cli_control/bridge.py`

- [ ] **Step 1: 写失败测试**（照 #98 scene 系列的 fake_request / _StubClient 模式）。断言要点：
  - `time_scale()`（无参）→ method="time_scale"、params=`{}`（**不带 value 键**）
  - `time_scale(2.5)` → params=`{"value": 2.5}`
  - `pause()` / `unpause()` → 各自 method、params=`{}`
  - `step_frames(5)` → params=`{"frames": 5, "physics": False}`、RPC timeout=30.0（`max(30, 5/10+10)`）
  - `step_frames(600)` → RPC timeout=70.0
  - bridge 四个包装委托 + 返回透传
- [ ] **Step 2: 跑两文件确认失败**（AttributeError）
- [ ] **Step 3: 实现** —— client.py（`scene_change` 之后）：

```python
    async def time_scale(self, value: float | None = None) -> dict:
        """读 / 写 Engine.time_scale（issue #102）。

        value=None 纯读。合法域 (0, 100]，越界 → -32602。
        返回 {"time_scale": x}。wait_game_time 跟随 time_scale，
        套件整体倍速时 wait 语义不变、墙钟变快。
        """
        params: dict = {} if value is None else {"value": value}
        return await self.request("time_scale", params)

    async def pause(self) -> dict:
        """暂停 SceneTree（issue #102）。幂等；返回 {"paused": True}。"""
        return await self.request("pause", {})

    async def unpause(self) -> dict:
        """恢复 SceneTree（issue #102）。幂等；返回 {"paused": False}。"""
        return await self.request("unpause", {})

    async def step_frames(self, frames: int, physics: bool = False) -> dict:
        """paused 前置下确定性推进 N 帧再停（issue #102）。

        未 pause → 1009 NOT_PAUSED。返回 {"stepped": N, "paused": True}。
        网络超时对齐 wait_frames（最低 10fps 估算 + 10s grace）。
        """
        return await self.request(
            "step_frames",
            {"frames": frames, "physics": physics},
            timeout=max(30.0, frames / 10.0 + 10.0),
        )
```

bridge.py（`scene_change` 之后）四个同款同步包装（docstring 一句话 + `self._run(...)`）。

- [ ] **Step 4: 跑确认通过** → **Step 5: Commit** —— `feat(time): client/bridge 两层时间控制（#102）`

---

### Task 4: CLI RpcSpec ×4（单测 TDD）

**Files:** `python/tests/test_cli.py`、`python/godot_cli_control/cli.py`

- [ ] **Step 1: 写失败测试**（照 #98 scene 系列模式，含 `_ShouldNotConnect`）：
  - `test_time_specs_registered`：四个 spec 在 `RPC_BY_NAME`；time-scale / step-frames 有 preflight
  - `test_time_text_formatters`：`time-scale` → `"time_scale = 2.5"`；`pause` → `"paused: True"`→（按实现定，见 Step 3）；`step-frames` → `"stepped 5 frames (still paused)"`
  - preflight 负例（parametrize + monkeypatch sys.argv + SystemExit 64 + -1003 信封）：`time-scale 0`、`time-scale -1`、`time-scale 101`、`time-scale abc`；`step-frames 0`、`step-frames 3601`、`step-frames abc`
  - 正例不连 daemon 没法跑，注册性断言即可
- [ ] **Step 2: 跑确认失败**（KeyError）
- [ ] **Step 3: 实现** —— cli.py 四处：

```python
# ① preflight（_preflight_scene_change 之后）：
def _preflight_time_scale(ns: argparse.Namespace) -> None:
    if ns.value is None:
        return  # 无参 = 读当前值
    v = _require_float(ns.value, "time-scale", "value")
    if not 0 < v <= 100:
        raise ValueError(
            f"time-scale: value 必须 > 0 且 <= 100，收到 {v}（要冻结游戏用 pause，别用 0）"
        )


def _preflight_step_frames(ns: argparse.Namespace) -> None:
    try:
        frames = int(ns.frames)
    except (TypeError, ValueError):
        raise ValueError(f"step-frames: frames 必须是整数，收到 {ns.frames!r}")
    if not 1 <= frames <= 3600:
        raise ValueError(f"step-frames: frames 必须在 1..3600，收到 {frames}")


# ② handler（cmd_scene_change 之后）：
async def cmd_time_scale(client: GameClient, ns: argparse.Namespace) -> dict:
    value = float(ns.value) if ns.value is not None else None
    return await client.time_scale(value)


async def cmd_pause(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.pause()


async def cmd_unpause(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.unpause()


async def cmd_step_frames(client: GameClient, ns: argparse.Namespace) -> dict:
    return await client.step_frames(int(ns.frames), physics=ns.physics)


# ③ extra_args（_register_scene_change_args 之后）：
def _register_time_scale_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("value", nargs="?", default=None,
                   help="新倍速（>0 且 <=100）；省略则读当前值")


def _register_step_frames_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("frames", help="推进帧数（1..3600）")
    p.add_argument("--physics", action="store_true",
                   help="推进 physics_frame（默认 process_frame）")


# ④ RPC_SPECS（scene-change 条目之后插入）：
    RpcSpec(
        name="time-scale",
        handler=cmd_time_scale,
        description=(
            "读 / 写 Engine.time_scale（无参 = 读）。wait-time 按 game time 计，"
            "倍速后语义不变、墙钟变快。合法域 (0, 100]。注意：--record 下仍生效，"
            "录出的是加速画面。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="time-scale 5",
        extra_args=_register_time_scale_args,
        preflight=_preflight_time_scale,
        text_formatter=lambda r: f"time_scale = {r.get('time_scale')}",
    ),
    RpcSpec(
        name="pause",
        handler=cmd_pause,
        description="暂停 SceneTree（get_tree().paused = true）。幂等。",
        positionals=(),
        example="pause",
        text_formatter=lambda r: f"paused: {r.get('paused')}",
    ),
    RpcSpec(
        name="unpause",
        handler=cmd_unpause,
        description="恢复 SceneTree。幂等。",
        positionals=(),
        example="unpause",
        text_formatter=lambda r: f"paused: {r.get('paused')}",
    ),
    RpcSpec(
        name="step-frames",
        handler=cmd_step_frames,
        description=(
            "paused 状态下确定性推进 N 帧再停（物理断言银弹：推 N 个物理帧后状态"
            "必然确定）。必须先 pause，否则报 1009，exit 1。"
        ),
        positionals=(),  # 由 extra_args 注册
        example="step-frames 3 --physics",
        extra_args=_register_step_frames_args,
        preflight=_preflight_step_frames,
        text_formatter=lambda r: f"stepped {r.get('stepped')} frames (still paused)",
    ),
```

- [ ] **Step 4: 跑 test_cli.py 全文件通过** → **Step 5: Commit** —— `feat(time): CLI time-scale/pause/unpause/step-frames + preflight（#102）`

---

### Task 5: daemon --time-scale + plugin 透传（单测 TDD）

**Files:** `python/tests/test_daemon.py`、`python/tests/test_pytest_plugin.py`、`python/godot_cli_control/daemon.py`、`python/godot_cli_control/cli.py`（daemon start 段）、`python/godot_cli_control/pytest_plugin.py`

- [ ] **Step 1: 写失败测试**：
  - test_daemon.py（照 `:1279` 的 `--game-bridge-idle-timeout` Popen argv 捕获先例）：
    - `daemon.start(time_scale=2.5)` → argv 含 `--cli-time-scale=2.5`
    - 不传 → argv 无 `--cli-time-scale` 前缀
    - `daemon.start(time_scale=0)` / `(time_scale=101)` → 抛 `DaemonError`（spawn 前拒绝，对齐 "--record requires --movie-path" 先例）
  - test_pytest_plugin.py（照现有 FakeDaemon kwargs 捕获模式）：`runpytest("--godot-cli-time-scale", "3")` → `FakeDaemon.start` 收到 `time_scale=3.0`；不传 → `time_scale=None`
  - test_cli.py：`daemon start --time-scale abc` / `--time-scale 0` → exit 64 + -1003 信封（argparse type 校验路径）
- [ ] **Step 2: 跑确认失败**
- [ ] **Step 3: 实现**：

daemon.py —— `start()` 签名加 `time_scale: float | None = None`，校验（`record and headless` 检查之后）：

```python
        if time_scale is not None and not 0 < time_scale <= 100:
            raise DaemonError(
                f"--time-scale 必须 > 0 且 <= 100，收到 {time_scale}"
                "（要暂停游戏用 pause RPC，不要 time-scale 0）"
            )
```

argv 构造（`idle_timeout` 行之后）：

```python
        if time_scale is not None:
            args.append(f"--cli-time-scale={time_scale}")
```

cli.py —— daemon start 子命令注册处（找 `--idle-timeout` 的注册位置，同处）加：

```python
def _time_scale_arg(raw: str) -> float:
    """argparse type：daemon start --time-scale 的域校验（错误走 -1003 + 64）。"""
    try:
        v = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是数字，收到 {raw!r}")
    if not 0 < v <= 100:
        raise argparse.ArgumentTypeError(f"必须 > 0 且 <= 100，收到 {v}")
    return v

# daemon start parser:
    p.add_argument("--time-scale", type=_time_scale_arg, default=None,
                   help="启动即设 Engine.time_scale（>0 且 <=100），整套 e2e 倒速用")
```

`cmd_daemon_start` 把 `ns.time_scale` 传给 `d.start(..., time_scale=ns.time_scale)`。

注：argparse 的 `add_subparsers` 默认 `parser_class=type(self)`，所以 daemon start
子 parser 也是 `_EnvelopeArgumentParser`，`ArgumentTypeError` 会走 error() →
-1003 信封 + 64（cli.py 类 docstring 也说明了子 parser error() 共享 argv）。
若测试发现不走信封（极小概率），回退方案：去掉 type 校验，改在
`cmd_daemon_start` 里手动校验抛用法错（对齐 `_resolve_idle_timeout` 模式）。

pytest_plugin.py —— addoption 组里加：

```python
    group.addoption(
        "--godot-cli-time-scale",
        action="store",
        default=None,
        help="Engine.time_scale applied at daemon startup (e.g. 5 to speed up the whole suite).",
    )
```

`godot_daemon` fixture 的 start 调用改为：

```python
    raw_scale = config.getoption("--godot-cli-time-scale")
    time_scale = float(raw_scale) if raw_scale is not None else None
    ...
    daemon.start(headless=headless, port=port, time_scale=time_scale)
```

- [ ] **Step 4: 跑 test_daemon.py + test_pytest_plugin.py + test_cli.py 通过**
- [ ] **Step 5: Commit** —— `feat(time): daemon start --time-scale + pytest plugin 透传（#102）`

---

### Task 6: e2e（真 Godot）

**Files:** Create `python/tests/test_e2e_time.py`（harness 照抄 `test_e2e_scene.py`：**含 module-scope `demo_project` fixture 与 function-scope `daemon` fixture 的完整定义，不能跨文件引用**；拷贝列表用 example 原版四文件即可，本文件不需要 second.tscn）

- [ ] **Step 1: 写完整测试文件**（e2e 不逐用例 RED/GREEN）。用例：

```python
def test_pause_freezes_and_step_frames_advances_physics(daemon: Any) -> None:
    """pause 冻结物理 → step-frames --physics 确定性推进 → unpause。"""
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main/UI/StartButton")["result"]["found"]
    assert _run_cli(project, "click", "/root/Main/UI/StartButton")["ok"]
    assert _run_cli(project, "wait-node", "/root/Main/World/Player")["result"]["found"]
    # player 正在下落；pause 后位置必须冻结
    assert _run_cli(project, "pause")["result"]["paused"] is True
    y1 = _run_cli(project, "get", "/root/Main/World/Player", "position:y")["result"]["value"]
    _run_cli(project, "wait-time", "0.3")  # 墙钟流逝，但树已 pause
    y2 = _run_cli(project, "get", "/root/Main/World/Player", "position:y")["result"]["value"]
    assert y2 == y1, f"paused 下位置不得变化：{y1} -> {y2}"
    # 确定性推进 10 个物理帧：重力必须使 y 增大
    r = _run_cli(project, "step-frames", "10", "--physics")
    assert r["ok"] is True, r
    assert r["result"]["stepped"] == 10
    assert r["result"]["paused"] is True
    y3 = _run_cli(project, "get", "/root/Main/World/Player", "position:y")["result"]["value"]
    assert y3 > y2, f"10 个物理帧后应下落：{y2} -> {y3}"
    assert _run_cli(project, "unpause")["result"]["paused"] is False


def test_step_frames_without_pause_returns_1009(daemon: Any) -> None:
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main")["result"]["found"]
    r = _run_cli(project, "step-frames", "3")
    assert r["ok"] is False
    assert r["error"]["code"] == 1009


def test_time_scale_roundtrip(daemon: Any) -> None:
    project = daemon
    assert _run_cli(project, "wait-node", "/root/Main")["result"]["found"]
    assert _run_cli(project, "time-scale")["result"]["time_scale"] == 1.0
    assert _run_cli(project, "time-scale", "5")["result"]["time_scale"] == 5.0
    assert _run_cli(project, "time-scale")["result"]["time_scale"] == 5.0
    # 还原，避免影响同 daemon 的后续操作（虽然 daemon 是 function-scope，防御性）
    assert _run_cli(project, "time-scale", "1")["ok"]


def test_daemon_start_time_scale_applies_at_startup(demo_project: Path) -> None:
    """daemon start --time-scale 2：连上即读到 2.0（第 0 帧生效路径）。"""
    project = demo_project
    start = _run_cli(project, "daemon", "start", "--headless", "--time-scale", "2", timeout=90)
    assert start["ok"] is True and start["result"]["started"], start
    try:
        r = _run_cli(project, "time-scale")
        assert r["result"]["time_scale"] == 2.0
    finally:
        _run_cli(project, "daemon", "stop", timeout=30)
```

（前三个用例用 function-scope `daemon` fixture；第四个自管 daemon 起停，签名直接依赖 `demo_project`。）

- [ ] **Step 2: 跑 e2e 确认通过**

Run: `GODOT_BIN=$HOME/.local/bin/godot .venv/bin/python -m pytest python/tests/test_e2e_time.py -q`
Expected: 4 passed。坑提示：若 `position:y` 的 get shape 与断言不符，先 grep test_e2e_example.py 的 get 断言写法对齐。

- [ ] **Step 3: Commit** —— `test(time): e2e pause/step-frames/time-scale/启动倍速（#102）`

---

### Task 7: 文档同步（契约 7）

**Files:** `python/godot_cli_control/templates/skill/SKILL.md`、`addons/godot_cli_control/README.md`

- [ ] **Step 1: SKILL.md 五处**（英文，风格对齐周边）：
  1. Command catalogue 的 **Scene:** 组后加 **Time:** 组：
     ```markdown
     **Time:**
     - `time-scale [value]` — read (no arg) or set `Engine.time_scale`. Valid range `(0, 100]`. `wait-time` counts game time, so a higher scale speeds up the whole suite without changing wait semantics.
     - `pause` / `unpause` — freeze / resume the scene tree (`get_tree().paused`). Idempotent.
     - `step-frames <n> [--physics]` — while paused, advance exactly N frames then stop (deterministic stepping for physics assertions). Requires `pause` first — otherwise error `1009`.
     ```
  2. Error code reference 表 1008 行后加 1009 行（NOT_PAUSED：step-frames called while the tree is not paused — call `pause` first; precondition error, not a param error）
  3. Daemon management 段：`daemon start` 选项处补 `--time-scale N`（applies `Engine.time_scale` from the very first frame）
  4. pytest plugin 段：CLI 选项列表补 `--godot-cli-time-scale`
  5. Common pitfalls 三条：
     - With `--record` (Movie Maker fixed-FPS), `time_scale` still applies — the captured video plays back sped-up. Don't combine unless that's what you want.
     - `time-scale` also shortens the wall-clock duration of `wait-time` (game-time semantics unchanged) — don't "compensate" wait times after scaling.
     - `step-frames` requires `pause` first (error `1009`) — the intended pattern is `pause` → `step-frames` → assert → `unpause`.
- [ ] **Step 2: addon README**：命令表加 4 行 + 错误码表加 1009（风格照抄现有行）
- [ ] **Step 3: 渲染检查**：`format_full_help()` 含 time-scale/pause/unpause/step-frames；跑 `test_skills_install.py`
- [ ] **Step 4: Commit** —— `docs(time): SKILL.md/README 同步 Time 命令组 + 1009 + pitfalls（#102）`

---

### Task 8: 全量验证 + PR

- [ ] **Step 1: Python 全量**（`coverage run -m pytest` + `--fail-under=80`，带 GODOT_BIN 让 e2e 真跑；委托 subagent）
- [ ] **Step 2: GUT 全量**（委托 subagent）
- [ ] **Step 3: push + PR**（标题 `feat(time): time-scale/pause/step-frames 时间控制（#102）`，body 含 Fixes #102 + 测试结论；`gh pr merge --auto --squash`；若 main 有更新先 rebase）
- [ ] **Step 4: 收尾盘点**（按 CLAUDE.md「实施收尾必开 Issue」；CLAUDE.md 已知遗留段更新留到批次收尾）
