# godot-cli-control —— Claude / AI agent 工作指引

## 核心原则：这是一个「AI 友好型 CLI」

本项目的设计目标是**让 AI agent 能仅通过 shell 子命令驱动一个运行中的 Godot 4 项目**——
点节点、读写属性、模拟输入、截图、录像、查场景树、跑断言。

「AI 友好」不是 README 用语，而是贯穿源码的契约。**任何新增 / 修改 / 重构必须先回到这条主线**：

> *如果一个 LLM 只能 shell 出去执行一条命令、并且只能解析单行 JSON，它能否搞清楚下一步该做什么？*

### 必须保住的契约（破了它就破了 AI 友好性）

1. **JSON 信封默认开**
   - 成功 ：`{"ok": true, "result": <data>}` 单行 stdout，exit 由命令语义决定
   - 失败 ：`{"ok": false, "error": {"code": <int>, "message": "..."}}` 单行 stdout
   - 任何异常都必须落进信封，不允许 traceback 漏到 stdout（traceback 走 stderr 给人看）。
   - `--text` / `--no-json` 是 legacy 旁路，不能反过来变默认。
   - 实现锚点：`python/godot_cli_control/cli.py` 的 `_emit_*_payload` / `_run_rpc`。

2. **错误码三段制，不允许撞码**
   - 服务端（GDScript LowLevelApi）：正整数 `1xxx`（业务）+ `-32xxx`（JSON-RPC 标准）
   - 客户端（Python CLI）：`-1xxx`
   - 三段互不重叠，单 `code` 字段无歧义。新增码前先查 `SKILL.md` 错误码表 + `addons/godot_cli_control/bridge/error_codes.gd`（业务码集中常量）。

3. **退出码语义化**
   - 0 = 成功 / 布尔 true / 节点存在 / wait 命中
   - 1 = RPC 错（含 `exists`/`visible`=false、`wait-node` timeout、`daemon status` stopped）
   - 2 = 连接 / IO 错，或 infra 前置失败（daemon 起不来、daemon stop 系统错误；这些带 `-1006`）；`daemon stop` ffmpeg 转码失败也是 2
   - 64 = 用法错：argparse + RPC 子命令的 preflight / 运行期参数解析失败，以及 `run <script>` 脚本路径不存在或缺 `run(bridge)`；统一携带 `-1003`（#82 / #111：`-1003` 恒等于 64）
   - 3 = `daemon stop --all` 部分失败（专用，避免与 2 撞）
   - shell `if godot-cli-control exists /root/Foo; then …` 必须能用。

4. **shell 是 canonical surface**
   - 每个 RPC 都必须有对应 CLI 子命令，参数是位置形式 + JSON 字面量（避免引号嵌套地狱）。
   - `def run(bridge):` 脚本是次选，仅用于"必须保持单连接跨多步"的场景。
   - 新加 RPC 的标准流程：
     1. `client.py` 加 async 方法
     2. `bridge.py` 加同步包装
     3. `cli.py` 加 `RpcSpec` + handler + 文本格式化（`text_formatter`）+ 必要时 `preflight` / `exit_code_from`
     4. `addons/.../low_level_api.gd` 或 `input_simulation_api.gd` 加 RPC handler
     5. 更新 `SKILL.md`（模板在 `python/godot_cli_control/templates/skill/SKILL.md`）+ addon `README.md` 错误码 / 命令表

5. **preflight 优先于网络往返**
   - 用户 / agent 用法错（如 `combo` 没传 steps）必须在连 daemon **之前**报错，
     不能让 agent 干等 30s connection retry 才知道自己参数写错了。
   - 实现锚点：`RpcSpec.preflight` + `cli.py:_preflight_combo`。

6. **不让大 payload 撑爆 agent 上下文**
   - `screenshot` 强制写文件路径，禁止 base64 灌 stdout。
   - `tree` / `children` 默认深度有上限；超大场景要有 truncate 信号。
   - 新加返回大数据的 RPC 要先想清楚 trim 策略。

