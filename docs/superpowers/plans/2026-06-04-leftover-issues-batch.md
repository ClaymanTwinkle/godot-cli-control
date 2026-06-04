# 遗留 issue 批次实现计划（#111 #92 #108 #110 #112 #107 #113）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 三个独立 PR 清掉 7 个遗留 issue：退出码语义统一（#111+#92）、wait 家族文件拆分（#108）、小杂项批次（#110+#112+#107+#113）。

**Architecture:** 三个 PR 均直接基于 main、顺序合并（不 stack）。PR-A 纯 Python 侧；PR-B 纯 GDScript 侧搬移；PR-C 依赖 PR-B 的新文件。

**Tech Stack:** Python ≥3.10 / argparse / pytest；Godot 4 GDScript / GUT。

**用户已确认的设计决策（AskUserQuestion，2026-06-04）：**
- #111 → 改代码统一到 64（自定义 `ArgumentParser.error`）
- #92 → 混合拆分：脚本路径不存在 / 缺 `run(bridge)` 是用法错 → `-1003` + exit 64；`DaemonError` 是真 infra → 新码 `-1006 CLIENT_CODE_PRECONDITION` + exit 2
- #109 保持 KIV 不动

**基线事实（main @ ac0b402，已实测核对）：**
- `cli.py:43-60`：`EXIT_OK=0 / EXIT_RPC_ERROR=1 / EXIT_INFRA_ERROR=2 / EXIT_PARTIAL=3 / EXIT_USAGE=64`；`CLIENT_CODE_CONNECTION=-1001 / TIMEOUT=-1002 / USAGE=-1003 / IO=-1004 / SCRIPT_ERROR=-1005 / INTERNAL=-1099`。**-1005 已被占用，新前置码必须用 -1006。**
- `cli.py:1681-1890` `build_parser()`：无自定义 `error()`/`parser_class`；subparsers 在 1711/1719/1875 三处创建（argparse 的 `add_subparsers` 默认 `parser_class=type(self)`，根 parser 用子类后子 parser 自动继承——实现时必须验证这一点）。
- `cli.py:1216-1225` 脚本路径不存在 → `-1003` + `EXIT_INFRA_ERROR`；`cli.py:1245-1261` `cmd_run` 内 `DaemonError` → `-1003` + `EXIT_INFRA_ERROR`；`cli.py:1026-1028` `cmd_daemon_start`、`cli.py:1096-1098` `cmd_daemon_stop`（单项目分支）同款带 `-1003`。**`cmd_daemon_stop --all` 的 `DaemonError` 分支（1071-1073）只设 `entry["rc"] = EXIT_INFRA_ERROR`，没有 code 字段，不在 #92 改动范围。**
- **缺 `run(bridge)` 在 `cli.py:1446-1448`：`_emit_usage(...)`（发 -1003）+ `return 1` —— 当前是 -1003 + exit 1。`_exec_user_script` 内共 5 处裸 `return 1`（1388 加载失败、1445 load_error、1448 missing_run、1460 connection、1467 fallback），#92 只许改 1448 这一处，碰其他会污染 SCRIPT_ERROR/CONNECTION 语义。`cmd_run` 把 `_exec_user_script` 返回值原样当 exit code（1277-1282）。**
- `low_level_api.gd` 共 756 行。wait 家族：`_SignalCapture` 74-109、`wait_for_node_async` 497-508、`wait_game_time_async` 542-551、`wait_property_async` 557-605、`_wait_compare` 611-628、`_deep_equal` 631-643、`wait_frames_async` 647-660、`wait_signal_async` 665-709；常量 `_MAX_WAIT_SECONDS`(17)、`_MAX_WAIT_FRAMES`(19)、`_WAIT_PROP_OPS`(21)。
- wait 家族对本文件的依赖：`wait_property_async` → `_read_property()`（589，留在 low_level_api，**经 setup 注入 Callable**）+ `_wait_compare` + `CliControlVariantCodec`；`wait_signal_async` → `_node_not_found()`/`_err()`（小 helper，搬移时复制一份到 wait_api.gd，与 input_simulation_api 自带 helper 同款先例）。
- `game_bridge.gd:49-56`：子 API 实例化先例（`LowLevelApi.new()` + `add_child`；`_input_sim_api.setup(_on_async_response)` 是 setup 注入 Callable 的先例）。wait 5 个方法注册在 `_register_methods()` 的 188-192 行，kind 均为 `"async"`。
- GUT：`test_low_level_api.gd` 共 1034 行，wait 测试覆盖 **848-1034**（wait_frames 848-867、wait_property 871-941、wait_signal 945-1021，**外加 999-1034 的 3 个 timeout/tolerance 类型校验测试**）。搬移时用 `grep -n "func test_.*wait" ` 全量圈定，不要按死区间抄。wait_for_node / wait_game_time 的既有测试也需全文搜索确认。
- **`test_game_bridge.gd` 也依赖 wait 路由**：`before_each`（101-118）手动设 `_bridge._low_level_api = _low`(Stub) + `_register_methods()`；`test_async_handler_success_emits_result_frame`（279-288）发 `wait_for_node` 依赖路由命中 stub。拆分后必须同步加 `StubWaitApi`（或 before_each 设 `_bridge._wait_api`），否则该测试路由到 null callable 必挂。307/323 两处直接覆写 `_methods["wait_for_node"]` 的测试不受影响。
- `test_e2e_input.py:203-221` `probe_project` fixture（function scope，1 个消费者，自管 daemon start/stop 无隔离依赖）；`test_e2e_input.py:114-131` `godot_project` 是 module scope 先例。
- `test_cli.py`：`test_run_script_path_not_exist`（495-520，断言 `-1003`+`EXIT_INFRA_ERROR`）、`test_run_daemon_start_error`（619-651）、`test_cmd_run_catches_unexpected_exception_into_envelope`（658-698，断言 -1099 兜底，别误当 script 错测试）。
- 文档锚点：SKILL.md 模板退出码表 41-50、wait-signal 行 184、get 编码段 152-155、tree 截断 pitfall 253-269；addon README 错误码表 119-141（-1003 括注在 137）+ **退出码表 161-169（169 行写了 #82 那条 2/64 说明，同样要同步）**。

