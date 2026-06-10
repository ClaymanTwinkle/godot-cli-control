# #157 CLI 体验四改（PR A）设计

> 来源 issue：#157「CLI 体验小项清单」。本 spec 只覆盖 **items 1/2/3/5**（PR A）。
> item 4（`emit_signal` 黑名单逃生门）触安全网契约，单独走 PR B，不在本 spec 范围。

**目标**：一次清掉四个 AI/脚本体验毛刺——退出码 2 过载、`--port` 位置敏感、`get` sub-path typo 静默 null、`run` 不支持 `--time-scale`——且不破坏任何 AI 友好契约（JSON 信封、三段错误码、语义化退出码、shell canonical、preflight、SKILL.md 同步）。

**非目标**：item 4；不重构 argparse 整体结构；不动 `set` 侧（其 footgun 0.2.5 已 loud）。

---

## item 5 — `run` 透传 `--time-scale`

**现状**：`daemon start --time-scale N` 有（cli.py argparse + 传 `daemon.start(time_scale=…)`），`run` 没有；`daemon.start()` 签名已含 `time_scale: float | None = None`（daemon.py），校验 `0 < x <= 100` 也已在 `daemon.start`。SKILL.md 自承不对称，workaround 是脚本第一行 `bridge.time_scale(N)`。

**设计**：
1. `run_p` 加 `--time-scale`，**镜像** `start_p` 的定义：`type=_time_scale_arg, default=None, help` 同文案。
2. `cmd_run` 的 `daemon.start(...)` 调用补一行 `time_scale=getattr(ns, "time_scale", None)`。
3. 校验复用既有两道：argparse 的 `_time_scale_arg`（格式 → usage 64）+ `daemon.start` 运行期 `0<x<=100`（→ DaemonError → -1006/exit 2），与 `daemon start` 行为对称，不新增校验逻辑。

**契约影响**：无新错误码、无新退出码。

**测试**：
- `test_cli`：`cmd_run` 的 Namespace mock **必须补 `time_scale` 字段**（命中既知坑：CLI handler 新读 `ns.<字段>` → 手构造 Namespace 缺字段 → AttributeError，且被 `cmd_run` 最外层 except 兜成 -1099 掩盖真因）。新增断言：`cmd_run` 把 `time_scale` 透传进 `daemon.start`（mock daemon 捕获 kwargs）。
- 改完跑**全套** `test_cli.py`，不能只跑触碰文件。

---

## item 2 — RPC 子命令接受后置 `--port`

**现状**：顶层 `--port`（RPC 连接目标，与 `--instance` 同属顶层 `conn_grp` mutex，default=None）必须写在子命令**前**；`--json`/`--text` 已做到位置自由（前后都行）。SKILL.md 列为 pitfall。

**机制参照**：`_add_output_format_flags(p)` 用 `default=argparse.SUPPRESS` 在顶层 parser + 每个 subparser 各加一次 `--json/--text`；SUPPRESS 让 subparser 副本只在显式传入时写 namespace，不覆盖顶层 `set_defaults`。两位置都可写。

**设计**：
1. 抽 `_add_connection_flags(p)`：在 `p` 上建一个 mutex group，加 `--port`（`type=int, dest="port", default=argparse.SUPPRESS`）+ `--instance`（`dest="instance", default=argparse.SUPPRESS`），help 文案复用顶层同名 flag。
2. 在**每个 RPC 子命令** parser 上调用 `_add_connection_flags`（即从 RPC spec 表批量建的那批 subparser）；**不动** daemon start / run / init / logs / status 等 infra 子命令（它们的 `--port` 是监听端口，语义不同，由 `_add_daemon_flags` 提供）。
3. 顶层 `conn_grp` 的 `--port`/`--instance` 保留现状作为「前置」入口与 default 提供者。
4. **跨位置 mutex 守卫**：顶层 mutex 只管前置同给、subparser mutex 只管后置同给；`--instance foo get --port 5` 这种「instance 前、port 后」两边都漏判。parse 后加一次性 guard：若 `--port` 与 `--instance` **都被显式提供**（不论位置）→ usage 错（exit 64 / `-1003`），文案提示二选一。
   - 「是否显式提供」的检测：plan 阶段先读准顶层 `--instance` 的 default 哨兵（None 还是 `"default"` 字符串）。
     - 若顶层两者 default 均为 `None` 且无其它途径写非 None：guard 可简化为 `ns.port is not None and ns.instance is not None`。
     - 若 `--instance` default 是非 None 哨兵：改用 SUPPRESS + `hasattr` 检测显式性，并在 parse 后 `set_defaults`/getattr 兜回 fallback；**务必确认下游所有读 `ns.port`/`ns.instance` 的点不因 SUPPRESS 缺键而 AttributeError**（同 item 5 的 Namespace 坑同源）。

