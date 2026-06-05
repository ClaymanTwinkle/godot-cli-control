# init 默认覆盖 addon 目录（clobber_addon）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `init` 默认覆盖 `addons/godot_cli_control/`（rmtree+copy），新增 `--keep-addon` 逃生口，`--force` 降为兼容 no-op。

**Architecture:** 翻转 `init_cmd.run_init` 的 plugin 复制分支默认值（参数 `force: bool = False` 改名 `clobber_addon: bool = True`，与 `clobber_skills` 对称），CLI 侧 `--force` / `--keep-addon` 进 mutually_exclusive_group，`cmd_init` 以 `clobber_addon=not ns.keep_addon` 接线。文档三处同步 + 仓内渲染版 SKILL.md 重渲染（CI skill-render-drift 门禁）。

**Tech Stack:** Python ≥3.10 / argparse / pytest（venv 在仓库根 `.venv`，Python 3.14）；SKILL.md 重渲染必须 Python 3.12 + COLUMNS=80。

**Spec:** `docs/superpowers/specs/2026-06-05-init-clobber-addon-design.md`（已批准，必读）

**执行约定（来自仓库 CLAUDE.md / 用户全局规则）：**
- 跑测试一律委托 subagent（`model: "sonnet"`，**禁 run_in_background**），主会话只收精简结论
- 工作分支：`feat/init-clobber-addon`（已存在，spec commit 在其上）
- 测试命令统一在仓库根执行；e2e 需要 `GODOT_BIN=$HOME/.local/bin/godot`

---

### Task 1: run_init 行为翻转 + CLI 参数面

**Files:**
- Modify: `python/godot_cli_control/init_cmd.py`（签名 ~L48-58、docstring ~L59-77、plugin 分支 ~L126-140）
- Modify: `python/godot_cli_control/cli.py`（`cmd_init` L1801、init parser L2239-2243）
- Test: `python/tests/test_init.py`、`python/tests/test_cli.py`

- [ ] **Step 1: 写失败测试（API 层，test_init.py，加在 `test_run_init_idempotent` 之后）**

```python
def test_run_init_overwrites_existing_addon_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认 clobber_addon=True：已存在的 addon 整目录刷新，旧文件被清掉。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )
    project = _minimal_project(tmp_path)
    assert run_init(project) == 0

    plugin_dst = project / "addons" / "godot_cli_control"
    marker = plugin_dst / "user_local_hack.gd"
    marker.write_text("# 用户本地改动\n")

    result: dict = {}
    assert run_init(project, result=result) == 0
    assert not marker.exists(), "默认应 rmtree+copy，旧文件要被清掉"
    assert (plugin_dst / "plugin.cfg").is_file()
    assert result["plugin_copied"] is True
    assert result["plugin_overwritten"] is True


def test_run_init_keep_addon_preserves_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clobber_addon=False（CLI --keep-addon）：已存在 addon 原样保留。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )
    project = _minimal_project(tmp_path)
    assert run_init(project) == 0

    plugin_dst = project / "addons" / "godot_cli_control"
    marker = plugin_dst / "user_local_hack.gd"
    marker.write_text("# 用户本地改动\n")

    result: dict = {}
    assert run_init(project, clobber_addon=False, result=result) == 0
    assert marker.exists(), "--keep-addon 必须保留本地文件"
    assert result["plugin_copied"] is False
    assert result["plugin_overwritten"] is False
```

- [ ] **Step 2: 写失败测试（CLI 层，test_cli.py，加在 `test_init_subcommand_rejects_both_flags` 之后，模仿其写法）**

```python
def test_init_subcommand_accepts_keep_addon_flag() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--keep-addon"])
    assert ns.keep_addon is True
    assert ns.force is False


def test_init_subcommand_force_still_accepted_as_noop() -> None:
    """--force 降为兼容 no-op：必须仍被 argparse 接受。"""
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--force"])
    assert ns.force is True
    assert ns.keep_addon is False


def test_init_subcommand_rejects_force_with_keep_addon() -> None:
    """--force 与 --keep-addon 互斥（mutually_exclusive_group）。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["init", "--force", "--keep-addon"])


def test_cmd_init_wires_keep_addon_to_clobber_addon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_init 必须把 --keep-addon 翻译成 clobber_addon=False。"""
    import argparse

    from godot_cli_control.cli import OUTPUT_JSON, cmd_init

    captured: dict = {}

    def fake_run_init(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("godot_cli_control.init_cmd.run_init", fake_run_init)
    ns = argparse.Namespace(
        path=None,
        force=False,
        keep_addon=True,
        no_skills=False,
        skills_only=False,
        skills_no_clobber=False,
        no_gitignore=False,
        output_format=OUTPUT_JSON,
    )
    assert cmd_init(ns) == 0
    capsys.readouterr()
    assert captured["clobber_addon"] is False
    assert "force" not in captured
```