**每个 PR 的统一收尾步骤：** 跑 GUT（`GODOT_BIN=/Users/kesar/.local/bin/godot .venv/bin/python addons/godot_cli_control/tests/run_gut.py`）+ pytest 全量（`cd python && PATH="$PATH:/Users/kesar/.local/bin" ../.venv/bin/coverage run -m pytest -q`，覆盖率 ≥80%）+ 改了 SKILL.md 模板则 `python -c "from godot_cli_control import cli; print(cli.format_full_help())"` 验渲染 + ruff（`../.venv/bin/ruff check .`）。

---

## PR-A（分支 `fix/111-92-exit-code-semantics`）：退出码语义统一

### Task A1: 自定义 ArgumentParser —— argparse 层用法错统一到 -1003 + exit 64（#111）

**Files:**
- Modify: `python/godot_cli_control/cli.py`（`build_parser` 附近）
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: 写失败测试**：`subprocess` 或 `pytest.raises(SystemExit)` 方式断言：① `get`（缺位置参数）→ exit 64 + stdout 单行 JSON `{"ok": false, "error": {"code": -1003, ...}}` + usage 在 stderr；② `wait-prop /n p 1 --op bogus`（非法 choices）→ 同上；③ 未知子命令 → 同上；④ `--help` 仍 exit 0；⑤ argv 含 `--text` 时不输出 JSON（人类可读错误走 stderr），仍 exit 64。
- [ ] **Step 2: 跑测试确认失败**（当前 argparse 默认 exit 2）。
- [ ] **Step 3: 实现**：新增 `class _EnvelopeArgumentParser(argparse.ArgumentParser)`，覆写 `error(self, message)`：`print_usage(sys.stderr)`；若 `"--text"`/`"--no-json"` 不在 `sys.argv` 则 stdout 发 `-1003` JSON 信封（复用 `_emit_error_payload`），否则人类可读错误走 stderr；`raise SystemExit(EXIT_USAGE)`。`build_parser()` 根 parser 换成该子类；**验证子 parser 确实继承了子类**（argparse `add_subparsers` 默认 `parser_class=type(self)`，写一条未知子命令/子命令缺参的测试盯住）。不要覆写 `exit()`（保住 `--help` exit 0）。
- [ ] **Step 4: 跑测试通过 + Commit**

