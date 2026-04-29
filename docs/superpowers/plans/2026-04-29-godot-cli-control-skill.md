# godot-cli-control Skill 与 init 集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `godot-cli-control init` 在用户 Godot 项目根同时落盘 Claude Code 与 Codex 两份 SKILL.md（同源单模板 + 占位符渲染），让任何在该项目工作的 AI agent 立即学会这套 CLI 与 Python `GameClient` API。

**Architecture:** 单模板源 `python/godot_cli_control/templates/skill/SKILL.md` 含 `{{version}}` / `{{cli_help}}` 两个占位符。新增纯函数模块 `skills_install.py` 负责渲染与落盘（无依赖、独立可测）。`init_cmd.run_init` 在原 4 步后追加第 5 步调用 `install_skills`；`cli.py` 新增 `--no-skills` / `--skills-only` 互斥选项，并把 `_build_parser` 重命名为 `build_parser` 暴露给 `skills_install`。

**Tech Stack:** Python 3.10+、`importlib.resources`、`argparse`、`pathlib`、pytest、hatchling 打包。

**Spec:** `docs/superpowers/specs/2026-04-29-godot-cli-control-skill-design.md`

---

## File Structure

**Create:**
- `python/godot_cli_control/templates/__init__.py`（空，让 hatch 把 templates 作为包带进 wheel）
- `python/godot_cli_control/templates/skill/__init__.py`（同上）
- `python/godot_cli_control/templates/skill/SKILL.md`（模板，约 250 行）
- `python/godot_cli_control/skills_install.py`（约 50 行纯函数）
- `python/tests/test_skills_install.py`（约 6 个用例）
- `.claude/skills/godot-cli-control/SKILL.md`（在本仓库根，渲染好的版本，给开发本仓库的 agent 用）

**Modify:**
- `python/godot_cli_control/cli.py:380` — `_build_parser` 重命名为 `build_parser`
- `python/godot_cli_control/cli.py:430-450` — `init_p` 加 `--no-skills` / `--skills-only` 互斥组
- `python/godot_cli_control/cli.py:317-323` — `cmd_init` 把新参数传给 `run_init`
- `python/godot_cli_control/cli.py:474` — `main()` 内部 `_build_parser` 调用换成 `build_parser`
- `python/godot_cli_control/init_cmd.py:33-96` — `run_init` 签名加 `install_skills_=True, skills_only=False`，追加第 5 步调用
- `python/tests/test_init.py` — 新增 3 个端到端用例覆盖 init 的 skills 行为
- `python/tests/test_cli.py` — 新增 1 个用例覆盖新参数的 argparse 行为
- `python/pyproject.toml:38-46` — 必要时给 `templates/` 加 force-include
- `README.md:25-46` — One-shot install 段补 skill 说明 + 新增 "Agent integration" 小节
- `python/README.md` — 同步对应说明

**Test:**
- `python/tests/test_skills_install.py`（新建）
- `python/tests/test_init.py`（已有，扩展）
- `python/tests/test_cli.py`（已有，扩展）

---

## Task 1: 重命名 `_build_parser` → `build_parser`

**为什么先做这个：** `skills_install` 渲染时需要拿 CLI `--help` 文本，必须先把 parser 构造函数从私有变公有；这是个零行为改动的纯重构，先单独提交便于后续 task 的 diff 干净。

**Files:**
- Modify: `python/godot_cli_control/cli.py:380`（函数定义）
- Modify: `python/godot_cli_control/cli.py:474`（`main()` 内部调用）

- [ ] **Step 1: 全文件查找所有 `_build_parser` 引用**

Run: `grep -n "_build_parser" python/godot_cli_control/cli.py python/tests/`
Expected output: 只在 `cli.py` 出现两处（定义 + 调用），测试无引用。

- [ ] **Step 2: 重命名函数与调用点**

```python
# cli.py:380
def build_parser() -> argparse.ArgumentParser:  # 原 _build_parser
    ...

# cli.py:474（main 函数内）
def main() -> None:
    parser = build_parser()  # 原 _build_parser()
    ...
```

- [ ] **Step 3: 跑现有测试确保零回归**

Run: `cd python && pytest tests/ -x -q`
Expected: 全绿（与改名前一致）。

- [ ] **Step 4: Commit**

```bash
git add python/godot_cli_control/cli.py
git commit -m "refactor(cli): expose build_parser (was _build_parser) for skill rendering"
```

---

## Task 2: 创建 templates 包结构 + 写 SKILL.md 模板

**为什么先做这个：** `skills_install` 与所有后续测试都要消费这份模板；先把它落盘后，TDD 用例可以直接断言渲染行为。

