# Changelog

## [Unreleased]

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
- SKILL.md 增加 *Error code reference*：服务端 `1001-1004` / JSON-RPC `-32xxx` / 客户端 `-1xxx` 三段含义的总表。
- SKILL.md 增加 `set` / `call` 的安全 blacklist 提示（`queue_free` / `set_script` 等会被 `-32602` 拒绝）。
- README.md *Highlights* 重写"Two client APIs"行为"Shell is canonical"，反映新的 CLI 一等地位。
- `GameBridge` (sync) 补齐 `get_text` / `is_visible` / `combo_cancel` 三个方法，与 async `GameClient` 表面对齐。

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