7. **SKILL.md 是 AI agent 的入口，必须随 CLI 同步**
   - `init` 命令会把 `python/godot_cli_control/templates/skill/SKILL.md` 渲染（注入 `{{cli_help}}` + `{{version}}`）后写到目标 Godot 项目的 `.claude/skills/godot-cli-control/SKILL.md` 与 `.codex/skills/.../SKILL.md`。
   - **改 CLI 子命令、改错误码、改默认行为时，SKILL.md 必须一起改**。错误码表、退出码表、JSON 信封示例、common pitfalls 是 agent 唯一能看到的 ground truth。
   - 改完跑一次 `python -c "from godot_cli_control import cli; print(cli.format_full_help())"` 检查渲染没崩。

8. **localhost-only / blacklist 安全网不能为了"方便"放掉**
   - GameBridge 永远 listen `127.0.0.1`；release build 必须自动 disable。
   - method/property blacklist 是防 RCE 的最后一道，不能让单条新功能的 PR 把它松开。第三方项目要扩需求请走 `godot_cli_control/method_blacklist_extra` ProjectSettings 走"增量"路径。

## Repo layout 速查

```
addons/godot_cli_control/   # Godot 4 GDScript 插件 + GUT 测试
python/godot_cli_control/   # Python CLI / GameClient (async) / GameBridge (sync) / pytest plugin / SKILL.md 模板
python/tests/               # pytest 套件（pytest-asyncio）
docs/                       # 设计文档
release.sh                  # 发版脚本
```

依赖：`websockets>=14,<16`，Python ≥ 3.10。覆盖率门槛 80%（`pyproject.toml [tool.coverage.report] fail_under=80`）。

## 测试 & 覆盖率

- 测试不能用 `pytest --cov` 跑，必须 `coverage run -m pytest`。原因写在 `pyproject.toml` 的注释里：pytest11 entry-point 在 pytest 启动时 import 包，pytest-cov 上得太晚会 miss 掉 import-time 语句。
- 跑测试遵循全局规则：**用 subagent 委托执行（指定 `model: "sonnet"`），主会话只接收精简结论**，避免大量 pytest 输出污染上下文。
- GDScript 那侧的单测走 `./addons/godot_cli_control/tests/run_gut.sh`（bash，Linux/macOS）或跨平台的 `python addons/godot_cli_control/tests/run_gut.py`（CI 用这个，三平台通吃），都需要 `GODOT_BIN`。改其中一个记得对齐另一个。

## 发版 / CI

- 版本由 `hatch-vcs` 从 git tag 派生，写到 `python/godot_cli_control/_version.py`。
- 改 SKILL.md 模板后想验证 `init` 注入正确：跑 `pytest python/tests/test_skills_install.py`。

## 已知遗留 issue（PR 路过时顺手修）

*（截至 2026-06-05，已 land：#96/#97/#99/#100 三连（wait 原语/codec/多属性读）、#111/#92 退出码统一、#108 wait_api 拆分、#110/#112/#107/#113 收尾批、#118/#119 review 快修（PR #120）、#98 场景隔离 scene-reload/scene-change + fresh_scene（PR #121）、#102 时间控制 time-scale/pause/step-frames + 1009（PR #125）、#122/#123/#124 收尾批：bridge fixture 兜底还原 pause/time_scale + fresh_scene 1008 pitfall + GUT wait_frames 替换（PR #126，本批；顺手追平了仓内 .claude/skills 渲染版 SKILL.md——它不会自动刷新，改模板/CLI 后记得用 skills_install.render_skill 重渲染——必须 COLUMNS=80 + Python 3.12（argparse usage 折行随 Python 小版本变，CI skill-render-drift 以 3.12 为准））。#127/#128 CI 门禁批（PR #129）：ci-ok 聚合 job 上线，main required check 锚定它（gh pr merge --auto 现在真等绿）；skill-render-drift 防漂移检查上线。#101 视觉态断言（PR #130）：sprite-info（effective_region/frame_texture）+ screenshot --node 裁剪 + 1010/1011。#103 诊断三件套（PR #131）：errors 结构化查询（Logger 拦截 + seq 游标，需 Godot 4.5+，否则 1012）+ daemon logs --tail + pytest no_push_errors fixture / 失败自动截图。仍 open：#109（KIV）；#88、#18（需 maintainer 操作）——feature backlog 已清空。下次 review 发现新坑请补到这里。）*
