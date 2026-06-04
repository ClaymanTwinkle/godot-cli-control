# Changelog

## [Unreleased]

### Fixed (BREAKING — exit code changes)

- **#111 argparse 用法错 exit 2 → exit 64**: 所有 argparse 层错误（缺位置参数、非法 choices、未知子命令等）现在统一输出 `-1003` 信封并以 exit 64 退出。之前是 exit 2（argparse 默认）。影响：脚本若用 `[ $? -eq 2 ]` 判 argparse 错需改为 `[ $? -eq 64 ]`。
- **#92 `run <script>` 前置错误拆分**:
  - 脚本路径不存在 / 脚本缺 `run(bridge)` 函数：由旧的 exit 2（或 exit 1）改为 exit 64，错误码由 `-1003` 保持不变。这是用法错，应修正调用而非重试。
  - `daemon start`（`run` 自动启停 / `daemon start` 命令）/ `daemon stop` 系统级失败（端口冲突、找不到 Godot binary、PID 文件丢失等）：错误码由 `-1003` 改为 **新码 `-1006` (PRECONDITION)**，exit code 保持 exit 2。影响：`run`/`daemon` 失败时，通过 `error.code` 区分用法错（`-1003` → 修调用）与基础设施错（`-1006` → 修环境）现在变得无歧义。

### Added
- **feat(wait): `wait-prop` / `wait-signal` / `wait-frames` 条件等待原语 + 错误码 1007（#96）**: 三个新 CLI 子命令。`wait-prop <path> <prop> <value> [--op eq|ne|gt|lt|ge|le] [--timeout] [--tolerance]` 等属性满足条件（exit 0=matched, 1=timeout）；`wait-signal <path> <signal> [--timeout]` 等信号发射（exit 0=emitted, 1=timeout）；`wait-frames <N> [--physics]` 推进 N 帧。服务端错误码 1007 SIGNAL_NOT_FOUND（wait-signal 传未知信号名，永久性 schema 错，不应 retry）。Python `GameClient` 对应方法：`wait_property` / `wait_signal` / `wait_frames`。

### Changed
- **BREAKING** `get` 复合 Variant 返回从旧版 `"(x, y)"` 字符串改为 set-schema 数组 + type 字段；信封 result 从裸值变 `{"value": <array-or-scalar>, "type"?: "<GodotType>"}` 对象（#99）。写侧 `set` 接受的 Array layout 完全一致，get→set round-trip 无需转换。Python API `get_property` / `get_properties` 只返回裸 value（type 已剥离），要拿 type 字段走 CLI `get` 或底层 `client.request("get_property", ...)`。

### Added
- **feat: `get_properties` 多属性同帧原子读 + `get` 多属性形式（#100）**：`get <path> <prop1> <prop2>` 一次 RPC 同帧读取多个属性，不存在部分新鲜/部分旧数据的竞态；任一属性缺失整体失败（1002，message 点名所有缺失项）。sub-path 形式（`position:x`）在多属性列表中也支持。

### Fixed
- **#52 `set` 走 JSON Array 喂 Vector/Color/Rect 时静默失败**：`set zoom '[1.8, 1.8]'` 等价于 `node.set("zoom", [1.8, 1.8])`，Godot 隐式构造失败 → 实际值是 `Vector2(0,0)` 或被 clamp 到 `0.00001`，但服务端仍返 `{success: true}`。`handle_set_property` 现在查 `get_property_list()` 拿声明类型，把 numeric Array 转成对应 Variant。长度不匹配或元素非数字时 fail-loud 返 `-32602 "value type mismatch ..."`，不再 silent corruption。
- **sub-path 标量赋值现在真的会写入**：之前 `set <node> position:x 1.8` 调的是 `Object.set("position:x", 1.8)`，Godot 4 的 `Object.set` 把整串当字面属性名找不到就 silent no-op（依旧返 `{success: true}` 但 `position.x` 不变）。改用 `Object.set_indexed(NodePath, value)` 才会按 sub-path 写入。是 #54 review 阶段被新加的 `test_set_subpath_scalar_still_works` 捕获的隐藏 silent-fail。