注意：`cmd_init` 是 `from .init_cmd import ... run_init` 的函数内 import，monkeypatch 目标必须是 `godot_cli_control.init_cmd.run_init`。

- [ ] **Step 3: 跑新测试确认失败**

Run（subagent）: `cd /Users/kesar/Projects/godot-cli-control && .venv/bin/python -m pytest python/tests/test_init.py python/tests/test_cli.py -q -k "overwrites_existing_addon or keep_addon or force_still"`
Expected: FAIL —— `run_init` 无 `clobber_addon` 参数（TypeError）、parser 无 `--keep-addon`（SystemExit/AttributeError）。

- [ ] **Step 4: 改 `init_cmd.py`**

签名（L48-58）：`force: bool = False` → `clobber_addon: bool = True`：

```python
def run_init(
    project_root: Path,
    clobber_addon: bool = True,
    write_skills: bool = True,
    skills_only: bool = False,
    clobber_skills: bool = True,
    write_gitignore: bool = True,
    *,
    output_format: str = "text",
    result: dict[str, Any] | None = None,
) -> int:
```

docstring 在 `skills_only=True` 段之前补一段（原文没有 force 的说明，新增即可）：

```
    ``clobber_addon=False``（CLI ``--keep-addon``）：已存在
    ``addons/godot_cli_control/`` 时跳过插件复制，保留用户本地版本。
    默认 True：每次 init 都 rmtree+copy 刷新 addon，保证与当前 CLI 版本
    同步 —— 与 ``clobber_skills`` 默认覆盖同源的设计理由（CLI 升级后
    GDScript 侧必须跟上，否则两侧协议错位）。
```

plugin 分支（L128-135）：

```python
        if plugin_dst.exists():
            if clobber_addon:
                shutil.rmtree(plugin_dst)
                _copy_plugin(plugin_src, plugin_dst)
                _say(f"覆盖：{plugin_dst}")
                _record(plugin_copied=True, plugin_overwritten=True)
            else:
                _say(f"已存在：{plugin_dst}（--keep-addon 保留，未更新）")
```

模块 docstring（L1-15）第 2 条「复制插件物料到 ``addons/godot_cli_control/``」改为「复制插件物料到 ``addons/godot_cli_control/``（已存在则默认整目录刷新，``--keep-addon`` 跳过）」。

- [ ] **Step 5: 改 `cli.py`**

`cmd_init`（L1801）：`force=ns.force,` → `clobber_addon=not ns.keep_addon,`
（mutex 保证 `--force` 与 `--keep-addon` 不并存；单传 `--force` 时 `keep_addon=False` → 覆盖，与其意图一致，故 `ns.force` 无需再读。）

init parser（L2239-2243 的 `--force` 定义处）改成 mutex group：

```python
    addon_group = init_p.add_mutually_exclusive_group()
    addon_group.add_argument(
        "--force",
        action="store_true",
        help=(
            "覆盖已存在的 addons/godot_cli_control"
            "（现已是默认行为，本 flag 仅为兼容保留）"
        ),
    )
    addon_group.add_argument(
        "--keep-addon",
        action="store_true",
        help=(
            "已存在 addons/godot_cli_control 时跳过插件复制"
            "（保留本地版本，不随 CLI 升级刷新；默认会覆盖以同步版本）"
        ),
    )
```

- [ ] **Step 6: 更新存量测试**

- 四处 `argparse.Namespace(...)`（以 `force=False,` 为锚）：`python/tests/test_init.py` 约 L427、L459、L645 + `python/tests/test_cli.py` 约 L723（`test_cmd_init_catches_unexpected_exception_into_envelope` 内）。每处在 `force=False,` 后加一行 `keep_addon=False,`（保留 `force` 字段——真 parser 的 namespace 两个属性都有，测试镜像之）。漏掉任何一处会在 Step 7 红：`AttributeError: Namespace object has no attribute 'keep_addon'`。
- `test_run_init_idempotent`（L179-193）：docstring 改为「重复跑 init 不能破坏 project.godot 或抛错（addon 会被默认刷新，project.godot patch 仍幂等）」；L190 注释「# 第二次：应该 noop」改为「# 第二次：addon 整目录刷新（默认 clobber），project.godot 不得重复 patch」。
- 如有其它 `run_init(..., force=...)` 关键字调用（grep 确认），改为 `clobber_addon=`。

- [ ] **Step 7: 跑相关测试确认全绿**

Run（subagent）: `cd /Users/kesar/Projects/godot-cli-control && .venv/bin/python -m pytest python/tests/test_init.py python/tests/test_cli.py -q`
Expected: 全 PASS。

- [ ] **Step 8: Commit**

```bash
git add python/godot_cli_control/init_cmd.py python/godot_cli_control/cli.py python/tests/test_init.py python/tests/test_cli.py
git commit -m "feat(init): 默认覆盖 addons/godot_cli_control，--keep-addon 逃生口

force→clobber_addon（默认 True，与 clobber_skills 对称）；--force 降为
兼容 no-op，与 --keep-addon 互斥。理由与 SKILL.md 默认 clobber 同源：
CLI 升级后重跑 init 必须让 GDScript 侧跟上，否则协议错位。"
```

