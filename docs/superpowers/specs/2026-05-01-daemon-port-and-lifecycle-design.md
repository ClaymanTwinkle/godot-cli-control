# 端口与守护进程生命周期改进设计

- 日期：2026-05-01
- 作者：kesar（与 Claude 协作头脑风暴）
- 状态：草稿，待评审

## 目标

消除 `godot-cli-control daemon` 在多项目并发与异常退出场景下的两个核心痛点：

1. **端口冲突 / 选号麻烦**：默认端口 `9877` 硬编码，多项目并发或被外部进程占用时需要手动 `--port` 错开。
2. **孤儿守护进程**：Python CLI 崩溃 / 终端关闭 / 用户忘记 `daemon stop` 时，Godot 子进程会以孤儿形态长期驻留，且跨项目时缺乏统一观察手段。

## 非目标

- 不替换 TCP 为 Unix Domain Socket（架构改动过大，addon、Windows 兼容均需重做）
- 不实现父进程死亡级联（`PR_SET_PDEATHSIG` 在 macOS 缺失，`kqueue` 方案复杂；以注册表 + idle timeout 兜底已足够）
- 不引入新的第三方依赖（注册表用标准库 `pathlib` + `json` + `hashlib`）
- 不破坏现有 `.cli_control/` 项目级状态文件契约（仍是 source of truth）
- 不改 RPC 协议本身

## 背景

当前实现状况（来自代码探查）：

- 默认端口 `9877` 硬编码于 `python/godot_cli_control/client.py:15`
- `daemon start --port <N>` 由 `python/godot_cli_control/cli.py:952-957` 解析
- 启动前 `_check_port_available(port)` 在 `python/godot_cli_control/daemon.py:399-416` 做 bind 预检（最近的 commit 368d9aa）
- 端口落盘到 `<project>/.cli_control/port`，RPC 命令按 `--port → port file → DEFAULT_PORT` 顺序读取（`cli.py:1304-1308`）
- Godot 端通过 `--game-bridge-port=<N>` CLI flag 接收端口（`addons/godot_cli_control/bridge/game_bridge.gd:244-261`）
- daemon 状态文件全部在 `<project>/.cli_control/` 下（`godot.pid` / `port` / `godot.log` / `last_exit_code` / `movie_path` / `godot_bin`）
- 没有任何全局视角：跨项目 daemon 无法一次性查看或停止
- 没有任何空闲检测：Godot 子进程会一直跑到被外部终止

## 用户决策汇总

头脑风暴中已落锤的三点：

1. **方案 A 改默认端口为 0（自动分配）**：`9877` 仅在用户显式 `--port 9877` 时使用，不再作为默认值兜底
2. **方案 B 加全局注册表 + 跨项目 daemon 子命令**：`daemon ls` 扫描时**自动清理死记录**，不引入额外 `prune` 子命令
3. **方案 C 加 idle-timeout 自动 shutdown**：**默认关闭**，opt-in via `--idle-timeout 30m` 等显式 flag

## 设计

### A. 端口自动分配

**接口契约**

- `daemon start` 的 `--port` argparse 默认值由 `9877` 改为 `0`
- `port=0` 时由 OS 分配空闲端口；`port>0` 时走原有 `_check_port_available` 预检
- 实际使用的端口写入 `<project>/.cli_control/port`，并以 `--game-bridge-port=<actual>` 传给 Godot 子进程
- `client.py` 的 `DEFAULT_PORT = 9877` 保留，仅作 RPC 命令在"既无 `--port` 又无 port file"时的最后兜底（极少触发）

**实现要点**

新函数 `_allocate_port(requested: int) -> int` 替代当前 `_check_port_available`：

```python
def _allocate_port(requested: int) -> int:
    """Return a usable port. requested=0 means OS-allocate."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", requested))
        except OSError as e:
            raise DaemonError(
                f"port {requested} already in use ({e}). "
                "请 stop 旧 daemon 或换 --port"
            )
        return s.getsockname()[1]
```

- 关闭 socket 后到 Godot listen 之间存在极小 race window，但内核短期内不会立即重用同一端口；如果真的撞上，原 Godot 启动失败检测路径仍能兜住
- `daemon.start` 在拿到 `actual_port` 后写 `port_file`，再传给 Popen
- 替换 `daemon.py:138` 的硬编码 `--game-bridge-port={port}` 为 `--game-bridge-port={actual_port}`

**用户感知**