### Added
- **feat(input): press/tap/hold/combo 注入 InputEventAction 进事件管线，_input/_unhandled_input 可见（#97）**：之前的实现只调 `Input.action_press/release`，仅翻轮询状态位，事件回调收不到。现改为 `Input.parse_input_event(InputEventAction)`，轮询（`is_action_pressed`/`get_vector`）与事件回调（`_input`/`_unhandled_input`）两种游戏写法均可感知注入输入。边界：`InputEventAction` 不携带鼠标坐标，依赖位置的 `_gui_input` 控件仍需用 `click`。
- **#54 全部复合 Variant 都支持 Array → Variant coerce**：从 4-float 简单类型到 16-float 矩阵，全部按 axis-vector 顺序的 flat numeric Array 写入（每 N 元素 = 一个 Vector 轴）。覆盖：`Vector2/2i/3/3i/4/4i`、`Rect2/2i`、`Color`（3-element=RGB / 4-element=RGBA）、`Plane(a,b,c,d)`、`Quaternion(x,y,z,w)`、`AABB(pos 3, size 3)`、`Basis(9 axis-vector)`、`Transform2D(xaxis 2, yaxis 2, origin 2)`、`Transform3D(basis 9, origin 3)`、`Projection(16 axis-vector)`。具体 layout 见 SKILL.md。`Plane.normal` 和 `Quaternion` 不会自动归一化（与 Godot ctor 一致）。
- **#54 防御性 fallback**：未在 coerce 名单也不在 `_ARRAY_PASSTHROUGH_SAFE_TYPES`（基本类型 / Object / 集合 / Packed*Array）白名单的声明类型 + Array 输入会 fail-loud，避免未来 Godot 加新 compound Variant 时 silent-corrupt 回归。
- **#54 sub-path + Array 现在 fail-loud**：`set <node> transform:origin '[10, 20, 30]'` 在 Godot 里会 silent-corrupt（Vector3 leaf 不会从 Array 隐式构造，origin 仍是 (0,0,0)），server 现在主动返 `-32602 "sub-path + Array is not supported"` 提示走 top-level 形式（如 `set <node> transform '[basis 9, origin 3]'`）。sub-path 标量赋值（`position:x 1.8`）不变。

### AI-friendly CLI 改造（多个 BREAKING change）

把 CLI 重定位成 AI agent 的一等接口：默认结构化输出、补齐读 / 写 / 发现的 shell 命令、明确退出码契约。shell-only 的 agent 现在不需要写 Python 脚本就能完成全部操作。

#### Added
- **12 个新 RPC 子命令**（覆盖客户端全部能力）：`get` / `set` / `call` / `text` / `exists` / `visible` / `children` / `wait-node` / `wait-time` / `pressed` / `combo-cancel` / `actions`。
- **顶层 `--json` / `--text` / `--no-json`**：默认 `--json`，输出统一信封 `{"ok": true, "result": ...}` 或 `{"ok": false, "error": {"code": N, "message": "..."}}`，单行 stdout 易于 jq / json.loads。
- **`combo` 三种喂法**：`--steps-json '[...]'`（inline）/ `combo combo.json`（文件，旧用法保留）/ `combo -`（stdin）。
- **`exists` / `visible` / `wait-node` exit-code-as-result**：shell `if godot-cli-control exists /root/Foo; then …` 直接可用。
- **`actions` 默认过滤 `ui_*` 内置**，`--all` 看全。
- **`list_input_actions` RPC**（GD 端）+ `GameClient.list_input_actions(include_builtin)` + `GameClient.get_pressed()`：客户端缺的两个方法补齐。
- **`RpcError` 异常**（继承 `RuntimeError`）保留服务端 `code` 字段，给上层做特定错误码 retry / 信封序列化。
- **GUT 测试**覆盖 `list_input_actions` 默认过滤 / `--all` / 字母序。

#### Changed
- **BREAKING**：`--json` 默认开。RPC 子命令的 stdout 现在是 JSON 信封；旧的人类可读字符串通过 `--text`（或别名 `--no-json`）保留。
- **BREAKING**：`screenshot` 的 `output_path` 改成必填。旧"省略路径 → base64 灌 stdout"行为已删（撑爆 LLM 上下文）。
- **BREAKING**：RPC 错误从裸 traceback 改成结构化信封，exit code 1（RPC error）/ 2（连接、超时、用法错误）。脚本里靠 grep traceback 文本判错的需要切到读 exit code + JSON `error.code`。
- `daemon status` 在 JSON 模式下也产出信封 `{"state": "running", "pid": ..., "port": ...}` 或 `{"state": "stopped"}`；exit code 语义不变（0=running, 1=stopped）。
- SKILL.md 模板重写：新增 *AI Quickstart* / *Exit codes* / *JSON envelope* 段；shell vs Python 论调倒过来——shell 是 canonical，Python 桥仅在跨步保持单连接时使用。

