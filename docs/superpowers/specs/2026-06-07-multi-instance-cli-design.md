# 同项目多实例 CLI 控制（命名实例）设计

日期：2026-06-07
状态：已与用户对齐（场景=同一项目多实例；范围=CLI 优先，pytest fixture 后续开 issue；布局=全部迁入 instances/）

## 背景 / 动机

典型场景是联机游戏 e2e：同一个 Godot 项目要同时跑一个 server 实例 + N 个 client
实例，CLI（以及背后的 AI agent）需要能分别控制每一个——给 client1 点按钮、读
server 的玩家数。

当前架构在「多项目并行」上已经完备（端口 OS 自动分配 + 全局注册表 + `daemon ls`），
但**同一项目同时只能有一个受控实例**，卡在四处按项目路径单键的设计：

| 锚点 | 现状 | 位置 |
|---|---|---|
| 端口发现 | `<project>/.cli_control/port` 单文件 | `daemon.py` `discover_port()` |
| 进程状态 | `godot.pid` / `godot.log` / `movie_path` 项目级单文件 | `daemon.py` `Daemon.__init__` |
| 全局注册表 | `<project_hash>.json` 一项目一条 | `registry.py` `DaemonRecord.record_file` |
| RPC 选目标 | 读 cwd 的 port 文件，无实例概念 | `cli.py` main()「RPC：解析端口」段 |

GDScript 侧（GameBridge）零阻塞：它只认 `--game-bridge-port=N`，每实例各占一个
端口天然隔离。**本设计 GDScript 侧零改动。**

## 目标

1. 同一项目可同时 `daemon start` 多个命名实例，互不踩踏。
2. 每条 RPC 命令可指定目标实例；不指定时语义可预测（见下）。
3. 现有单实例工作流（不传任何新 flag）行为不变。
4. SKILL.md / addon README 同步，agent 看单行 JSON 错误就知道下一步传什么。

## 非目标（收尾开 issue 跟进）

- pytest plugin 多实例 fixture（如 `godot_instances` 工厂，联机 e2e 在单测里抽
  server bridge + client bridge）。
- 一条命令广播多实例（编排层）。
- GDScript 侧任何改动。

## 设计

### 1. 实例命名

- `daemon start --name <name>` 指定实例名；不传 = `default`。
- 实例名校验：`^[A-Za-z0-9_-]{1,32}$`（要落进文件路径与注册表文件名），非法名
  preflight 报 `-1003` + exit 64。
- `daemon start --name x` 而 x 已在跑 → 沿用现「already running」错误，按实例粒度。

### 2. 状态布局（全部迁入 instances/）

```
<project>/.cli_control/
├── config.json                  # 项目级配置，不动
└── instances/<name>/
    ├── port
    ├── godot.pid
    ├── godot.log
    ├── movie_path
    └── last_exit_code
```

- `Daemon.__init__` 加 `instance: str = "default"` 参数，`control_dir` 默认变为
  `<project>/.cli_control/instances/<name>/`。五个状态文件相对路径不变，整体搬家。
  显式传入 `control_dir` 时它优先（现签名已有该参数，保持语义：override 即全权）。
- `_load_config()` 仍读项目级 `.cli_control/config.json`，不动。
- `.gitignore` 已整体忽略 `.cli_control/`，无需变更。

### 3. 端口发现与实例解析

- `discover_port(project_root, instance=None)` 加 `instance` 参数：
  - `instance` 显式给出 → 只读 `instances/<name>/port`。
  - `instance=None` → 实例解析（见下）。
- **实例解析（resolve）规则**——AI 友好的关键，CLI RPC、`GameClient()`、
  `GameBridge()` 三方共用同一入口（沿 issue #91 的单入口原则）。
  解析**以 `project_root` 为根**（默认 `Path.cwd()`，与 `discover_port` 现约定
  一致；`daemon stop --project` 等显式传入时用传入值），不查全局注册表：
  1. 扫 `<project_root>/.cli_control/instances/*/`，按 pid 探活过滤出「在跑」实例。
  2. 恰好 1 个在跑 → 自动选中（无论叫什么名）。
  3. ≥2 个在跑 → 报错，message 列出全部实例名：
     `multiple instances running: client1, server — pass --instance <name>`。
     CLI 层这是 **preflight**（本地 FS 即可判定，先于任何网络往返），
     `-1003` + exit 64。
  4. 0 个在跑 → fallback 读一次 legacy `.cli_control/port`（平滑升级旧 daemon，
     只读不回写），再 fallback `DEFAULT_PORT`，维持现行为。
- **防 legacy 双开**：`default` 实例的 `is_running()` 额外探活一次 legacy
  `.cli_control/godot.pid`（只读）。否则旧版 CLI 启动的 daemon 还活着时，新版
  `daemon start`（默认实例）查新路径以为没在跑，会再起一个。命中 legacy 存活 →
  报「already running」，提示先 `daemon stop`。
- `GameClient` / `GameBridge` 构造函数加可选 `instance: str | None = None`，
  与现有 `port` 参数互斥语义一致（显式 `port` 优先）。

### 4. CLI 表面

```bash
# 启动
godot-cli-control daemon start --name server --headless
godot-cli-control daemon start --name client1

# RPC：顶层 --instance（与现有顶层 --port 同级、argparse 互斥组）
godot-cli-control --instance client1 click /root/Main/JoinButton
godot-cli-control --instance server get-property /root/Net player_count

# 管理：daemon 子命令统一 --name（创建/管理视角），不传走实例解析规则
godot-cli-control daemon status --name server
godot-cli-control daemon logs --name client1
godot-cli-control daemon stop --name server
godot-cli-control daemon stop --all               # 语义不变：全局所有项目所有实例
godot-cli-control daemon stop --all --project .   # 新组合：指定项目的全部实例
godot-cli-control daemon ls                       # 新增 instance 列
```