- 默认行为变化：`daemon start` 不再固定占 9877。用户日常通过 CLI 调命令完全无感（CLI 自动读 port file）
- 外部脚本如果硬编码 `connect("127.0.0.1", 9877)`，现在必须改为读 `.cli_control/port`，或显式 `daemon start --port 9877`
- README / SKILL 模板需要更新示例

### B. 全局守护进程注册表

**注册表位置**

`~/.local/state/godot-cli-control/daemons/`（XDG state 风格，跨平台用 `Path.home() / ".local/state/..."` 即可，无需 platformdirs）

**记录格式**

每个守护进程一份 `<project_hash>.json`，其中 `project_hash = hashlib.sha1(str(project_root.resolve()).encode()).hexdigest()[:12]`：

```json
{
  "project_root": "/Users/kesar/Projects/foo-game",
  "pid": 12345,
  "port": 54321,
  "started_at": "2026-05-01T10:30:00+08:00",
  "godot_bin": "/Applications/Godot.app/Contents/MacOS/Godot",
  "log_path": "/Users/kesar/Projects/foo-game/.cli_control/godot.log"
}
```

**生命周期**

- `daemon.start()` 在 Popen 成功并通过端口探测后写入注册表
- `daemon.stop()` 成功后删除对应记录
- `daemon ls` 扫描所有 json，对每条记录用 `os.kill(pid, 0)` 探活：
  - 活：列入输出
  - 死：**直接删除**注册表文件 + 对应项目的 `.cli_control/godot.pid` 与 `.cli_control/port`（与现有 `_cleanup_state` 行为一致）
- 不需要文件锁，注册表路径以 project_hash 区分，写入是幂等的

**新 CLI 子命令**

- `godot-cli-control daemon ls`
  - 默认输出 table：项目路径 / PID / 端口 / 启动时长
  - `--json` 输出机器可读
  - 自动清理死记录（清完才输出）
- `godot-cli-control daemon stop --all`
  - 对注册表里所有活记录依次 `_terminate(pid)`，逐条报告结果
  - exit code 取所有结果中最差的一个（任一失败 → 非 0）
- `godot-cli-control daemon stop --project <path>`
  - 等价于 `cd <path> && daemon stop`，但不要求 cwd 切换；对该项目的注册表项 + .cli_control 状态做完整清理

**新模块**

`python/godot_cli_control/registry.py`，纯函数 + 标准库实现，约 80 行：

- `registry_dir() -> Path`
- `project_hash(project_root: Path) -> str`
- `register(project_root, pid, port, godot_bin, log_path) -> None`
- `unregister(project_root) -> None`
- `list_all() -> list[DaemonRecord]`（探活 + 自动清理死记录）
- `stop_record(record) -> tuple[ok, msg]`

### C. 空闲自动 shutdown（opt-in）

**接口契约**

- `daemon start` 新增 `--idle-timeout <duration>` flag
- duration 解析支持 `30m` / `1h` / `90s` / `0`（0 = 永不超时，等价于不传 flag）
- 默认不传 → Godot 端不启用看门狗，行为与今天完全一致

**实现位置**

看门狗在 **Godot 端** 实现，不在 Python 侧。原因：daemon 启动后 Python CLI 即退出，常驻进程是 Godot 本身。

**Godot 端改动（`addons/godot_cli_control/bridge/game_bridge.gd`）**

- `_parse_args()` 增加 `--game-bridge-idle-timeout=<secs>` 解析（解析失败或缺省 → 0 = 关闭）
- bridge 内维护 `_last_activity_ms: int`，初始为启动时刻
- 每次 `_handle_command` 入口处更新 `_last_activity_ms = Time.get_ticks_msec()`
- `_ready` 时若 `_idle_timeout_secs > 0`，启动一个 1 秒 Timer：
  - `_check_idle()`: 如果 `(now - _last_activity_ms) / 1000 > _idle_timeout_secs`，调用 `get_tree().quit()`
- Godot 退出后，Python 端无主动通知；下次 `daemon ls` 探活会清理孤儿注册表项 + 项目状态文件

**Python 端改动（`python/godot_cli_control/daemon.py`）**

- `daemon.start` 接受 `idle_timeout: int | None = None`
- CLI 解析 `--idle-timeout` flag → 调用 `_parse_duration("30m") -> 1800` 转秒
- 传给 Popen：`--game-bridge-idle-timeout={secs}`（仅当 > 0 时附加）
- 注册表记录中可选记 `idle_timeout` 字段（便于 `daemon ls` 输出列）

## 实施步骤（建议顺序）

1. **方案 A**：改默认端口
   - `cli.py` 中 `--port` argparse default → 0
   - `daemon.py` 引入 `_allocate_port`，替换 `_check_port_available` 调用点
   - 改 Popen 传参用 `actual_port`
   - 单测：`port=0` 时 returned port 落盘正确；`port=9877` 占用时仍立即失败
