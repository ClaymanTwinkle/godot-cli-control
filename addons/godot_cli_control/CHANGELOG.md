# Changelog

## [Unreleased]

### Added
- Initial release as Godot 4 plugin + Python package.
- 21 RPC methods (节点查询 / 操作 / 输入模拟 / 截图 / 等待).
- Three activation modes: `--cli-control` flag, `GODOT_CLI_CONTROL=1` env var, Project Setting (debug-build only).
- Property/method blacklist for security.
- Movie writer / record support via wrapper script.

### Known Limitations
- Headless + Movie Maker is incompatible (Godot upstream issue): record requires GUI mode.
- Default `GODOT_BIN` path is macOS-specific.
- Windows requires WSL or custom PowerShell wrapper.