#### Fixed
- `GameClient.request()` 之前丢掉了服务端 `error.code`（统一 raise `RuntimeError` 只带 message）；现在 raise `RpcError(code, message)` 透传。
- `combo` 用法错误（无 steps、`--steps-json` 与位置参数互冲）现在通过 preflight 在连 daemon **之前**报 `EXIT_USAGE(64)`，不再让 agent 干等 30s 连接 retry。
- connection retry 日志从 `WARNING` 降到 `DEBUG`：`cmd 2>&1 | jq` 不再被 retry 行污染；最终的 `ConnectionError` 仍由 dispatcher 统一信封到 stdout。
- `daemon start` 在 `--json` 模式下产出 `{"ok":true,"result":{"started":true,"port":N,"pid":N}}`；`daemon stop` 输出 `{"ok":true,"result":{"stopped":true,"rc":N}}`，`rc` 透出方便 agent 判定 ffmpeg 转码是否成功。Text 模式行为不变。

#### Docs
- SKILL.md 增加 *Error code reference*：服务端 `1001-1004` / JSON-RPC `-32xxx` / 客户端 `-1xxx` 三段含义的总表（含新增的 `-1004` IO error 与 `-1099` internal error）。
- SKILL.md 增加 `set` / `call` 的安全 blacklist 提示（`queue_free` / `set_script` 等会被 `-32602` 拒绝），以及 JSON 解析 footgun 例（`set ... text null` 会存 Variant null，要存字符串得 `'"null"'`）。
- README.md *Highlights* 重写"Two client APIs"行为"Shell is canonical"，反映新的 CLI 一等地位。
- `GameBridge` (sync) 补齐 `get_text` / `is_visible` / `combo_cancel` 三个方法，与 async `GameClient` 表面对齐。

#### Hardening (post-review)
- `_run_rpc` 加兜底 `except Exception` 把任何未预见的客户端异常（`AttributeError` / `KeyError` / 协议解析意外等）也落进 `-1099` 信封，绝不让 traceback 漏到 stdout 破坏 `--json` 契约。完整 traceback 仍写 stderr 帮 debug。
- `OSError` 拆分：网络层（`socket.gaierror` / errno ECONNREFUSED 等）继续走 `-1001`；本地文件 IO（`screenshot` 写盘失败常见的 `PermissionError` / `FileNotFoundError`）走新增的 `-1004` (IO)，避免 agent 误以为 daemon 挂了去重启。
- `combo -` 检测 stdin 是 TTY 时直接拒绝（之前会 `read()` 无限阻塞）。
- `screenshot` 路径自动 `expanduser()`：`screenshot ~/foo.png` 不再创建字面 `~` 目录。

#### Out of scope (intentional)
- `run <script.py>` 子命令保留旧的人类可读 stderr 输出。它是交互式脚本宿主，不是 RPC，新的 JSON 信封契约只覆盖 RPC 子命令 + daemon 三命令。

### AI-friendliness review fixes (2026-05-11)

#### Fixed
- **CLI flag position**: `--json` / `--text` / `--no-json` 现在在 RPC 子命令尾部也接受（之前只能写最前面）。argparse 子 parser 用 `default=argparse.SUPPRESS` + 顶层 `set_defaults` 兜底，避免子 parser 默认值覆盖父 parser 解析结果。
- **Error code 1004 collision**: `low_level_api.gd` 的 scene tree 超限改用新业务码 `1005 "scene tree too large"`，与 input_simulation `1004 "combo in progress"` 解耦。新增 `error_codes.gd` 集中常量。
- **pytest fixture default port**: `--godot-cli-port` 默认从 9877 改为 0（OS-assigned），与 `daemon start` 默认对齐；多项目并行测试不再撞端口。
- **Addon README error-code table**: 之前只列到 1003，补全 1004 / 1005 / 客户端 -1xxx 段。
- **Scene tree hard limit bypass (DoS fix)**: `handle_get_scene_tree` 入口现在把 `max_nodes` clamp 到硬墙 `_BUILD_TREE_MAX_NODES` (5000)。修复前客户端传 `max_nodes=999999` 会让服务端先把整棵超大树构造成 Dictionary 再被 1005 错误丢弃（内存浪费 / OOM 路径）。
- **Error code 1003 semantic split**: screenshot 在 viewport texture 为 null 时不再借用 `1003 METHOD_NOT_FOUND`，改用新业务码 `1006 RESOURCE_UNAVAILABLE`。1003 现在是纯 schema 错（不应 retry），1006 是 transient 错（短重试可能成功），agent 据此分别处置。