2. **方案 B**：全局注册表
   - 新建 `registry.py` + 单测
   - `daemon.start` 末尾 `register(...)`、`daemon.stop` 末尾 `unregister(...)`
   - CLI 加 `daemon ls`、`daemon stop --all`、`daemon stop --project`
   - 集成测：双项目 start → ls 显示两条 → kill -9 一个 → ls 自动清理
3. **方案 C**：idle timeout
   - GDScript 端先加：解析 flag、记录 last_activity、Timer 检查、quit
   - Python 端 `--idle-timeout` flag + duration parser + 传 Popen 参
   - 集成测：`--idle-timeout 5s` 启动后空闲 8s，进程自然退出，`daemon ls` 自动清理
4. **文档同步**
   - 更新 `README.md` 中 `daemon start` 示例与 port file 说明
   - 更新 `addons/godot_cli_control/SKILL.md` 与 init 落盘的 SKILL 模板，强调"默认 port=auto，请通过 .cli_control/port 读取"
   - 在 `docs/` 加一段 daemon ls 的使用说明

## 关键文件

| 文件 | 改动概要 |
|---|---|
| `python/godot_cli_control/cli.py` | `--port` 默认 0；新增 `daemon ls` / `daemon stop --all` / `daemon stop --project` / `--idle-timeout` |
| `python/godot_cli_control/daemon.py` | `_allocate_port` 替换 `_check_port_available`；`start`/`stop` 调用 registry；接受 `idle_timeout` |
| `python/godot_cli_control/registry.py` | 新文件，全局注册表读写 + 探活清理 |
| `python/godot_cli_control/client.py` | `DEFAULT_PORT` 注释明确：仅作 RPC 兜底 |
| `addons/godot_cli_control/bridge/game_bridge.gd` | 解析 `--game-bridge-idle-timeout`，加 Timer + `_last_activity_ms` |
| `README.md` | 更新示例 |
| `addons/godot_cli_control/SKILL.md` 与 init 落盘模板 | 同步文档 |

## 兼容性与迁移

- **破坏性变更**：默认端口不再是 9877。受影响人群：在外部脚本（非通过本 CLI）中硬编码连 9877 的用户。
  - 迁移方案 1：脚本改读 `<project>/.cli_control/port`
  - 迁移方案 2：`daemon start --port 9877` 显式指定，恢复旧行为
  - README 在变更日志区域显著标注
- 注册表目录是新增的，不影响旧版本
- idle timeout 默认关，对现有用户零感知

## 验证

实施完成后端到端验证：

1. **端口自动分配**
   - `daemon start`（不带 --port），观察 `.cli_control/port` 写入的不是 9877 而是一个高位端口
   - `cat .cli_control/port` → `screenshot test.png` 能正常工作（CLI 自动读 port file）
   - 第二个项目并发 `daemon start` 不冲突
   - `daemon start --port 9877` + 立即再次 `daemon start --port 9877` → 后者立即报"already in use"
2. **全局注册表**
   - 在两个不同项目分别 `daemon start`
   - 任意 cwd 下 `godot-cli-control daemon ls` → 显示两行，含项目路径 + PID + 端口 + 已运行时长
   - 手动 `kill -9 <pid>` 一个，再 `daemon ls` → 死记录被自动清理，对应项目的 `.cli_control/godot.pid` 也被清
   - `daemon stop --all` → 全部停止，`daemon ls` 输出空
   - `daemon stop --project /path/to/foo` → 只停 foo 项目
3. **idle timeout**
   - `daemon start --idle-timeout 5s` 启动
   - 等待 8 秒不发任何命令 → `pgrep godot` 应找不到该进程
   - `daemon ls` 自动清理，输出无该项
   - 对照组：`daemon start`（无 idle 参数）等待 8 秒进程仍在
4. **回归**
   - 现有所有测试通过
   - `daemon stop` / `daemon status` 在新注册表下仍正确

## 后续 issue（实施收尾时开）

按用户全局规则（CLAUDE.md 中"实施收尾必开 issue"），实施 PR 合并前盘点：

- 是否要给 `daemon ls` 加 `--all-projects` vs `--mine` 区分（如果将来扩到多用户场景）
- Windows 上的注册表路径合规性（当前 `~/.local/state/...` 在 Windows 不是惯例，但项目目前似乎不主打 Windows）
- 是否给 idle timeout 加一个项目级 config 默认值（避免每次手敲 flag）