**Files:**
- Create: `python/godot_cli_control/templates/__init__.py`（空）
- Create: `python/godot_cli_control/templates/skill/__init__.py`（空）
- Create: `python/godot_cli_control/templates/skill/SKILL.md`

- [ ] **Step 1: 建包目录与空 __init__.py**

```bash
mkdir -p python/godot_cli_control/templates/skill
touch python/godot_cli_control/templates/__init__.py
touch python/godot_cli_control/templates/skill/__init__.py
```

- [ ] **Step 2: 写 SKILL.md 模板**

写入 `python/godot_cli_control/templates/skill/SKILL.md`，内容如下（使用单行字符串占位符 `{{version}}` / `{{cli_help}}`，由 `skills_install.render_skill` 替换）：

````markdown
---
name: godot-cli-control
description: Use when driving a Godot 4 game from a script or terminal — clicking buttons, simulating input actions, taking screenshots, dumping the scene tree, or recording demos. Trigger when the user mentions godot-cli-control, the godot-cli-control CLI/daemon, or asks to automate / scrape / black-box-test a Godot scene.
---

# godot-cli-control

WebSocket bridge for headless / scripted control of Godot 4 scenes. A daemon process owns a running Godot instance; clients (the CLI or the Python `GameClient`) send RPC over `ws://127.0.0.1:<port>` to click nodes, simulate input actions, read/write properties, dump the scene tree, take screenshots, and record movies.

## When to use

**Use this when:**
- Black-box testing a Godot UI from outside the engine
- Recording a demo / regression video by scripting input
- Building a bot or smoke test that exercises the running game
- Scraping the live scene tree or a node's text/property

**Don't use this when:**
- You can write a normal GDScript unit test (use GUT inside the project instead)
- You need to reason about source logic — read the `.gd` files directly
- The change is to the Godot project's data layer with no UI involvement

## Quick start

```bash
# Once per Godot project (already done if you can read this file):
godot-cli-control init

# Per session:
godot-cli-control daemon start          # boots Godot in the background
godot-cli-control tree 3                # confirm RPC works
# ... your work ...
godot-cli-control daemon stop
```

## CLI reference

<!-- BEGIN cli_help (auto-generated by godot-cli-control init) -->
```
{{cli_help}}
```
<!-- END cli_help -->

## Python `GameClient` API

`from godot_cli_control.client import GameClient` — async WebSocket client; use as `async with GameClient(port=...) as client:`.

| Method | Purpose |
|---|---|
| `await client.click(path)` | Click a Control/Button node by absolute scene path |
| `await client.get_property(path, prop)` | Read a node property |
| `await client.set_property(path, prop, value)` | Write a node property |
| `await client.call_method(path, method, *args)` | Invoke an arbitrary node method |
| `await client.get_text(path)` | Shortcut for label/button text |
| `await client.node_exists(path)` | Boolean existence check |
| `await client.is_visible(path)` | Visibility check |
| `await client.get_children(path)` | Direct children (one level) |
| `await client.screenshot()` | Returns PNG bytes |
| `await client.get_scene_tree(depth=5)` | Tree dump as nested dict |
| `await client.wait_for_node(path, timeout=5.0)` | Poll until node appears |
| `await client.wait_game_time(seconds)` | Wait N in-game seconds |
| `await client.action_press(action)` | Press an InputMap action (sticky) |
| `await client.action_release(action)` | Release a sticky action |
| `await client.action_tap(action, duration=0.1)` | Press → wait → release |
| `await client.hold(action, duration)` | Hold for N seconds, auto-release |
| `await client.combo(steps)` | Run a JSON `[{press/release/wait}, ...]` sequence |
| `await client.combo_cancel()` | Abort running combo |
| `await client.release_all()` | Release every held action |

## `def run(bridge)` script mode

For multi-step scenarios that don't fit a single CLI call, write a Python script with a `run(bridge)` entry point and invoke it via `godot-cli-control run my_script.py`. The runner auto-starts the daemon (if not already running) and tears it down on exit.

```python
# my_script.py
def run(bridge):
    bridge.click("/root/Main/StartButton")
    bridge.wait_game_time(0.5)
    assert bridge.get_text("/root/Main/Score") == "0"
```

