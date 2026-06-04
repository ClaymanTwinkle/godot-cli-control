# 时间控制设计：time-scale / pause / unpause / step-frames（issue #102）

日期：2026-06-04
状态：已批准（brainstorming 四节逐节确认）

## 问题

方法注册表没有 `Engine.time_scale` / `get_tree().paused` / 帧步进类控制：

1. e2e 套件时长被真实时间 sleep 主导（XGame 实测两个 e2e 文件里 0.15~0.3s 的
   `wait_game_time` 10+ 处，全按真实墙钟流逝）
2. 无法确定性帧推进：物理/动画类断言只能「等足够久再读」，本质是和调度器赛跑

## 目标

1. `time_scale`：读 / 写 `Engine.time_scale`——`wait_game_time` 用 `create_timer`
   （跟随 time_scale），所以「wait-time 语义不变、墙钟整体倍速」天然成立
2. `pause` / `unpause`：写 `get_tree().paused`
3. `step_frames`：paused 状态下确定性推进 N 帧再停（「推 3 个物理帧后位置必然是 X」）
4. `daemon start --time-scale N`：启动即设默认倍速（pytest/CI 整套倒速场景）

## 已决策的设计选择

| 决策点 | 选择 | 备注 |
|---|---|---|
| step_frames 的 pause 前置 | 要求已 paused，否则报错（1009） | fail-loud 教会 agent「pause → step → 断言」模型；不隐式改全局状态 |
| pause CLI 形态 | `pause` / `unpause` 两个命令 | 动词即意图，零参数零歧义 |
| 启动倍速 | 这轮做，走 Godot cmdline arg | `--cli-time-scale=<x>`，第 0 帧生效无竞态；读取路径对齐 `--cli-control` 激活旗标先例 |
| 与 --record 交互 | 文档说明，不加硬限制 | Movie Maker 下 time_scale 仍生效（录出加速画面），可预测行为；合法场景（故意录倒速）不误伤 |
| time-scale 读通道 | CLI 无参 = 读当前值 | Engine 不是节点，`get` 够不着，这是唯一读通道 |
| time_scale 合法域 | `(0, 100]` | =0 冻死 wait-time 且与 pause 职责重叠，拒收；>100 防呆 |
| GDScript 放置 | 独立 `time_api.gd` | 对齐 wait_api（#108）/ scene_api（#98）拆分先例 |
| plugin 透传 | 这轮做 `--godot-cli-time-scale` | issue 核心动机是 e2e 提速，这一步闭环 |

## 组件设计

### 1. GDScript：`addons/godot_cli_control/bridge/time_api.gd`

新文件，`class_name TimeApi extends Node`。GameBridge `_ready` 实例化并
`add_child`（子节点继承 GameBridge 的 `PROCESS_MODE_ALWAYS` → tree paused 时
RPC 照常工作，这是 step_frames 的机制基础）。注册：

```
_methods["time_scale"]  = {"callable": _time_api.handle_time_scale, "kind": "sync"}
_methods["pause"]       = {"callable": _time_api.handle_pause, "kind": "sync"}
_methods["unpause"]     = {"callable": _time_api.handle_unpause, "kind": "sync"}
_methods["step_frames"] = {"callable": _time_api.step_frames_async, "kind": "async"}
```

**`handle_time_scale(params)`**（sync）

1. `value` 可选：缺省 → 返回 `{"time_scale": Engine.time_scale}`（纯读）
2. 有 value：非 int/float → `-32602`；`<= 0.0` 或 `> 100.0` → `-32602`
3. 设置 `Engine.time_scale = value`，返回 `{"time_scale": value}`

**`handle_pause(params)` / `handle_unpause(params)`**（sync）

- 写 `get_tree().paused`，幂等（重复调用照常成功），返回 `{"paused": true/false}`

**`step_frames_async(params)`**（async）

1. `frames` 必填 int，`1..3600`（对齐 wait_frames `_MAX_WAIT_FRAMES` 防呆）→ 否则 `-32602`
2. `physics` 可选 bool（默认 false）
3. 前置：`get_tree().paused` 必须为 true → 否则 **`1009 NOT_PAUSED`**
   （message 给出正确用法：「pause first, then step-frames」）
4. 执行：`paused = false` → `await get_tree().physics_frame / process_frame` ×N
   → `paused = true` → 返回 `{"stepped": N, "paused": true}`
