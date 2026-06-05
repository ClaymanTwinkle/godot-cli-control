# init 默认覆盖 addon 目录设计：clobber_addon

日期：2026-06-05
状态：已批准（brainstorming 确认）

## 问题

`init` 对 `addons/godot_cli_control/` 的现行为是「已存在则跳过，`--force` 才覆盖」
（`init_cmd.py` `run_init` 的 plugin 复制分支）。这在 CLI 升级场景是个坑：

1. `pip install -U godot-cli-control` 后重跑 `init`，不带 `--force` 会留下旧版
   addon —— Python 侧与 GDScript 侧协议错位，错误还不在 init 时报，而是延迟到
   RPC 行为不对时才暴露
2. 与 SKILL.md 的默认行为不一致：SKILL.md 默认 clobber（spec §4，理由正是
   「跟随版本与 CLI 帮助自动同步」），同一个同步理由对 addon 只会更强
3. 项目哲学本就不鼓励用户改 addon 目录（定制走 `method_blacklist_extra`
   ProjectSettings 增量路径），「保护用户对 addon 的改动」保护的是反模式

## 目标

1. `init` 默认覆盖已存在的 `addons/godot_cli_control/`（rmtree + copytree，
   与现 `--force` 路径同实现）
2. 保留逃生口：新增 `--keep-addon`，已存在则跳过插件复制（真改过 addon 的人用）
3. `--force` 保留为兼容 no-op，旧脚本不破

## 已决策的设计选择

| 决策点 | 选择 | 备注 |
|---|---|---|
| 默认行为 | 覆盖（rmtree+copy） | 与 SKILL.md clobber 同源的版本同步理由 |
| `--force` 处置 | 保留为 no-op | help 注明「现已是默认行为」；删掉会让旧脚本 exit 64 |
| 逃生口命名 | `--keep-addon` | 「保留」语义直白；不复用 `--skills-no-clobber` 的 no-clobber 命名是因为 keep 更短且 addon 只有一个目标（skills 有两条路径才需要 clobber/补缺语义） |
| `--force` × `--keep-addon` | mutually_exclusive_group | 同时传走 argparse 用法错既有路径（exit 64 / `-1003`） |
| `--skills-only` × `--keep-addon` | 接受并忽略 | 与今天 `--force` × `--skills-only` 同等对待；插件复制本就被跳过 |
| `run_init` 参数 | `force: bool = False` 改名 `clobber_addon: bool = True` | 与 `clobber_skills` 对称；调用方仅 `cli.cmd_init` 与测试，无对外 API 承诺 |

## 组件设计

### 1. CLI（`cli.py`）

- `--force`：保留 `store_true`，help 改为「覆盖已存在的 addons/godot_cli_control
  （现已是默认行为，本 flag 仅为兼容保留）」
- 新增 `--keep-addon`：`store_true`，help 说明「已存在 addons/godot_cli_control
  时跳过插件复制（保留本地版本，不随 CLI 升级刷新）」
- 二者放进 `add_mutually_exclusive_group()`
- `cmd_init`：`clobber_addon=not ns.keep_addon` 传给 `run_init`；不再有任何
  `if ns.force` 分支（单传 `--force` 时 `keep_addon=False` → 覆盖，行为本就
  与其意图一致；mutex 保证两 flag 不并存）

### 2. `init_cmd.run_init`

- 签名：`force: bool = False` → `clobber_addon: bool = True`
- plugin 复制分支翻转：
  - `clobber_addon=True`（默认）且已存在 → rmtree + copy，
    `_say(f"覆盖：{plugin_dst}")`，`_record(plugin_copied=True, plugin_overwritten=True)`
  - `clobber_addon=False` 且已存在 → 跳过，
    `_say(f"已存在：{plugin_dst}（--keep-addon 保留，未更新）")`
  - 不存在 → 复制（不变；`plugin_overwritten` 保持 False，与现行为一致）
- JSON 字段 `plugin_copied` / `plugin_overwritten` 名称与语义不变

### 3. 不动的部分

- SKILL.md 写入逻辑（`clobber_skills` / `--skills-no-clobber`）原样
- project.godot patch / godot_bin 检测 / .gitignore 维护原样
- `skills_install.py` 不改（其 docstring 里「`force=True` 是 init 的默认」
  讲的是 skills 路径，本就成立）

## 测试策略

| 场景 | 断言 |
|---|---|
| 已存在 addon + 默认 init | 旧文件（塞个 marker 文件进 addon 目录）被清掉，新物料落位；`plugin_overwritten=True` |
| `--keep-addon` + 已存在 | marker 文件原样保留；`plugin_copied=False` |
| `--force` 仍被接受 | 行为与默认一致，不报错 |
| `--force --keep-addon` | argparse 用法错 → exit 64 |
| 存量测试 | 3 处 `argparse.Namespace(force=False, ...)` 补 `keep_addon=False`（`force` 字段可留可删，`cmd_init` 不再读取） |
| `run_init` API 级 | 直接调用处的 `force=` 关键字改 `clobber_addon=` |

## 文档同步（契约 7）

- argparse help 变 → `{{cli_help}}` 注入自动跟随；仓内 `.claude/skills` 渲染
  副本必须用 `skills_install.render_skill` 重渲染（COLUMNS=80 + Python 3.12，
  过 CI skill-render-drift）
- `python/README.md:28` 硬写了旧语义（"idempotent … Pass `--force` to
  overwrite"）→ 改为「重跑 init 会刷新 addon 与 SKILL.md 到当前 CLI 版本；
  `--keep-addon` 保留本地 addon」；`:99` 的 usage 行同步
- 顶层 `README.md` Agent integration 节的升级建议（"refresh with
  `init --skills-only`"）补一句：重跑完整 `init` 可同时刷新 addon
- SKILL.md 模板 prose 与 addon README 未硬写 `--force` 语义，不用动

## 不做的事（YAGNI）

- 不做「比较版本号 / 内容 hash，只在不同时覆盖」—— rmtree+copy 本就廉价幂等，
  加判断只引入新失败面
- 不做覆盖前自动备份 —— addon 目录在用户 git 仓里，git 即备份
- 不做 `--keep-addon` 下的版本错位检测告警（真需要再说）