**契约影响**：无新错误码（复用既有 usage `-1003`/exit 64）；行为是**放宽**（多接受一个位置）+ 一个新的冲突 guard。preflight 性质（连 daemon 前就报）。

**测试**：
- `test_cli`：① 后置 `--port` 解析成功（`get /root/x foo --port 9999` → ns.port==9999）；② 前置仍工作（回归）；③ 跨位置 `--port`+`--instance` 同给 → exit 64 / `-1003`；④ 任一 RPC 子命令抽样验证后置可用。
- 注意 argparse parser 是模块级构造，新加 flag 后全套 `test_cli` 跑通。

---

## item 1 — 转码失败专用退出码 `4`

**现状**：`daemon.stop()` 在「进程已正常停止 + 录制原始 AVI 保留 + 仅 ffmpeg 转 mp4 失败」时返回 `rc = 2`（daemon.py），与「连接失败 `-1001`/daemon 起不来 `-1006`」共用 exit 2；`run` 在「脚本成功 + 转码失败」也回 2，信封带 `daemon_stop_warning="daemon stop rc=2"`。脚本无法用 `rc==2` 直接判 infra 故障。

**设计**（已选「新增专用 exit 4」）：
1. cli.py 加常量 `EXIT_TRANSCODE_FAILED = 4`（紧邻现有 `EXIT_OK/…/EXIT_USAGE`）。
2. daemon.py 层加自己的模块常量（如 `STOP_RC_TRANSCODE_FAILED = 4`，**不反向 import cli**，守住分层），`stop()` 转码失败处 `rc = 2 → STOP_RC_TRANSCODE_FAILED`。
3. `daemon stop`（单实例）退出码直接透传 `stop()` 的 rc → 4。
4. `run` finally 块现有 `if exit_code == 0 and stop_rc != 0: exit_code = stop_rc` 天然透传 4；`daemon_stop_warning` 文案改为点明转码失败（如 `"daemon stop rc=4 (mp4 transcode failed; raw recording preserved)"`）。信封仍 `ok:true`（录制原件在、脚本成功，仅转码这步软失败）。
5. `daemon stop --all` 聚合退出码**语义不变**：`_emit_stop_summary` 的聚合 rc 由 `had_failure`（仅 `DaemonError` 异常）驱动，**不看** child 的 stop rc——故某 child 转码失败今天聚合返回 0、改后仍聚合 0，只是 per-entry `rc` 由 2 变 4（细节落在 JSON entry 里）。这是**有意保持现状、不扩 scope**：把转码失败纳入聚合 3 会改变「`stop --all` rc==0」的既有契约，属另议。`--instance all` 是 RPC 广播、不涉转码，不受影响。

**契约影响**：**退出码契约扩张**——4 此前空闲（在用 0/1/2/3/64，sysexits 64-78 不冲突）。必须同步三处退出码表：
- 项目根 **CLAUDE.md**：原则 3 退出码表，把「daemon stop ffmpeg 转码失败也是 2」「`run` 脚本成功+转码失败回 2」移到新行 `4 = 进程已正常停止、仅 mp4 转码失败（录制原件保留；信封仍 ok:true + daemon_stop_warning）`；exit 2 描述删去转码分支。
- **SKILL.md 模板**（`python/godot_cli_control/templates/skill/SKILL.md`）退出码表同款修改。
- **渲染版**（`.claude/skills/godot-cli-control/SKILL.md` 及 `.codex/...` 若仓内有）用 `skills_install.render_skill`，`COLUMNS=80` + python3.12 重渲染。

**测试**：
- pytest：`daemon.stop()` 转码失败返回 4（mock `_transcode_movie` → False）；`run` 脚本成功+转码失败 → exit 4 且 `daemon_stop_warning` 含转码字样；`daemon stop --all` 一 child 转码失败 → 仍 3（回归）。
- `test_skills_install`：渲染注入不崩。

---

## item 3 — `get` sub-path leaf typo fail-loud

**现状**：`_read_property`（low_level_api.gd）对 `position:x` 形 sub-path 只校验 `":"` 前 top-level 名存在（缺则 1002），leaf 段 typo 时 `get_indexed(NodePath)` 静默返回 `null`，与「真 null 值」不可区分。SKILL.md pitfalls 已声明该边界。`set` 侧 0.2.5 已从静默改 loud。