- 顶层 flag 叫 `--instance`（选择视角）、daemon 子命令叫 `--name`（创建/管理视角），
  SKILL.md 命令表明示两者指同一概念。
- `--instance` 与顶层 `--port` 在 argparse mutually exclusive group 中。
- **`--name` 用独立 helper 注册**（如 `_add_instance_name_flag(p)`），分别挂到
  `start` / `run` / `status` / `logs` / `stop` 五个 subparser。**不要**塞进
  `_add_daemon_flags()`——后者带 `--record` / `--headless` / `--fps` /
  `--idle-timeout` 等 start 专属 flag，且只挂在 `start_p` / `run_p` 上
  （`cli.py:2109,2202`），会污染 status/logs/stop 或漏掉它们。
  `run --name x` 语义：x 未跑则启动、连接、脚本结束停掉自己启动的实例，与现单实例一致。
  不传 `--name` 的 `run` 走同一实例解析规则：恰 1 个在跑 → 连它；≥2 → 歧义报错；
  0 个 → 启动 `default` 实例、跑完停掉。
- `daemon stop` 选靶语义（现 `--all` 与 `--project` 在 mutually exclusive group，
  `cli.py:2126`，**需拆掉该互斥**改为显式组合校验）：
  - 不传任何 flag → 对 cwd 项目走实例解析规则（1 个自动选中；≥2 报歧义错，防误停）。
  - `--name x` → 停 cwd 项目的实例 x。
  - `--project <path>`（无 `--name`）→ 对该项目走同一实例解析规则（≥2 同样报歧义错）。
  - `--project <path> --name x` → 停该项目实例 x。
  - `--all` → 全局所有项目所有实例（语义不变）。
  - `--all --project <path>` → 该项目全部实例（新组合）。
  - `--all --name` → 用法错（互斥）。

### 5. 全局注册表

- `DaemonRecord` 加 `instance: str` 字段；记录文件名
  `<project_hash>-<instance>.json`。
- `list_all()` 兼容读旧文件名 `<project_hash>.json`（缺 `instance` 字段视为
  `default`），死进程照常探活清理 → 升级零手工迁移，旧记录自然淘汰。
- `_prune()` 按记录的 instance 清理 `instances/<name>/` 下的 pid/port 文件；
  对旧格式记录清理 legacy 平铺路径（现逻辑保留为兼容分支）。
- `register()` / `unregister()` 加 `instance` 参数。
- `cmd_daemon_stop --all` 的逐记录循环（`cli.py:1322` 附近
  `Daemon(Path(r.project_root)).stop()`）必须改为
  `Daemon(Path(r.project_root), instance=r.instance).stop()`——否则 `--all`
  对命名实例会一直打 default 路径、静默停不掉。

### 6. 错误码 / 退出码

不新增错误码。多实例歧义与非法实例名都是用法错：`-1003` + exit 64（契约 #3/#4，
`-1003` 恒等于 64），message 自带下一步指引。其余退出码语义不变。

### 7. 文档同步（契约 #7）

- SKILL.md 模板（`python/godot_cli_control/templates/skill/SKILL.md`）：
  - daemon 命令表加 `--name`；新增「多实例」小节（场景示例：server + client）；
  - common pitfalls 加「多实例在跑时不传 `--instance` 会 64/-1003 报错并列出实例名」。
- addon `README.md` 命令表对齐。
- 仓内 `.claude/skills` 渲染版用 `skills_install.render_skill` 重渲
  （COLUMNS=80 + Python 3.12，CI skill-render-drift 锚定 3.12）。
- CHANGELOG `[Unreleased]` 记用户可见变更。

### 8. 测试

- 单测（pytest，subagent 跑）：
  - `Daemon` 新路径布局、实例名校验；
  - `discover_port` / 实例解析：0/1/N 实例、legacy fallback；
  - 注册表：新文件名、旧文件名兼容读、`_prune` 双格式；
  - 共存场景：legacy 格式记录（在跑）+ 新命名实例同项目并存，`daemon ls` 双格式
    同列；legacy daemon 在跑时 `daemon start`（默认实例）报 already running；
  - CLI preflight：歧义报错信封、`--instance`+`--port` 互斥、非法名；
  - `daemon ls` instance 列、`stop` 各 flag 组合选靶矩阵（含 `--all --name` 用法错）。
- e2e（本机真 Godot，subagent 跑，禁后台）：同项目双实例 start（server/client）
  → 各自 RPC（如 get-property）互不串台 → `--instance` 缺省歧义报错 → 分别 stop。
- 38k parity 矩阵、GUT、screenshot 回归均不涉及，不需要跑。

## 兼容性总结

| 旧物 | 处理 |
|---|---|
| 旧 daemon 写的 `.cli_control/port` | 实例解析 0-命中时只读 fallback，重启 daemon 后自然迁移 |
| 旧 daemon 还在跑时新版 `daemon start` | default 实例 `is_running()` 兼探 legacy pid，报 already running 防双开 |
| 注册表旧文件名 `<hash>.json` | `list_all()` 兼容读，视为 default，死后清理；`register()` 只写新格式 `<hash>-<instance>.json`，文件名不同不碰撞 |
| 不传任何新 flag 的现工作流 | 默认实例 `default`，单实例自动选中，行为不变 |
| 顶层 `--port` 直连 | 保留，与 `--instance` 互斥 |