### Task A2: run/daemon 前置失败混合拆分（#92）

**Files:**
- Modify: `python/godot_cli_control/cli.py`
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: 先改既有测试到新期望（失败态）**：`test_run_script_path_not_exist` → 断言 `-1003` + `EXIT_USAGE(64)`；`test_run_daemon_start_error` → 断言 `-1006` + `EXIT_INFRA_ERROR(2)`；缺 `run(bridge)` 路径 → `-1003` + 64（没有现成测试就新增）；`cmd_daemon_start` / `cmd_daemon_stop` 单项目分支的 `DaemonError` → `-1006` + 2。`stop --all` 行为不变（部分失败仍 `EXIT_PARTIAL=3`，entry 无 code 字段，不动）。
- [ ] **Step 2: 实现**：新增 `CLIENT_CODE_PRECONDITION = -1006`（注释：infra 前置失败，恒 exit 2，与 -1003≡64 双射互补）。改五处，一处不多：
  1. `cli.py:1216-1225` 脚本不存在 → `-1003` + `EXIT_USAGE`
  2. `cli.py:1446-1448` 缺 `run(bridge)`：`_emit_usage` 保持 `-1003`，`return 1` → 改为返回会被 `cmd_run` 透传的 `EXIT_USAGE`。**`_exec_user_script` 其余 4 处裸 `return 1`（1388/1445/1460/1467）严禁动**
  3. `cmd_run` `DaemonError`（1245-1261）→ `-1006` + `EXIT_INFRA_ERROR`
  4. `cmd_daemon_start`（1026-1028）→ `-1006` + `EXIT_INFRA_ERROR`
  5. `cmd_daemon_stop` 单项目分支（1096-1098）→ `-1006` + `EXIT_INFRA_ERROR`。**1071-1073 的 `--all` entry["rc"] 是退出码聚合标记不是 code，保持 2 不动**
- [ ] **Step 3: 跑测试通过 + Commit**

### Task A3: 文档全家桶同步

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（退出码表 41-50、错误码表）
- Modify: `addons/godot_cli_control/README.md`（错误码表 119-141：-1003 行去掉「run/daemon 也用 -1003 但 exit 2」括注、新增 -1006 行；**退出码表 161-169 同步**，169 行的 #82 说明要更新）
- Modify: `CLAUDE.md`（退出码语义段：64 含 argparse；2 的描述改为 -1006；「已知遗留 issue」段先不动，PR-C 收尾统一更新）
- Modify: `addons/godot_cli_control/CHANGELOG.md`（BREAKING：argparse 用法错 2→64；run 前置 -1003/2 → 拆分）
- Modify: 根 `README.md` Recent changes（如有同款表）

- [ ] **Step 1: 改文档**（对照 #82 当时的改法）；**Step 2: 验 SKILL.md 渲染**；**Step 3: Commit**

---

## PR-B（分支 `refactor/108-wait-api-split`）：wait 家族拆 wait_api.gd（纯搬移）

### Task B1: 新建 wait_api.gd + game_bridge 改挂载

**Files:**
- Create: `addons/godot_cli_control/bridge/wait_api.gd`
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd`（删除被搬移段）
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`（49-56 实例化 + 175-212 注册改指向）