**设计**：
1. `_read_property` 命中 sub-path 时，先 `node.get(top_level)` 取得起点值，对 leaf 段**逐段 walk** 校验，再决定是否放行 `get_indexed`。
2. **封闭复合类型** → 合法 leaf 集映射（GDScript `const` Dictionary，键 `typeof()`，值 `PackedStringArray`）。当前段值类型在 map 中且 leaf **不在**其合法集 → 复用 `CliControlErrorCodes.PROPERTY_NOT_FOUND`（1002），message 形如 `"Sub-path leaf not found: position:typo (valid for Vector2: x, y)"`（带 valid leaves，AI 友好）。
3. 当前段类型**不在 map**（开放容器 Dictionary/Array/Object，或未纳入校验的类型）→ **停止校验、退回现状** `get_indexed`，零回归。
4. **leaf 合法则降级**：取该 leaf 的值作为下一段 walk 的起点，支持嵌套（如 `transform:origin:x`）；任一段落入开放/未知类型即停。

**关键风险与对策——误杀比静默更坏**：硬编码 leaf 集若**漏列**合法 leaf，会把合法读取误判 1002（回归），比现状静默 null 更坏。对策：
- **只对能 100% 枚举完整的类型 fail-loud**。**PR A 仅纳入 vector 系**——`TYPE_VECTOR2/2I` (x,y)、`TYPE_VECTOR3/3I` (x,y,z)、`TYPE_VECTOR4/4I` (x,y,z,w)，leaf 集铁稳完整、零漏列风险。
- **Color/Rect2/Rect2i/Transform2D/Transform3D/Basis/Plane/Quaternion/AABB 等暂不纳入**：其 get_indexed 合法 leaf 集大/含派生访问器（如 Color 的 r/g/b/a + r8/g8/b8/a8/h/s/v…），完整集需在 Godot 内实证枚举，无法此刻拍胸脯，纳入即冒误杀风险。作为 **follow-up**（PR A 收尾按分诊决定开 issue）：要求「先用 GUT 实证该类型所有合法 leaf 全过 + typo 被拒、确认枚举完整后才加进 map」。walk 机制设计成通用，未来加类型只是往 `_CLOSED_LEAVES` map 加一行 + 配套 GUT。
- 不在 map 的类型（含上面暂缓的、以及 Dictionary/Array/Object 开放容器）一律走现状 `get_indexed` 路径——保证「最坏退回到今天的行为」，永不引入新的误杀。

**契约影响**：无新错误码（复用 1002）、无新退出码；`exists`/`visible` 等布尔语义不变。是把一个静默 footgun 改 loud。

**测试**（GUT，`run_gut.py`，需 `GODOT_BIN`）：
- 纳入的每类型：合法 leaf 读取返回正确值（不误杀）；typo leaf → 1002 + message 含 valid 列表。
- 真 null 属性值仍返回 `{"value": null}` 不报错（区分「真 null」与「typo」）。
- 开放容器（Dictionary leaf 不存在）→ 保持现状返回 null 不报错（零回归）。
- 嵌套 sub-path（`transform:origin:x`，若 Transform 纳入）正确 walk。

---

## 跨项约束（落地收尾必查）

- **覆盖率 ≥ 80**（`coverage run -m pytest`，不用 `pytest --cov`）；测试一律 subagent 委托（model sonnet）跑，主会话只收结论。
- **CHANGELOG `[Unreleased]`** 记四条用户可见变更（exit 4 新增、`--port` 后置、`run --time-scale`、sub-path fail-loud）。
- **SKILL.md** 退出码表 + pitfalls（删「sub-path typo 静默 null」与「--port 必须前置」两条，或改写为已修）+ `--port` 位置说明 + `run` 支持 `--time-scale` —— 模板改完渲染版同步重渲染。
- **CLAUDE.md** 退出码表（item 1）+ 易踩坑（sub-path/--port 两条若改了行为，对应 pitfall 更新或删除）。
- 串行 base main，`gh pr merge --auto`（main required check = `ci-ok` 聚合 job，真等绿）。
- 收尾分诊：本 PR 触碰范围内能当场修的当场修；越界发现先记清单，PR 前统一分诊再决定是否开 issue。

## 实施顺序建议（plan 细化）

item 5（最机械）→ item 2（CLI mutex）→ item 1（退出码+三处文档）→ item 3（bridge+GUT，最重）。每项独立 commit，TDD 红→绿→重构→提交。