---

### Task 2: 文档同步 + 仓内 SKILL.md 重渲染

**Files:**
- Modify: `python/README.md:28`、`python/README.md:99`
- Modify: `README.md`（Agent integration 节，~L193-197）
- Regenerate: `.claude/skills/godot-cli-control/SKILL.md`（CI skill-render-drift 门禁）

- [ ] **Step 1: 改 `python/README.md`**

L28 整行替换为：

```markdown
Re-running `init` refreshes both `addons/godot_cli_control/` and the SKILL.md files to match the installed CLI version (the plugin directory is wiped and re-copied; `project.godot` patching stays idempotent). Pass `--keep-addon` to keep an existing `addons/godot_cli_control/` untouched.
```

L99：`godot-cli-control init [--path DIR] [--force]` → `godot-cli-control init [--path DIR] [--keep-addon]`

- [ ] **Step 2: 改顶层 `README.md`**

L196-197 的 `init --skills-only` 代码块之后、L199「If you've hand-edited」之前，插入一段：

```markdown
Re-running plain `godot-cli-control init` also works and additionally refreshes the bundled `addons/godot_cli_control/` plugin to the new version (pass `--keep-addon` if you've deliberately modified your copy).
```

- [ ] **Step 3: 重渲染仓内 SKILL.md（必须 Python 3.12 + COLUMNS=80，否则 CI skill-render-drift 假红）**

一次性 3.12 venv（已存在则跳过创建）：

```bash
[ -x /tmp/sk312/bin/python ] || (python3.12 -m venv /tmp/sk312 && /tmp/sk312/bin/pip install -q 'websockets>=14,<16')
cd /Users/kesar/Projects/godot-cli-control
COLUMNS=80 PYTHONPATH=python /tmp/sk312/bin/python -c "from godot_cli_control import cli; from godot_cli_control.skills_install import render_skill; from godot_cli_control._version import version; open('.claude/skills/godot-cli-control/SKILL.md','w').write(render_skill(version, cli.format_full_help()))"
```

（此命令已在本机验证可跑通；系统 python3.12 缺 websockets，必须走 /tmp/sk312。）

- [ ] **Step 4: 检查重渲染 diff 只含预期变化**

Run: `git diff --stat .claude/skills/ && git diff .claude/skills/ | head -80`
Expected: 仅 init 小节 help 文本（`--force` 措辞、新增 `--keep-addon`、usage 行 `[--force | --keep-addon]`）+ 末行版本号变化。出现大面积重排 = COLUMNS 或 Python 版本不对，回 Step 3。

- [ ] **Step 5: 渲染健康检查 + skills install 测试**

Run（subagent）: `cd /Users/kesar/Projects/godot-cli-control && .venv/bin/python -c "from godot_cli_control import cli; print(len(cli.format_full_help()))" && .venv/bin/python -m pytest python/tests/test_skills_install.py -q`
Expected: 打印长度无异常；test_skills_install 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add python/README.md README.md .claude/skills/godot-cli-control/SKILL.md
git commit -m "docs(init): README 同步 init 覆盖语义 + 重渲染仓内 SKILL.md"
```

---

### Task 3: 全量验证 + PR

- [ ] **Step 1: 全量测试 + 覆盖率 + lint（subagent，禁 run_in_background）**

```bash
cd /Users/kesar/Projects/godot-cli-control
GODOT_BIN=$HOME/.local/bin/godot .venv/bin/coverage run -m pytest python/tests/ -q
.venv/bin/coverage report | tail -5
.venv/bin/ruff check .
```

Expected: pytest 全 PASS（e2e 必真跑，GODOT_BIN 已给，「缺 GODOT_BIN」不是合法跳过理由）；coverage ≥ 80%（fail_under 门槛）；ruff 0 错。

- [ ] **Step 2: 提交计划文档（若有未提交改动）并推送开 PR**

```bash
git push -u origin feat/init-clobber-addon
```

gh 操作需绕本地代理（`env -u http_proxy -u https_proxy -u all_proxy ...` + 禁沙箱）：

```bash
gh pr create --title "feat(init): 默认覆盖 addon 目录 + --keep-addon 逃生口" --body "<按 spec 摘要，含 spec/plan 链接>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
gh pr merge --auto --squash
```

合并挂上后用 `gh pr view --json autoMergeRequest` 自检非 null（required check = ci-ok）。

- [ ] **Step 3: 收尾**

- 等 CI 绿自动合并后，按仓库惯例在 main 上更新 `CLAUDE.md`「已知遗留 issue」段（注明本次 land）
- 盘点遗留问题，按全局规则开 GitHub issues（如无遗留则明说）