#### Added
- `tree --max-nodes <N>`（默认 200）：节点数软上限；超出时响应含 `truncated: true` + `total_nodes`，agent 据此决定分子树。硬墙仍是 5000 节点 → `1005`。
- `set` / `call --text-value`：禁用 JSON 解析、把 value/args 强制按字符串处理，避开 `null` / `true` / `42` 这类字面量被解析成 Variant 类型的 footgun。

#### Changed
- **BREAKING (轻微)**：`daemon start` / `run` 默认 headless 行为改为基于 `sys.stdout.isatty()` 自动判定 —— pipe / CI / agent shell 默认 headless；交互终端默认开窗。新增 `--gui` 强制开窗 flag。`--headless` 仍可显式传，覆盖自动判。脚本里依赖 "默认会开窗" 的需要加 `--gui`。

## [0.1.6] - Unreleased

### Added
- `godot-cli-control init`: 一键 onboarding 子命令 + 跨平台 Python daemon (取代手写 wrapper)。
- pytest11 entry-point: `godot_daemon` / `bridge` fixtures,装包即用。
- PowerShell wrapper (`bin/run_cli_control.ps1`),原生 Windows 不需 WSL。
- LowLevelApi: 项目可通过 ProjectSetting 扩展 property/method 黑名单。
- GameBridge: `outbound_buffer_size` 可经 ProjectSetting 配置。
- Skill 集成:`init` 默认渲染并写入 Claude + Codex 的 `SKILL.md`(`--no-skills` / `--skills-only` / `--skills-no-clobber` 三档可选)。
- CLI `-h`:子命令分组 + per-command usage / examples。
- GUT 单测覆盖 `LowLevelApi` + `InputSimulationApi`,接入 CI。
- CI 矩阵扩展到 Windows + macOS;walkthrough 改写为 Python。

### Changed
- **BREAKING**: 移除基于方向的 `move_*` API — 插件不应假设项目里的 action 名(811ce99)。
- `runner` 委托到 `cli._exec_user_script`,统一脚本执行路径。
- `cli.build_parser` 改为 public(为 skill 渲染暴露)。
- `__version__` 改为从 `_version.py`(hatch-vcs 生成)读取,不再硬编码。
- 安装文档统一切到 `pipx install godot-cli-control`。

### Fixed
- `bridge`: 加固 JSON-RPC,容忍畸形请求与超长 combo steps。
- `bridge`: 日志里的 URL 与实际 listen addr 对齐。
- `daemon`: 启动时清理上一次残留的 `movie_path`。
- `client`: 使用 `127.0.0.1` 字面量连接,绕开 `localhost` IPv6 解析问题。
- `cli`: 用户脚本可被 import + dataclass 友好;`finally` 块恢复 `sys.path` / `sys.modules`。
- `cancel_combo` 正确把响应回送给 caller;反射类方法纳入黑名单。
- 截屏在 headless 模式下不再 hang。
- CI(Windows runner):强制 stdout utf-8 让中文 `print` 不崩;保留 Godot 原始 exe 名。

## [0.1.0] – [0.1.5]
- 早期未维护 changelog;关键节点见 git tag `v0.1.0`..`v0.1.5`。
- Initial release as Godot 4 plugin + Python package.
- 21 RPC methods (节点查询 / 操作 / 输入模拟 / 截图 / 等待).
- Three activation modes: `--cli-control` flag, `GODOT_CLI_CONTROL=1` env var, Project Setting (debug-build only).
- Property/method blacklist for security.
- Movie writer / record support via wrapper script.

### Known Limitations
- Headless + Movie Maker is incompatible (Godot upstream issue): record requires GUI mode.
- Default `GODOT_BIN` path is macOS-specific.
- Windows 用户从 0.1.5 起可用 PowerShell wrapper / Python daemon,无需 WSL。