5. `await process_frame` 是 SceneTree 信号、不依赖节点 process_mode，
   GUT 环境下也能真测

**启动倍速**

- `static func parse_cmdline_time_scale(args: PackedStringArray) -> float`：
  解析 `--cli-time-scale=<x>`，无该参数 / 非数字 / 越界（(0,100] 外）返回 `-1.0`
  ——静态纯函数，GUT 可单测
- GameBridge `_ready`（激活判定之后）调用它（读取路径对齐 `--cli-control`
  现有实现），返回值 > 0 则 `Engine.time_scale = x`；非法值 `printerr` 警告
  并忽略，**不挡启动**

### 2. 错误码

`error_codes.gd` 新增 `NOT_PAUSED: int = 1009`：step_frames 的状态前置错
（业务码——与 -32602 参数错区分：参数没问题，是世界状态不满足前置）。

### 3. Python 三层 + daemon + plugin

- **client.py**：
  - `time_scale(value: float | None = None) -> dict`（params 仅在 value 非 None 时带 `"value"`）
  - `pause()` / `unpause() -> dict`
  - `step_frames(frames: int, physics: bool = False) -> dict`，
    RPC 网络超时对齐 wait_frames：`max(30.0, frames / 10.0 + 10.0)`
- **bridge.py**：四个同步包装
- **cli.py**：四个 `RpcSpec`：
  - `time-scale [value]`（value 可选位置参数；preflight：有值时数字 + (0,100]）
  - `pause` / `unpause`（零参数）
  - `step-frames <n> [--physics]`（preflight：整数 + 1..3600）
  - 退出码默认语义（1009 → exit 1），无 `exit_code_from`
- **daemon.py**：`start(..., time_scale: float | None = None)`，非 None 时启动
  参数追加 `--cli-time-scale=<x>`；CLI `daemon start --time-scale N`
  （preflight 域校验）
- **pytest plugin**：`--godot-cli-time-scale` 选项（默认 None），
  `godot_daemon` fixture 透传 `daemon.start(time_scale=...)`

## 测试策略

| 层 | 文件 | 覆盖 |
|---|---|---|
| GUT | `tests/gut/test_time_api.gd`（新） | time_scale 读/写/非法值与域负例；pause/unpause 幂等；step_frames 未 paused → 1009、frames 非法 → -32602、**paused 下真推帧**（pause → step 2 帧 → 断言返回 shape 且结束仍 paused）；`parse_cmdline_time_scale` 静态单测（合法/缺失/非数字/越界）。注意 teardown 还原 `Engine.time_scale = 1.0` 与 `paused = false`，避免污染其它测试文件 |
| Python 单测 | test_cli.py / test_client.py / test_bridge.py / test_daemon.py / test_pytest_plugin.py | preflight 域校验、RpcSpec 注册、三层委托（mock）、daemon 启动参数含 `--cli-time-scale`、plugin 选项透传 |
| e2e | `test_e2e_time.py`（新） | ① pause → step-frames --physics 推进（platformer player 受重力，断言 position:y 确定性变化且推进期间外 paused 不动）→ unpause；② time-scale 设置/读回往返；③ daemon start --time-scale 启动即生效（连上直接读 time_scale 断言） |

## 文档同步（契约 7）

- SKILL.md 模板：Command catalogue 加 **Time** 分组（time-scale / pause /
  unpause / step-frames）、错误码表加 1009、`daemon start --time-scale` 说明、
  Common pitfalls 三条：
  1. `--record`（Movie Maker 固定帧率）下 time_scale 仍生效——录出的是加速画面
  2. time-scale 同时加速 `wait-time` 的墙钟表现（game-time 语义不变）
  3. `step-frames` 必须先 `pause`，否则 1009
- addon README：命令表 / 错误码表同步
- 跑 `format_full_help()` 渲染检查 + `test_skills_install.py`

## 不做的事（YAGNI）

- 不做 `step-frames` 的自动 pause（fail-loud 已决策）
- 不做 record 模式下的 time_scale 硬限制（文档说明已决策）
- 不做 `Engine.physics_ticks_per_second` 等其它时间参数的暴露
- 不做 pause 状态查询 RPC（`pause`/`unpause` 返回值已带；真需要再说）
