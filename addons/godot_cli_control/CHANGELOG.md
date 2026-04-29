# Changelog

## [Unreleased]

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