`bridge` is a synchronous wrapper around `GameClient` — same method names, no `await`. Sibling-imports work (the script's directory is on `sys.path`).

## Common pitfalls

- **`ConnectionRefused` on every RPC** — daemon isn't running. Run `godot-cli-control daemon start` first.
- **Node paths must be absolute** — start with `/root/...`. Relative paths return "node not found".
- **`InvalidMessage: did not receive a valid HTTP response`** — `all_proxy` / `http_proxy` env var is hijacking localhost. The client sets `proxy=None` to defend, but if you see weird handshake errors, `unset all_proxy` first.
- **Daemon won't start** — check `.cli_control/godot_bin` exists and points at a real Godot 4 binary, or `export GODOT_BIN=/path/to/godot`.

---

Generated from godot-cli-control v{{version}}. Re-run `godot-cli-control init --skills-only` to refresh.
````

- [ ] **Step 3: 验证占位符位置**

Run: `grep -c "{{version}}" python/godot_cli_control/templates/skill/SKILL.md && grep -c "{{cli_help}}" python/godot_cli_control/templates/skill/SKILL.md`
Expected: `2` 行（version 在 footer 出现一次，但 grep -c 数行；这里实际两处占位符，但都各占一行）。具体结果：`{{version}}` 1 行、`{{cli_help}}` 1 行。允许微调。

- [ ] **Step 4: Commit**

```bash
git add python/godot_cli_control/templates/
git commit -m "feat(skill): add SKILL.md template with version/cli_help placeholders"
```

---

## Task 3: 写 `skills_install.py`（TDD）

**Files:**
- Create: `python/godot_cli_control/skills_install.py`
- Create: `python/tests/test_skills_install.py`

- [ ] **Step 1: 写第一个失败测试 — `render_skill` 替换占位符**

Create `python/tests/test_skills_install.py`：

```python
"""skills_install 单元测试 —— 渲染 + 落盘行为，纯函数零 IO 副作用边界。"""

from __future__ import annotations

from pathlib import Path

import pytest


# ── render_skill ──


def test_render_skill_substitutes_placeholders() -> None:
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="1.2.3", cli_help="USAGE: foo")

    assert "1.2.3" in out
    assert "USAGE: foo" in out
    assert "{{version}}" not in out
    assert "{{cli_help}}" not in out


def test_render_skill_keeps_frontmatter_intact() -> None:
    from godot_cli_control.skills_install import render_skill

    out = render_skill(version="0.0.0", cli_help="x")

    assert out.splitlines()[0] == "---"
    assert "name: godot-cli-control" in out
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd python && pytest tests/test_skills_install.py -x -q`
Expected: ImportError / ModuleNotFoundError on `godot_cli_control.skills_install`。

- [ ] **Step 3: 写最小实现让 render_skill 测试过**

Create `python/godot_cli_control/skills_install.py`：

```python
"""SKILL.md 模板渲染与落盘。

设计要点：
* `render_skill` 是纯函数：入版本号 + CLI help 文本，出渲染好的 markdown 字符串。
  不读环境、不算版本、不调 cli — 调用方注入，便于测试。
* `install_skills` 只关心 IO：把渲染结果写到 `.claude/skills/...` 与 `.codex/skills/...`
  两条相对路径。`force=False` 时跳过已存在的目标，适合"幂等保护用户改动"的语义；
  `force=True` 是 init 的默认（spec 决定）。
* 模板用 `importlib.resources.files` 取，wheel/editable install 都能命中。
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

CLAUDE_REL = Path(".claude/skills/godot-cli-control/SKILL.md")
CODEX_REL = Path(".codex/skills/godot-cli-control/SKILL.md")


def render_skill(version: str, cli_help: str) -> str:
    """读模板 → 占位符替换 → 返回最终文本。纯函数。"""
    template = (
        files("godot_cli_control.templates.skill") / "SKILL.md"
    ).read_text(encoding="utf-8")
    return template.replace("{{version}}", version).replace(
        "{{cli_help}}", cli_help
    )


def install_skills(
    project_root: Path,
    *,
    version: str,
    cli_help: str,
    force: bool = True,
) -> list[Path]:
    """把渲染好的 SKILL.md 写到 Claude / Codex 两条目标路径。

    返回实际写入的绝对路径列表（被 force=False 跳过的不计入）。
    """
    content = render_skill(version, cli_help)
    written: list[Path] = []
    for rel in (CLAUDE_REL, CODEX_REL):
        dst = project_root / rel
        if dst.exists() and not force:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        written.append(dst)
    return written
```

- [ ] **Step 4: 跑测试确认 render_skill 两个用例 pass**

Run: `cd python && pytest tests/test_skills_install.py -x -q`
Expected: 2 passed。

- [ ] **Step 5: 追加 install_skills 的 4 个失败测试**

往 `python/tests/test_skills_install.py` 末尾追加：

```python
# ── install_skills ──


def test_install_skills_writes_both_paths(tmp_path: Path) -> None:
    from godot_cli_control.skills_install import (
        CLAUDE_REL,
        CODEX_REL,
        install_skills,
        render_skill,
    )

    written = install_skills(
        tmp_path, version="9.9.9", cli_help="HELPTEXT_MARKER"
    )

    claude = tmp_path / CLAUDE_REL
    codex = tmp_path / CODEX_REL
    assert claude.exists()
    assert codex.exists()
    expected = render_skill(version="9.9.9", cli_help="HELPTEXT_MARKER")
    assert claude.read_text(encoding="utf-8") == expected
    assert codex.read_text(encoding="utf-8") == expected
    assert set(written) == {claude, codex}


def test_install_skills_creates_parent_dirs(tmp_path: Path) -> None:
    """空 tmp_path 下也能成功 —— 验证 mkdir(parents=True)。"""
    from godot_cli_control.skills_install import install_skills

    # 不预建任何目录
    install_skills(tmp_path, version="0", cli_help="")

    assert (tmp_path / ".claude" / "skills" / "godot-cli-control").is_dir()
    assert (tmp_path / ".codex" / "skills" / "godot-cli-control").is_dir()


def test_install_skills_force_true_overwrites(tmp_path: Path) -> None:
    from godot_cli_control.skills_install import (
        CLAUDE_REL,
        CODEX_REL,
        install_skills,
    )

    # 预先写脏内容
    for rel in (CLAUDE_REL, CODEX_REL):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("DIRTY", encoding="utf-8")

    install_skills(tmp_path, version="1.0", cli_help="x", force=True)

    assert "DIRTY" not in (tmp_path / CLAUDE_REL).read_text(encoding="utf-8")
    assert "DIRTY" not in (tmp_path / CODEX_REL).read_text(encoding="utf-8")


def test_install_skills_force_false_skips_existing(tmp_path: Path) -> None:
    from godot_cli_control.skills_install import (
        CLAUDE_REL,
        CODEX_REL,
        install_skills,
    )

    # 预先写脏内容到 CLAUDE_REL；CODEX_REL 留空
    claude = tmp_path / CLAUDE_REL
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text("KEEP_ME", encoding="utf-8")

    written = install_skills(
        tmp_path, version="1.0", cli_help="x", force=False
    )

    assert claude.read_text(encoding="utf-8") == "KEEP_ME"
    assert (tmp_path / CODEX_REL).exists()
    assert claude not in written
    assert (tmp_path / CODEX_REL) in written
```

- [ ] **Step 6: 跑全部 6 个用例**

Run: `cd python && pytest tests/test_skills_install.py -x -q`
Expected: 6 passed（实现已经在 Step 3 完成，本步只是补测）。

- [ ] **Step 7: Commit**

```bash
git add python/godot_cli_control/skills_install.py python/tests/test_skills_install.py
git commit -m "feat(skill): add skills_install module — render + write Claude/Codex SKILL.md"
```

---

## Task 4: 改 `cli.py` 加 `--no-skills` / `--skills-only` 互斥参数

**Files:**
- Modify: `python/godot_cli_control/cli.py:430-450`（init 子命令定义）
- Modify: `python/godot_cli_control/cli.py:317-323`（`cmd_init` 函数）
- Modify: `python/tests/test_cli.py`（新增 1 个 argparse 用例）

- [ ] **Step 1: 写失败测试**

往 `python/tests/test_cli.py` 末尾追加：

```python
# ── init 子命令的 skill 互斥参数 ──


def test_init_subcommand_accepts_no_skills_flag() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--no-skills"])
    assert ns.cmd == "init"
    assert ns.no_skills is True
    assert ns.skills_only is False


def test_init_subcommand_accepts_skills_only_flag() -> None:
    from godot_cli_control.cli import build_parser

    ns = build_parser().parse_args(["init", "--skills-only"])
    assert ns.no_skills is False
    assert ns.skills_only is True


def test_init_subcommand_rejects_both_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """argparse mutually_exclusive_group 应让两者并存触发 SystemExit。"""
    from godot_cli_control.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["init", "--no-skills", "--skills-only"])
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd python && pytest tests/test_cli.py -x -q -k "no_skills or skills_only or both_flags"`
Expected: AttributeError on `ns.no_skills` 或 `unrecognized arguments`。

- [ ] **Step 3: 改 cli.py 加互斥组**

定位 `cli.py:430` 附近 `init_p` 定义块，在 `--force` 之后追加：

```python
    init_p.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 addons/godot_cli_control",
    )
    # ↓↓↓ 新增 ↓↓↓
    skills_group = init_p.add_mutually_exclusive_group()
    skills_group.add_argument(
        "--no-skills",
        action="store_true",
        help="跳过 .claude/.codex skill 写入",
    )
    skills_group.add_argument(
        "--skills-only",
        action="store_true",
        help="只写 skill 文件，跳过插件复制 / project.godot patch / godot_bin 检测",
    )
```

- [ ] **Step 4: 改 `cmd_init` 把新参数透传给 `run_init`**

定位 `cli.py:317` 附近 `cmd_init`，改为：

```python
def cmd_init(ns: argparse.Namespace) -> int:
    from .init_cmd import run_init

    return run_init(
        # 保留 .resolve()：run_init 内部用 relative_to(project_root) 打印 skill
        # 路径，相对路径会让 relative_to 在 cwd 不寻常时抛 ValueError。
        project_root=(Path(ns.path).resolve() if ns.path else Path.cwd()),
        force=ns.force,
        install_skills_=not ns.no_skills,
        skills_only=ns.skills_only,
    )
```

- [ ] **Step 5: 跑测试确认 pass**

Run: `cd python && pytest tests/test_cli.py -x -q`
Expected: 全绿（含 3 个新增用例 + 已有 3 个原 `_exec_user_script` 用例）。

注意：本步仅改 argparse 与函数签名转发；`run_init` 还没接受新关键字会引发其他 init 测试失败 —— 这正是 Task 5 要修的，先把改动留在 cli 侧。

- [ ] **Step 6: Commit（先不动 init_cmd，下一个 task 配合）**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py
git commit -m "feat(cli): add mutually exclusive --no-skills / --skills-only to init"
```

---

## Task 5: 改 `init_cmd.run_init` 接入 `install_skills`

**Files:**
- Modify: `python/godot_cli_control/init_cmd.py:33-96`（`run_init` 签名 + 末尾追加）
- Modify: `python/tests/test_init.py`（新增 3 个用例）

- [ ] **Step 1: 写 3 个失败测试**

先看 `python/tests/test_init.py` 末尾结构（找一个能造最小 Godot 项目的 fixture 或 helper；如果没有就内联 setup）。然后追加：

```python
# ── init 与 skills 的集成 ──


def _make_min_godot_project(tmp_path: Path) -> Path:
    """造一个最小 project.godot —— 满足 run_init 的入口校验。"""
    (tmp_path / "project.godot").write_text(
        "config_version=5\n\n[application]\nconfig/name=\"x\"\n",
        encoding="utf-8",
    )
    return tmp_path


def test_init_writes_both_skills_by_default(tmp_path: Path) -> None:
    """默认 install_skills_=True 时两条 SKILL.md 都生成。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    rc = run_init(proj)

    assert rc == 0
    assert (proj / CLAUDE_REL).is_file()
    assert (proj / CODEX_REL).is_file()


def test_init_no_skills_skips_skill_files(tmp_path: Path) -> None:
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    rc = run_init(proj, install_skills_=False)

    assert rc == 0
    assert not (proj / CLAUDE_REL).exists()
    assert not (proj / CODEX_REL).exists()


def test_init_skills_only_skips_plugin_and_patch(tmp_path: Path) -> None:
    """skills_only=True：addons/ 不被建立、project.godot 不被改动、SKILL.md 写入。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    original = (proj / "project.godot").read_bytes()

    rc = run_init(proj, skills_only=True)

    assert rc == 0
    assert not (proj / "addons" / "godot_cli_control").exists()
    assert (proj / "project.godot").read_bytes() == original
    assert (proj / CLAUDE_REL).is_file()
    assert (proj / CODEX_REL).is_file()
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd python && pytest tests/test_init.py -x -q -k "init_writes_both_skills or init_no_skills or init_skills_only"`
Expected: 3 个新用例失败（`run_init` 不接受新 kwargs，或 SKILL.md 没写）。

- [ ] **Step 3: 改 `run_init` 签名 + 追加第 5 步**

把 `init_cmd.py:33` 的 `run_init` 改为：

```python
def run_init(
    project_root: Path,
    force: bool = False,
    install_skills_: bool = True,
    skills_only: bool = False,
) -> int:
    """实施接入流程。返回进程 exit code。

    ``skills_only=True``：跳过 1-4 步（插件复制 / project.godot patch /
    godot_bin 检测），只写 SKILL.md —— 用于 CLI 升级后单独刷新 skill。
    ``install_skills_=False``：跳过第 5 步，给已自定义 skill 的用户留逃生口。
    两者由 cli.py 侧的 mutually_exclusive_group 保证不会同时为真。
    """
    if not (project_root / "project.godot").is_file():
        print(
            f"错误：{project_root} 下没有 project.godot —— 不像 Godot 项目根。\n"
            "如果你确实在 Godot 项目内，请用 --path 指向项目根。",
            file=sys.stderr,
        )
        return 1

    if not skills_only:
        # 原 1-4 步整体保持不动，仅缩进到此 if 块下
        plugin_src = locate_plugin_source()
        if plugin_src is None:
            print(
                "错误：找不到插件源（addons/godot_cli_control/）。\n"
                "如果是从源码 editable install，请确保仓库布局完整；\n"
                "如果是从 wheel 安装，包资源可能损坏，请重装。",
                file=sys.stderr,
            )
            return 1

        addons_dir = project_root / ADDONS_DIRNAME
        plugin_dst = addons_dir / PLUGIN_DIR_NAME
        if plugin_dst.exists():
            if force:
                shutil.rmtree(plugin_dst)
                _copy_plugin(plugin_src, plugin_dst)
                print(f"覆盖：{plugin_dst}")
            else:
                print(f"已存在：{plugin_dst}（用 --force 覆盖）")
        else:
            addons_dir.mkdir(parents=True, exist_ok=True)
            _copy_plugin(plugin_src, plugin_dst)
            print(f"复制：{plugin_src} → {plugin_dst}")

        patched, changes = _patch_project_godot(project_root / "project.godot")
        if changes:
            print(f"修改 project.godot：{', '.join(changes)}")
        elif patched:
            print("project.godot 已配置好（未改动）")

        godot_bin = find_godot_binary()
        if godot_bin:
            control_dir = project_root / ".cli_control"
            control_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(control_dir, 0o700)
            except OSError:
                pass
            (control_dir / "godot_bin").write_text(godot_bin + "\n")
            print(f"检测到 Godot：{godot_bin}（已写入 .cli_control/godot_bin）")
        else:
            print(
                "警告：未自动检测到 Godot 二进制。\n"
                "请 `export GODOT_BIN=/path/to/godot` 或写到 "
                ".cli_control/godot_bin。",
                file=sys.stderr,
            )

    if install_skills_:
        from . import _version, cli, skills_install

        cli_help = cli.build_parser().format_help()
        version = getattr(_version, "__version__", "unknown")
        written = skills_install.install_skills(
            project_root,
            version=version,
            cli_help=cli_help,
            force=True,  # init 默认即覆盖（spec §4 决定）
        )
        for p in written:
            print(f"写入 skill：{p.relative_to(project_root)}")

    print()
    print("已就绪。下一步：")
    print("  godot-cli-control daemon start          # 启动 daemon")
    print("  godot-cli-control tree 3                # 验证 RPC 通了")
    print("  godot-cli-control daemon stop           # 停止")
    return 0
```

- [ ] **Step 4: 跑全部 init 测试**

Run: `cd python && pytest tests/test_init.py -x -q`
Expected: 全绿（已有用例不退化 + 3 个新用例 pass）。

- [ ] **Step 5: 跑全测保险**

Run: `cd python && pytest tests/ -x -q`
Expected: 全绿（含 test_skills_install / test_cli / test_init / test_daemon / test_client）。

- [ ] **Step 6: Commit**

```bash
git add python/godot_cli_control/init_cmd.py python/tests/test_init.py
git commit -m "feat(init): write Claude+Codex SKILL.md via skills_install (default on, --no-skills opts out)"
```

---

## Task 6: 打包 — 验证 templates 进 wheel + 必要时加 force-include

**Files:**
- Modify: `python/pyproject.toml:38-46`（按需）

- [ ] **Step 1: 本地打 wheel 检查 templates 是否被带上**

```bash
cd python && python -m build --wheel --no-isolation 2>/dev/null || python -m build --wheel
```

如果 `python -m build` 缺，先 `pip install build`。生成的 wheel 在 `python/dist/`。

- [ ] **Step 2: unzip wheel 看 templates 是否就位**

```bash
cd python && unzip -l dist/godot_cli_control-*.whl | grep -E "templates|SKILL.md"
```

Expected: 看到至少 `godot_cli_control/templates/skill/SKILL.md`。

- [ ] **Step 3: 如果 Step 2 没看到 SKILL.md，加 force-include**

编辑 `python/pyproject.toml`，在已有的 `[tool.hatch.build.targets.wheel.force-include]` 段增加：

```toml
[tool.hatch.build.targets.wheel.force-include]
"../addons/godot_cli_control" = "godot_cli_control/_plugin"
"godot_cli_control/templates" = "godot_cli_control/templates"  # 新增

[tool.hatch.build.targets.sdist.force-include]
"../addons/godot_cli_control" = "godot_cli_control/_plugin"
"godot_cli_control/templates" = "godot_cli_control/templates"  # 新增
```

重打包重验。

- [ ] **Step 4: 如果 Step 2 已通过则跳过 Step 3，不动 pyproject**

- [ ] **Step 5: 清理 dist 后 commit（如有 pyproject 改动）**

```bash
rm -rf python/dist python/build
# 仅当 Step 3 改了 pyproject 时：
git add python/pyproject.toml
git commit -m "build: force-include templates/ in wheel + sdist"
```

如未动 pyproject，本 task 无 commit，跳到 Task 7。

---

## Task 7: 手动渲染并提交本仓库根的 `.claude/skills/godot-cli-control/SKILL.md`

**为什么：** 本仓库的开发者（与 agent）也要能看到 skill。`init` 不会跑在本仓库（这里不是 Godot 项目），所以一次性手动渲染并 commit。

**Files:**
- Create: `.claude/skills/godot-cli-control/SKILL.md`（仓库根）

- [ ] **Step 1: 用 Python 一次性渲染**

```bash
cd python && python -c "
from pathlib import Path
from godot_cli_control import skills_install, cli, _version
content = skills_install.render_skill(
    version=getattr(_version, '__version__', 'unknown'),
    cli_help=cli.build_parser().format_help(),
)
out = Path('..') / '.claude' / 'skills' / 'godot-cli-control' / 'SKILL.md'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(content, encoding='utf-8')
print(f'wrote {out}')
"
```

- [ ] **Step 2: 验证生成结果**

```bash
head -5 .claude/skills/godot-cli-control/SKILL.md
grep -c "{{version}}\|{{cli_help}}" .claude/skills/godot-cli-control/SKILL.md
```

Expected: 首行 `---`、第二行 `name: godot-cli-control`；占位符计数 0。

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/godot-cli-control/SKILL.md
git commit -m "docs(skill): commit rendered SKILL.md for in-repo agents"
```

---

## Task 8: 文档 — README 与 python/README

**Files:**
- Modify: `README.md:25-46`（One-shot install 段 + 新增 Agent integration 小节）
- Modify: `python/README.md`（同步对应说明）

- [ ] **Step 1: 改 README.md 顶层**

在 `README.md` "One-shot install" 段的 `init` 步骤说明列表里追加一行；并在 "Manual install" 之前插入新小节。

定位 `README.md:42-46` 的列表（"`init` does the manual steps for you:"），在末尾追加：

```markdown
- writes `.claude/skills/godot-cli-control/SKILL.md` and `.codex/skills/godot-cli-control/SKILL.md` so any AI agent (Claude Code / Codex) working in your Godot project knows this CLI exists. Pass `--no-skills` to skip; `--skills-only` to just refresh the skill files after upgrading the CLI.
```

并在 `## Manual install (advanced)` 之前插入：

```markdown
## Agent integration

When you run `godot-cli-control init`, two `SKILL.md` files are dropped under your Godot project root:

- `.claude/skills/godot-cli-control/SKILL.md` (Claude Code)
- `.codex/skills/godot-cli-control/SKILL.md` (Codex)

Both are rendered from the same template and pin the current CLI version + `--help` output, so an agent loaded into your project can immediately see the full command surface, the `GameClient` API, and the `def run(bridge)` script convention.

After a CLI upgrade (`pipx upgrade godot-cli-control`), refresh the files with:

```bash
godot-cli-control init --skills-only
```

If you've hand-edited a `SKILL.md` and don't want it overwritten, use `godot-cli-control init --no-skills` going forward.
```

- [ ] **Step 2: 改 python/README.md 加同样的小节（更短版本）**

定位 `python/README.md` 中 `init` 描述附近，加一段 3-5 行说明 skill 写入与 `--no-skills` / `--skills-only` 选项；与顶层 README 不重复全文，链回顶层 README 的 Agent integration 小节即可。

具体内容由实施者按 README 现有风格调整；保持简洁。

- [ ] **Step 3: 跑全测确认无回归**

Run: `cd python && pytest tests/ -x -q`
Expected: 全绿。

- [ ] **Step 4: Commit**

```bash
git add README.md python/README.md
git commit -m "docs: explain Claude/Codex skill integration in init flow"
```

---

## Task 9: 端到端手动验收

**Files:** 无改动；纯验证。

- [ ] **Step 1: editable install 重装**

```bash
cd python && pip install -e .
```

- [ ] **Step 2: 在临时目录跑 init**

```bash
mkdir -p /tmp/test-godot-cli-control && cd /tmp/test-godot-cli-control
echo 'config_version=5' > project.godot
godot-cli-control init
```

Expected stdout 含：
- `复制：...` 或 `已存在：...`
- `修改 project.godot：autoload/GameBridgeNode, editor_plugins/enabled` 或 `project.godot 已配置好（未改动）`
- `写入 skill：.claude/skills/godot-cli-control/SKILL.md`
- `写入 skill：.codex/skills/godot-cli-control/SKILL.md`

- [ ] **Step 3: 检查文件**

```bash
head -3 /tmp/test-godot-cli-control/.claude/skills/godot-cli-control/SKILL.md
head -3 /tmp/test-godot-cli-control/.codex/skills/godot-cli-control/SKILL.md
diff /tmp/test-godot-cli-control/.claude/skills/godot-cli-control/SKILL.md /tmp/test-godot-cli-control/.codex/skills/godot-cli-control/SKILL.md
grep -c "0\." /tmp/test-godot-cli-control/.claude/skills/godot-cli-control/SKILL.md  # 版本号能被找到
```

Expected: 两文件首行 `---`，diff 无输出（内容完全相同），含当前版本号片段。

- [ ] **Step 4: 测 `--no-skills`**

```bash
rm -rf /tmp/test-godot-cli-control/.claude /tmp/test-godot-cli-control/.codex
godot-cli-control init --no-skills
ls /tmp/test-godot-cli-control/.claude /tmp/test-godot-cli-control/.codex 2>&1
```

Expected: ls 两条都报 No such file。

- [ ] **Step 5: 测 `--skills-only`**

```bash
rm -rf /tmp/test-godot-cli-control/addons
echo 'config_version=5' > /tmp/test-godot-cli-control/project.godot   # 重置
godot-cli-control init --skills-only
ls /tmp/test-godot-cli-control/addons 2>&1   # 应不存在
cat /tmp/test-godot-cli-control/project.godot   # 应只有 config_version=5
ls /tmp/test-godot-cli-control/.claude/skills/godot-cli-control/SKILL.md   # 应存在
```

Expected: addons 不存在；project.godot 没被 patch；SKILL.md 仍写入。

- [ ] **Step 6: 测互斥**

```bash
godot-cli-control init --no-skills --skills-only
```

Expected: argparse 报 `not allowed with argument`，退出码非 0。

- [ ] **Step 7: 全测最终验证**

```bash
cd /Users/kesar/Projects/godot-cli-control/python && pytest tests/ -v
```

Expected: 全绿；新加的 6 + 3 + 3 = 12 个用例都列出。

- [ ] **Step 8: 清理**

```bash
rm -rf /tmp/test-godot-cli-control
```

---

## Task 10: 实施收尾 — 按 CLAUDE.md §7 开 follow-up issues

**Files:** 无改动；GitHub issue 操作。

- [ ] **Step 1: 开 issue「AssetLib 包是否需要带 templates/skill」**

```bash
gh issue create \
  --title "AssetLib 提交是否要带 templates/skill 目录" \
  --body "$(cat <<'EOF'
## 背景

#xx 实现后 godot-cli-control init 会把 SKILL.md 写到用户项目的 .claude/.codex 下，模板源在 python/godot_cli_control/templates/skill/SKILL.md，靠 wheel 派发。

## 问题

AssetLib 提交（refs #18）走的是 GDScript 插件路径，不带 Python wheel。如果用户走 AssetLib 安装，`godot-cli-control init` 这条 Python 命令不存在，skill 也没法落盘。

## 选项

1. AssetLib README 直接说明 "如果你也想要 agent skill，请额外 pipx install godot-cli-control"
2. 把 SKILL.md（不带占位符的静态版）也放进 addons/godot_cli_control/，让 GDScript 用户能手动复制
3. 不动 —— 当前 AssetLib 用户多半不在 agent 工作流里

## 建议

先选 1（最低成本），等 issue tracker 出现 "AssetLib 用户也要 skill" 的实际请求再升级到 2。
EOF
)"
```

- [ ] **Step 2: （可选）开 issue「daemon 启动时打印已装 skill 路径」**

按价值评估，可跳过；如要开则简短描述："让 agent 通过 daemon 输出反向发现 skill 位置，方便不读 README 也能找到。"

- [ ] **Step 3: 把开出的 issue 链接贴给用户**

把 Step 1（与可选 Step 2）的 issue URL 列出来给用户确认。

---

## 完成标志（done definition）

- [ ] `pytest python/tests/` 全绿
- [ ] `godot-cli-control init` 在最小 Godot 项目下产出两条 SKILL.md
- [ ] `--no-skills`、`--skills-only` 行为符合 Task 9 描述
- [ ] `.claude/skills/godot-cli-control/SKILL.md` 已在本仓库根提交（渲染版）
- [ ] README.md / python/README.md 含 Agent integration 说明
- [ ] follow-up issue 已开