- [ ] **Step 1: 搬移**：`class_name` 风格对齐 input_simulation_api.gd（`extends Node`）。搬：`_SignalCapture`、5 个 wait_*_async、`_wait_compare`、`_deep_equal`、`_MAX_WAIT_SECONDS`/`_MAX_WAIT_FRAMES`/`_WAIT_PROP_OPS` 常量。`_err`/`_node_not_found` 小 helper 复制一份（input_simulation_api 同款先例）。`_read_property` 依赖：`func setup(read_property: Callable)` 注入（镜像 `_input_sim_api.setup(_on_async_response)`），`wait_property_async` 内改调注入的 Callable。**逐行 diff 校验搬移段与原文一致（仅 helper 调用方式允许差异），不改任何行为。**
- [ ] **Step 2: game_bridge.gd**：`_wait_api = WaitApi.new()` + `add_child` + `setup(_low_level_api._read_property)`；5 个注册条目（188-192）callable 改指 `_wait_api.*`，kind 仍 `"async"`。
- [ ] **Step 3: GUT 搬移**：`test_low_level_api.gd` 用 `grep -n "func test_.*wait"` 全量圈定 wait 测试（实际覆盖 848-1034，含 999-1034 的 3 个 timeout/tolerance 类型校验测试）+ 全文搜索 wait_for_node/wait_game_time 既有测试 → 新建 `tests/gut/test_wait_api.gd`（before_each 实例化 WaitApi + setup 注入，对齐 test_input_simulation_api.gd 29-32 先例）。Tab 缩进。
- [ ] **Step 4: 修 test_game_bridge.gd**：`before_each`（101-118）补 `StubWaitApi`（或设 `_bridge._wait_api` + setup 注入），让 `test_async_handler_success_emits_result_frame`（279-288）的 `wait_for_node` 路由命中 stub；307/323 两处整条覆写 `_methods["wait_for_node"]` 的测试不用动。
- [ ] **Step 5: 跑 GUT 全量通过（139 个测试数不变）+ pytest e2e（wait 相关 e2e 走真协议，是行为不变的最强证据）+ Commit**

---

## PR-C（分支 `chore/110-112-107-113-leftovers`，基于 PR-B 合入后的 main）：小杂项批次

### Task C1: #110 wait_signal 加 reason 字段

**Files:**
- Modify: `addons/godot_cli_control/bridge/wait_api.gd`（原 low_level_api.gd:699-700 的 freed 分支）
- Test: `addons/godot_cli_control/tests/gut/test_wait_api.gd`

- [ ] **Step 1: 写失败 GUT 测试**：等待中 `node.free()` → 返回 `{"emitted": false, "reason": "node_freed"}`；真超时 → `{"emitted": false, "reason": "timeout"}`；命中 → `{"emitted": true, ...}` 无 reason（或 reason 缺省，对齐 wait_property：matched 时无 reason 字段）。
- [ ] **Step 2: 实现**：freed `break` 分支改记 `reason = "node_freed"`，循环耗尽默认 `"timeout"`，未命中返回时带上。**Step 3: 通过 + Commit**

### Task C2: #112 get 路径打磨

**Files:**
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd`
- Test: `addons/godot_cli_control/tests/gut/test_low_level_api.gd`

- [ ] **Step 1**: `raw_prop2/prop_name2`（208-209）去掉数字后缀；抽 `_top_level_of(property: String) -> String` 替换 179/202/229 三处 `property.split(":", true, 1)[0]`（注意三处 guard 形式不一：179/229 用 `is_sub_path` 变量、202 内联 `":" in prop_name`，语义等价，抽取时保持各自调用处行为不变）。
- [ ] **Step 2**: 补接缝 GUT 测试 1-2 个：`handle_get_property` 读 Object 值属性、StringName 值属性 → 断言信封里 value/type 形态（handler↔codec 接缝，codec 单测已有、链路没有）。**Step 3: GUT 通过 + Commit**

### Task C3: #107 codec 深度上限文档 + #113 fixture scope

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（get 段 152-155 + pitfalls，对照 tree 截断 pitfall 253-269 同款写法）
- Modify: `addons/godot_cli_control/README.md`（同步）
- Modify: `python/tests/test_e2e_input.py`（203-221 `probe_project` 加 `scope="module"`，对齐 114-131 `godot_project` 先例）

- [ ] **Step 1**: 文档补「编码递归深度上限 64，超深/自引用降级为字符串 `"<max-depth-exceeded>"`，agent 见到该哨兵应缩小读取范围（读子属性）而非当游戏数据」。**Step 2**: fixture 改 scope（确认现仅 1 消费者无隔离依赖）。**Step 3: 验 SKILL.md 渲染 + e2e input 测试通过 + Commit**

### Task C4: 收尾 —— CLAUDE.md 已知遗留 issue 段更新

- [ ] 更新 CLAUDE.md「已知遗留 issue」段：记录本批（#111/#92/#108/#110/#112/#107/#113）已 land；#109 KIV、#98/#101/#102/#103/#88/#18 仍 open。Commit。
