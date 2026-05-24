# AI-Friendly CLI Fixes Implementation Plan

> **Status:** ✅ Completed in #46 (2026-05-11). 所有 task 已 land；3 项 Minor follow-up 已开 issue 跟踪。下方 checkbox 保留原貌仅作历史记录。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 7 issues found during AI-friendliness review so the CLI fully delivers on the "shell-first, JSON-envelope, AI-driven" contract documented in `CLAUDE.md`.

**Architecture:** Surgical patches to existing modules — argparse wiring, error-code constants, defaults. No new subsystems. Each fix is isolated to 1–3 files plus the SKILL.md template / addon README that document the change.

**Tech Stack:** Python 3.10+, argparse, pytest (+ pytester + monkeypatch), GDScript 4 (Godot plugin), websockets.

---

## Pre-flight

This plan should be executed on a feature branch. The work is small enough (~6 commits) to ship as a single PR; per-task commits keep the history bisectable.

```bash
git checkout -b fix/ai-friendly-cli-cleanup
```

Tests must be run via subagent with `model: "sonnet"` per global CLAUDE.md. The harness command is `coverage run -m pytest python/tests/<file>.py -v` (note: `coverage run`, not `pytest --cov` — see `pyproject.toml` `[tool.coverage.run]` comment for why).

---

## File Map

| File | Tasks touching it | Why |
|---|---|---|
| `python/godot_cli_control/cli.py` | T1, T5, T6, T7 | argparse wiring (T1), tree `--max-nodes` (T5), set/call `--text-value` (T6), daemon start headless default (T7) |
| `python/godot_cli_control/client.py` | T5 | tree truncate response shape |
| `python/godot_cli_control/bridge.py` | T5 | sync wrapper for new tree shape |
| `python/godot_cli_control/pytest_plugin.py` | T2 | default port 0 |
| `addons/godot_cli_control/bridge/low_level_api.gd` | T3, T5 | new error code 1005 + `max_nodes` param |
| `addons/godot_cli_control/bridge/error_codes.gd` *(new)* | T3 | central GDScript error-code constants |
| `addons/godot_cli_control/bridge/input_simulation_api.gd` | T3 | use `ErrorCodes.COMBO_IN_PROGRESS` instead of magic 1004 |
| `python/godot_cli_control/templates/skill/SKILL.md` | T1, T3, T5, T6, T7 | document each behavioral change |
| `addons/godot_cli_control/README.md` | T4 | error-code table refresh |
| `addons/godot_cli_control/CHANGELOG.md` | T9 | one consolidated entry under [Unreleased] |
| `CLAUDE.md` | T8 | strike out the now-fixed known-issues list |
| `python/tests/test_cli.py` | T1, T5, T6, T7 | unit tests for argparse + new flags |
| `python/tests/test_pytest_plugin.py` | T2 | test default port = 0 |
| `python/tests/test_client.py` | T5 | test tree truncate parse |

---

## Task 1: Allow `--json` / `--text` after RPC subcommand

**Why:** `godot-cli-control click /root/X --json` currently fails with `unrecognized arguments: --json`. LLMs habitually append flags at the tail; this is the single most-impactful AI footgun. Only `daemon ls` works today because it duplicated `_add_output_format_flags(ls_p)` ad-hoc.

**Files:**
- Modify: `python/godot_cli_control/cli.py:1099-1280` (`build_parser`)
- Modify: `python/tests/test_cli.py` (add new test class)

**Steps:**

- [ ] **Step 1.1: Write failing test for RPC subcommand**

Add to `python/tests/test_cli.py`:

```python
class TestOutputFormatFlagsOnSubcommands:
    """--json / --text 必须能放在子命令前 *和* 后两种位置。

    AI agent 习惯把 flag 写在尾巴；早期 build_parser 只在顶层注册，
    `click /root/X --json` 会被 argparse 报 unrecognized。
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["click", "/root/X", "--json"],
            ["--json", "click", "/root/X"],
            ["click", "/root/X", "--text"],
            ["click", "/root/X", "--no-json"],
            ["exists", "/root/Foo", "--text"],
            ["tree", "3", "--json"],
            ["daemon", "status", "--json"],
            ["daemon", "stop", "--text"],
            ["daemon", "start", "--headless", "--json"],
        ],
    )
    def test_output_flag_accepted_at_tail(self, argv: list[str]) -> None:
        from godot_cli_control.cli import build_parser

        parser = build_parser()
        # parse_args raises SystemExit on rejection
        ns = parser.parse_args(argv)
        assert ns.output_format in ("json", "text")
```

- [ ] **Step 1.2: Run test to confirm failure**

Dispatch subagent (`model: "sonnet"`):

> Run `coverage run -m pytest python/tests/test_cli.py::TestOutputFormatFlagsOnSubcommands -v` from the repo root. Report PASS/FAIL counts and any error messages, no output dump.

Expected: most parametrize cases FAIL with `SystemExit` / `unrecognized arguments`.

- [ ] **Step 1.3: Add `_add_output_format_flags(sp)` to every subparser**

In `python/godot_cli_control/cli.py:build_parser`:

1. Add after each `daemon_subs.add_parser(...)` call (start_p, stop_p, status_p — `ls_p` already has it):
   ```python
   _add_output_format_flags(start_p)
   _add_output_format_flags(stop_p)
   _add_output_format_flags(status_p)
   ```
   Note: `status_p` doesn't currently have its own variable — name it (replace the bare `daemon_subs.add_parser("status", ...)` call with `status_p = daemon_subs.add_parser(...)`).

2. Add inside the RPC subcommand registration loop (around line 1278, after `if spec.extra_args is not None: spec.extra_args(sp)`):
   ```python
   _add_output_format_flags(sp)
   ```

3. Also add to `run_p` and `init_p` (defensive — they don't emit JSON envelopes today, but consistency matters and `init` already calls `_emit_top_error` paths through future error envelope work):
   ```python
   _add_output_format_flags(run_p)
   _add_output_format_flags(init_p)
   ```

- [ ] **Step 1.4: Run test to confirm pass**

Dispatch subagent: same command as Step 1.2.
Expected: all parametrize cases PASS.

- [ ] **Step 1.5: Smoke check at the shell**

```bash
/Users/kesar/Projects/godot-cli-control/.venv/bin/python -m godot_cli_control click /root/X --json 2>&1 | head -2
```
Expected: `{"ok": false, "error": {"code": -1001, ...}}` (connection refused — that's fine, parser accepted the flag).

- [ ] **Step 1.6: Update SKILL.md**

In `python/godot_cli_control/templates/skill/SKILL.md` *Common pitfalls* section: remove or rewrite the `--port doesn't change daemon port` bullet to also clarify `--json` / `--text` work at any position. Specifically replace lines 295–296 with:

```markdown
- **Top-level flags work in any position** — `--json` / `--text` / `--port N` are accepted both before and after subcommands as of this fix. Pre-fix sessions only honored them at the front.
```

- [ ] **Step 1.7: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py python/godot_cli_control/templates/skill/SKILL.md
git commit -m "fix(cli): 子命令尾部接受 --json / --text / --port

argparse 默认子命令不继承父 parser 的 optional flag。早期只在 daemon ls
单独补了 _add_output_format_flags，导致 \`click /root/X --json\` 这类
LLM 习惯写法被 argparse 拒。给所有 RPC 子命令 + daemon start/stop/
status + run + init 统一注入，flag 可放任意位置。"
```

---

## Task 2: pytest fixture default port → 0 (OS-assigned)

**Why:** `pytest_plugin.py` line 38 hardcodes `default=str(DEFAULT_PORT)` (= 9877). Multi-project parallel test runs collide; 9877 is also the port `daemon start` no longer uses by default. Must align with daemon's OS-assigned default.

**Files:**
- Modify: `python/godot_cli_control/pytest_plugin.py:33-78`
- Modify: `python/tests/test_pytest_plugin.py` (add test)

**Steps:**

- [ ] **Step 2.1: Write failing test**

Add to `python/tests/test_pytest_plugin.py`:

```python
def test_default_port_is_zero(pytester: pytest.Pytester) -> None:
    """--godot-cli-port 默认 0 = OS 自动分配，与 daemon start 默认对齐。

    早期版本默认 9877 会让多项目并行测试撞端口，且 9877 不再是 daemon
    start 默认。fixture 必须放手让 daemon 自己挑。
    """
    result = pytester.runpytest("--help")
    # default 值会出现在 help 输出里
    result.stdout.fnmatch_lines(
        ["*--godot-cli-port*", "*default: 0*"]
    )


def test_godot_daemon_passes_port_zero_when_default(
    pytester: pytest.Pytester,
) -> None:
    """fixture 拿默认值（不传 --godot-cli-port）时 daemon.start(port=0)。"""
    pytester.makeconftest(
        """
        import godot_cli_control.pytest_plugin as plugin

        OBSERVED: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return False
            def start(self, **kw): OBSERVED.append(kw.get("port"))
            def stop(self): return 0
            def current_port(self): return 5555

        class FakeBridge:
            def __init__(self, port): OBSERVED.append(f"bridge:{port}")
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_sessionfinish(session, exitstatus):
            assert OBSERVED[0] == 0, f"expected port=0 default, got {OBSERVED}"
        """
    )
    pytester.makepyfile(
        test_x="""
        def test_x(godot_daemon, bridge): pass
        """
    )
    result = pytester.runpytest("-v")
    assert result.ret == 0
```

- [ ] **Step 2.2: Run tests to confirm failure**

Dispatch subagent: `coverage run -m pytest python/tests/test_pytest_plugin.py::test_default_port_is_zero python/tests/test_pytest_plugin.py::test_godot_daemon_passes_port_zero_when_default -v`. Report PASS/FAIL.
Expected: FAIL (default still 9877).

- [ ] **Step 2.3: Change default to 0**

In `python/godot_cli_control/pytest_plugin.py:36-40`:

```python
group.addoption(
    "--godot-cli-port",
    action="store",
    default="0",  # 0 = OS-assigned，与 daemon start 默认对齐
    help="GameBridge WebSocket port for the godot_daemon fixture (default: 0 = OS-assigned).",
)
```

In the same file at line 100 (`bridge` fixture body) — already does `godot_daemon.current_port() or DEFAULT_PORT`, leave unchanged. The fix is only the option default.

- [ ] **Step 2.4: Run tests to confirm pass**

Same subagent command as Step 2.2.
Expected: PASS.

- [ ] **Step 2.5: Commit**

```bash
git add python/godot_cli_control/pytest_plugin.py python/tests/test_pytest_plugin.py
git commit -m "fix(pytest): --godot-cli-port 默认 0（OS-assigned）

之前默认 9877 会让多项目并行测试撞端口，且 daemon start 自身已改默认
0；fixture 必须对齐。bridge fixture 已经走 daemon.current_port() 兜底，
只需改 option 默认。"
```

---

## Task 3: Resolve error-code 1004 collision (scene tree → 1005)

**Why:** `low_level_api.gd:188` returns `1004 "scene tree too large"`; `input_simulation_api.gd` uses 1004 for `"combo in progress"` in 5 places. Same code, opposite retry semantics. SKILL.md only documents the combo case.

User decision: introduce **business code 1005** for scene-tree-too-large. Centralize all GDScript codes into a constants file so future code adds force a grep.

**Files:**
- Create: `addons/godot_cli_control/bridge/error_codes.gd`
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd:18-34, 187-189`
- Modify: `addons/godot_cli_control/bridge/input_simulation_api.gd` (re-grep first; previously observed 5 raw `_err(1004, …)` call sites + 1 comment match)
- Modify: `python/godot_cli_control/templates/skill/SKILL.md` (error-code table + new pitfall)
- Modify: `python/tests/test_client.py` (add code-table assertion if a constants module exists in Python; otherwise skip)

**Steps:**

- [ ] **Step 3.1: Create `error_codes.gd` constants module**

Create `addons/godot_cli_control/bridge/error_codes.gd`:

```gdscript
class_name CliControlErrorCodes
extends RefCounted
## 集中错误码常量。新加业务码必须在这里登记，
## 避免 1004 那种隐式撞码（input_sim 用 "combo in progress"，
## low_level 又用 "scene tree too large"）。
##
## 三段制（详见 SKILL.md 错误码表）：
##   1xxx        服务端业务码
##   -32xxx      JSON-RPC 标准
##   -1xxx       客户端（Python）侧；GDScript 这边不会产出

const NODE_NOT_FOUND: int = 1001
const PROPERTY_NOT_FOUND: int = 1002       # 也用于 "node has no 'text' property"
const METHOD_NOT_FOUND: int = 1003         # 也用于 "screenshot unavailable"
const COMBO_IN_PROGRESS: int = 1004
const SCENE_TREE_TOO_LARGE: int = 1005

const INVALID_PARAMS: int = -32602
const INVALID_REQUEST: int = -32600
const METHOD_UNKNOWN: int = -32601
```

- [ ] **Step 3.2: Update `low_level_api.gd` to use new constant**

In `addons/godot_cli_control/bridge/low_level_api.gd`:

1. Replace line 187–189 (`return _err(1004, "scene tree too large ...")`):
   ```gdscript
   return _err(
       CliControlErrorCodes.SCENE_TREE_TOO_LARGE,
       "scene tree too large (>%d nodes); lower 'depth' or query a subtree" % _BUILD_TREE_NODE_LIMIT,
   )
   ```

2. While here, replace other magic numbers with constants for consistency:
   - `_err(1001, ...)` → `_err(CliControlErrorCodes.NODE_NOT_FOUND, ...)`
   - `_err(1002, ...)` → `_err(CliControlErrorCodes.PROPERTY_NOT_FOUND, ...)`
   - `_err(1003, ...)` → `_err(CliControlErrorCodes.METHOD_NOT_FOUND, ...)`
   - `_err(-32602, ...)` → `_err(CliControlErrorCodes.INVALID_PARAMS, ...)`

- [ ] **Step 3.3: Update `input_simulation_api.gd`**

First re-grep to find all sites:
```bash
grep -nE "_err\(\s*1004" addons/godot_cli_control/bridge/input_simulation_api.gd
```
Replace each `_err(1004, "combo in progress")` site with:
```gdscript
_err(CliControlErrorCodes.COMBO_IN_PROGRESS, "combo in progress")
```
(Earlier observation: 5 raw `_err(` call sites + 1 comment-only match. Re-grep is authoritative.)

- [ ] **Step 3.4: Update SKILL.md error-code reference**

In `python/godot_cli_control/templates/skill/SKILL.md`:

1. In the *Server-side* error table (around line 73–78), add a new row for 1005:
   ```markdown
   | `1005` | Scene tree too large to serialize (default safety limit). Pass `--max-nodes` or query a subtree with `children` / `tree <subpath>`. Don't retry as-is. |
   ```

2. The 1004 row stays unchanged (combo-in-progress).

3. In *Common pitfalls*, add:
   ```markdown
   - **`tree` returns `1005 "scene tree too large"`** — your scene has more than 5000 visible nodes (a Grid / spawned-bullets situation). Pass `--max-nodes 200` to cap, or `children <path>` for one specific subtree.
   ```

- [ ] **Step 3.5: GUT test for 1005**

If GUT tests are runnable here (requires `GODOT_BIN`), add to `addons/godot_cli_control/tests/gut/test_low_level_api.gd` a test that builds a synthetic 6000-node tree and asserts `code == 1005`. If `GODOT_BIN` isn't set in this environment, skip GUT and rely on Step 3.6 (Python integration check via re-grep that no `1004` literal remains in `low_level_api.gd`).

- [ ] **Step 3.6: Static check — no orphan 1004 in low_level_api.gd**

Run:
```bash
grep -nE "(^|[^[:digit:]])1004([^[:digit:]]|$)" addons/godot_cli_control/bridge/low_level_api.gd
```
Expected: no matches.

```bash
grep -nE "(^|[^[:digit:]])1004([^[:digit:]]|$)" addons/godot_cli_control/bridge/input_simulation_api.gd
```
Expected: only matches inside comments referring to the combo case (no `_err(1004, …)` raw form).

- [ ] **Step 3.7: Commit**

```bash
git add addons/godot_cli_control/bridge/error_codes.gd \
        addons/godot_cli_control/bridge/low_level_api.gd \
        addons/godot_cli_control/bridge/input_simulation_api.gd \
        python/godot_cli_control/templates/skill/SKILL.md
git commit -m "fix(error-codes): scene tree 超限改 1005，与 combo 1004 解耦

之前 1004 同时表 \"combo in progress\"（input_sim）与 \"scene tree too
large\"（low_level）。两者 retry 策略截然不同，agent 拿到 1004 没法判。

- 新增 CliControlErrorCodes 常量类，集中所有 GDScript 错误码
- scene tree 超限改 1005 \"scene tree too large\"
- low_level / input_sim 全部用常量替换裸数字
- SKILL.md 错误码表 + pitfalls 加 1005 说明"
```

---

## Task 4: Refresh addon README error-code table

**Why:** `addons/godot_cli_control/README.md:102` lists only `-32600/-32601/-32602/1001/1002/1003`. Missing 1004, the new 1005, and the entire client-side `-1xxx` segment. GitHub viewers see this README first.

**Files:**
- Modify: `addons/godot_cli_control/README.md` (around the *RPC Reference* section)

**Steps:**

- [ ] **Step 4.1: Replace the one-liner with a proper subsection**

Find the line that reads:
```
Error codes: `-32600` invalid request, `-32601` unknown method, `-32602` invalid params, `1001` node not found, `1002` property not found, `1003` method not found.
```

Replace with:

```markdown
### Error codes

Three numeric ranges share `error.code`; they never overlap, so a single field is unambiguous.

| Code | Source | Meaning |
|---|---|---|
| `1001` | server | Node not found at the given path |
| `1002` | server | Property not found / shape mismatch |
| `1003` | server | Method not found / render unavailable |
| `1004` | server | Combo already in progress (call `combo-cancel` to retry) |
| `1005` | server | Scene tree too large (lower `depth` or pass `--max-nodes`) |
| `-32600` | server | Malformed JSON-RPC request |
| `-32601` | server | Unknown method name |
| `-32602` | server | Invalid params (incl. blocked methods/properties from the security blacklist) |
| `-1001` | client | Connection failure (daemon not running, port wrong, proxy hijacking localhost) |
| `-1002` | client | Timeout waiting for response |
| `-1003` | client | CLI usage error (combo missing steps, malformed `--steps-json`, …) |
| `-1004` | client | Local file IO error (e.g. screenshot can't write the destination) |
| `-1099` | client | Internal CLI bug — please file an issue |

For full retry guidance see the SKILL.md shipped by `godot-cli-control init` (`.claude/skills/godot-cli-control/SKILL.md` in the target project).
```

- [ ] **Step 4.2: Static check — table is complete**

```bash
grep -c '^| `' addons/godot_cli_control/README.md
```
Should now show ≥ 13 (was 0 before; the grep counts the new table rows).

- [ ] **Step 4.3: Commit**

```bash
git add addons/godot_cli_control/README.md
git commit -m "docs(addon): 错误码表补全 1004/1005 + 客户端 -1xxx 段

之前只列到 1003，1004/1005 与所有客户端码缺失。GitHub 用户进 addon
README 是第一入口，应自包含；引用 SKILL.md 作为完整 retry 指南。"
```

---

## Task 5: `tree` truncate signal (`--max-nodes`)

**Why:** Default `_BUILD_TREE_NODE_LIMIT=5000` is a hard wall (now returns 1005 after Task 3). LLMs frequently call `tree 3` blind — even 200 nodes can blow context budget. Need a soft cap with a structured `truncated: true` signal.

**Files:**
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd` (`handle_get_scene_tree` + `_build_tree`)
- Modify: `python/godot_cli_control/client.py` (`get_scene_tree` accept + pass `max_nodes`)
- Modify: `python/godot_cli_control/bridge.py` (`tree` accept `max_nodes`)
- Modify: `python/godot_cli_control/cli.py` (`cmd_tree` register `--max-nodes`, default 200)
- Modify: `python/godot_cli_control/templates/skill/SKILL.md` (document new arg + truncate shape)
- Modify: `python/tests/test_client.py` (parse-truncated test)
- Modify: `python/tests/test_cli.py` (argparse test)

**Steps:**

- [ ] **Step 5.1: Write failing test for CLI argparse acceptance**

Add to `python/tests/test_cli.py`:

```python
def test_tree_accepts_max_nodes_flag() -> None:
    from godot_cli_control.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["tree", "3", "--max-nodes", "50"])
    assert ns.depth == "3"
    assert ns.max_nodes == 50


def test_tree_max_nodes_default_is_200() -> None:
    from godot_cli_control.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["tree"])
    assert ns.max_nodes == 200
```

- [ ] **Step 5.2: Write failing test for client parse**

Add to `python/tests/test_client.py` (or create new `TestTreeTruncate` class):

```python
@pytest.mark.asyncio
async def test_get_scene_tree_returns_truncate_metadata() -> None:
    """服务端在节点超限时返回 {tree, truncated, total_nodes}，client 透传。"""
    import godot_cli_control.client as client_mod

    class _FakeWS:
        async def send(self, _): pass

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["params"] = params
        return {
            "tree": {"name": "root", "type": "Node", "path": "/root", "children": []},
            "truncated": True,
            "total_nodes": 6000,
        }

    client = client_mod.GameClient(port=1)
    monkeypatch_target = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.get_scene_tree(depth=3, max_nodes=100)
    finally:
        client_mod.GameClient.request = monkeypatch_target
    assert captured["params"] == {"depth": 3, "max_nodes": 100}
    assert result["truncated"] is True
    assert result["total_nodes"] == 6000
```

- [ ] **Step 5.3: Run failing tests**

Subagent: `coverage run -m pytest python/tests/test_cli.py::test_tree_accepts_max_nodes_flag python/tests/test_cli.py::test_tree_max_nodes_default_is_200 python/tests/test_client.py::test_get_scene_tree_returns_truncate_metadata -v`.
Expected: 3 FAIL.

- [ ] **Step 5.4: Implement CLI flag**

In `python/godot_cli_control/cli.py`:

1. Add a registrator (place near the other `_register_*_args` helpers around line 358):
   ```python
   def _register_tree_args(p: argparse.ArgumentParser) -> None:
       p.add_argument(
           "depth",
           nargs="?",
           default=None,
           help="遍历深度，默认 3",
       )
       p.add_argument(
           "--max-nodes",
           type=int,
           default=200,
           help=(
               "节点数软上限（默认 200）。超出时服务端截断子节点并返回 "
               "{truncated: true, total_nodes: N}，agent 据此决定是否拆分子树。"
           ),
       )
   ```

2. Update `cmd_tree` to forward `max_nodes`:
   ```python
   async def cmd_tree(client: GameClient, ns: argparse.Namespace) -> dict:
       depth = int(ns.depth) if ns.depth else 3
       return await client.get_scene_tree(depth=depth, max_nodes=ns.max_nodes)
   ```

3. Replace the existing `tree` `RpcSpec` so it uses `extra_args=_register_tree_args` and drops the inline `positionals=(Positional("depth", "?", ...))`.

- [ ] **Step 5.5: Implement client method**

In `python/godot_cli_control/client.py:get_scene_tree`:

```python
async def get_scene_tree(
    self, depth: int = 5, max_nodes: int | None = None
) -> dict:
    params: dict = {"depth": depth}
    if max_nodes is not None:
        params["max_nodes"] = max_nodes
    return await self.request("get_scene_tree", params)
```

- [ ] **Step 5.6: Implement sync bridge wrapper**

In `python/godot_cli_control/bridge.py:tree`:

```python
def tree(self, depth: int = 3, max_nodes: int | None = None) -> dict:
    return self._run(
        self._client.get_scene_tree(depth=depth, max_nodes=max_nodes)
    )
```

- [ ] **Step 5.7: Implement GDScript side**

Pre-check: confirm `_build_tree` has no other callers before changing its signature.
```bash
grep -rn "_build_tree(" addons/godot_cli_control/bridge/
```
Expected: only definition + recursive call inside `_build_tree`, plus one call from `handle_get_scene_tree`. If anywhere else, update those callers too.

In `addons/godot_cli_control/bridge/low_level_api.gd`:

1. Modify `handle_get_scene_tree` to honor `max_nodes` (default fallback to `_BUILD_TREE_NODE_LIMIT`):
   ```gdscript
   func handle_get_scene_tree(params: Dictionary) -> Dictionary:
       var max_depth: int = params.get("depth", 5) as int
       # max_nodes 是软上限：超出立刻停止 _build_tree 递归并把信号透出。
       # 不传时用硬墙 5000 兼容旧客户端。
       var max_nodes: int = params.get("max_nodes", _BUILD_TREE_NODE_LIMIT) as int
       if max_nodes <= 0:
           max_nodes = _BUILD_TREE_NODE_LIMIT
       var root: Node = get_tree().current_scene
       if root == null:
           root = get_tree().root
       var counter: Array[int] = [0]
       var tree: Dictionary = _build_tree(root, max_depth, 0, counter, max_nodes)
       var truncated: bool = counter[0] > max_nodes
       var response: Dictionary = {"tree": tree}
       if truncated:
           response["truncated"] = true
           response["total_nodes"] = counter[0]
       # 仍然保留硬墙：超 5000 走 1005，避免恶意大场景吃完 outbound buffer。
       if counter[0] > _BUILD_TREE_NODE_LIMIT:
           return _err(
               CliControlErrorCodes.SCENE_TREE_TOO_LARGE,
               "scene tree too large (>%d nodes); lower 'depth' or query a subtree" % _BUILD_TREE_NODE_LIMIT,
           )
       return response
   ```

2. Update `_build_tree` signature to accept `max_nodes`:
   ```gdscript
   func _build_tree(node: Node, max_depth: int, current_depth: int, counter: Array[int], max_nodes: int) -> Dictionary:
       counter[0] += 1
       var entry: Dictionary = {
           "name": node.name,
           "type": node.get_class(),
           "path": str(node.get_path()),
       }
       if node is CanvasItem:
           entry["visible"] = (node as CanvasItem).visible
       if "text" in node:
           entry["text"] = str(node.get("text"))
       if counter[0] > max_nodes:
           return entry
       var effective_max: int = _BUILD_TREE_HARD_LIMIT if max_depth == 0 else max_depth
       if current_depth < effective_max:
           var children: Array[Dictionary] = []
           for child: Node in node.get_children():
               children.append(_build_tree(child, effective_max, current_depth + 1, counter, max_nodes))
           entry["children"] = children
       return entry
   ```

- [ ] **Step 5.8: Update SKILL.md**

In `python/godot_cli_control/templates/skill/SKILL.md`:

1. In the *Read* command list, change:
   ```markdown
   - `tree [depth]` — full scene tree
   ```
   to:
   ```markdown
   - `tree [depth] [--max-nodes N]` — full scene tree (default `--max-nodes 200`; on overflow, response includes `truncated: true` and `total_nodes: N`)
   ```

2. Add a *Tree truncation* subsection (anywhere logical in the *Command catalogue* area):
   ```markdown
   ### Tree truncation

   `tree` caps output at 200 nodes by default to keep the JSON small enough for an LLM context window. When the cap is hit, the response carries explicit signals so you can decide whether to drill in:

   ```json
   {"ok": true, "result": {
     "tree": { ... partial subtree ... },
     "truncated": true,
     "total_nodes": 6000
   }}
   ```

   Responses are subject to a hard ceiling of 5000 nodes — beyond that you get `1005 "scene tree too large"` and must `--max-nodes` down or query a subtree:

   - `tree --max-nodes 50` — quick overview
   - `children /root/Game/Spawner` — drill into one branch
   - `tree 1` — depth-1 only
   ```

- [ ] **Step 5.9: Run all changed-area tests**

Subagent: `coverage run -m pytest python/tests/test_cli.py python/tests/test_client.py -v`.
Expected: all PASS, no regressions.

- [ ] **Step 5.10: Commit**

```bash
git add python/godot_cli_control/cli.py \
        python/godot_cli_control/client.py \
        python/godot_cli_control/bridge.py \
        addons/godot_cli_control/bridge/low_level_api.gd \
        python/godot_cli_control/templates/skill/SKILL.md \
        python/tests/test_cli.py \
        python/tests/test_client.py
git commit -m "feat(tree): --max-nodes 软上限与 truncated 信号

LLM 习惯盲调 \`tree 3\`；一个 200 节点 Grid 就能把 context 吃光。
新增 --max-nodes（默认 200），超出时服务端返回 {tree (partial),
truncated: true, total_nodes: N}，agent 据此判断是否分子树。

硬墙 5000 节点不变（防恶意 / outbound buffer 保护），超出仍走 1005。"
```

---

## Task 6: `set` / `call` `--text-value` escape hatch

**Why:** SKILL.md documents the `null` / `true` / numeric-string footgun, but the workaround is `'"null"'` — three layers of quotes that LLMs routinely mangle in prompt templates. A flag that disables JSON parsing for `value` (and `args`) eliminates the failure mode.

**Files:**
- Modify: `python/godot_cli_control/cli.py` (`cmd_set`, `cmd_call`, `_register_call_args`, register flag for `set`)
- Modify: `python/godot_cli_control/templates/skill/SKILL.md` (document)
- Modify: `python/tests/test_cli.py` (test)

**Steps:**

- [ ] **Step 6.1: Write failing test**

Add to `python/tests/test_cli.py`:

```python
def test_set_text_value_disables_json_parse() -> None:
    """--text-value 让 set 把 value 当字面字符串，不走 JSON-or-string fallback。"""
    from godot_cli_control.cli import build_parser, _resolve_value_for_set

    parser = build_parser()
    ns = parser.parse_args(["set", "/root/X", "flag", "true", "--text-value"])
    assert ns.text_value is True
    # 复用 _resolve_value_for_set 抽出来的解析函数
    assert _resolve_value_for_set(ns) == "true"


def test_set_default_still_json_parses() -> None:
    from godot_cli_control.cli import build_parser, _resolve_value_for_set

    parser = build_parser()
    ns = parser.parse_args(["set", "/root/X", "flag", "true"])
    assert ns.text_value is False
    assert _resolve_value_for_set(ns) is True


def test_call_text_value_disables_arg_parse() -> None:
    from godot_cli_control.cli import build_parser, _resolve_args_for_call

    parser = build_parser()
    ns = parser.parse_args(
        ["call", "/root/X", "set_label", "true", "42", "--text-value"]
    )
    assert _resolve_args_for_call(ns) == ["true", "42"]
```

- [ ] **Step 6.2: Run failing tests**

Subagent: `coverage run -m pytest python/tests/test_cli.py -k "text_value" -v`.
Expected: FAIL (function doesn't exist).

- [ ] **Step 6.3: Implement helper functions and flag wiring**

In `python/godot_cli_control/cli.py`:

1. Add helpers (near `_parse_json_arg`):
   ```python
   def _resolve_value_for_set(ns: argparse.Namespace) -> Any:
       """根据 --text-value 决定是 JSON-or-string 解析还是直接 string。"""
       if getattr(ns, "text_value", False):
           return ns.value
       return _parse_json_arg(ns.value)


   def _resolve_args_for_call(ns: argparse.Namespace) -> list:
       raw_args: list[str] = list(ns.args or [])
       if getattr(ns, "text_value", False):
           return list(raw_args)
       return [_parse_json_arg(a) for a in raw_args]
   ```

2. Update `cmd_set` and `cmd_call`:
   ```python
   async def cmd_set(client: GameClient, ns: argparse.Namespace) -> dict:
       value = _resolve_value_for_set(ns)
       return await client.set_property(ns.node_path, ns.prop, value)


   async def cmd_call(client: GameClient, ns: argparse.Namespace) -> Any:
       args = _resolve_args_for_call(ns)
       return await client.call_method(ns.node_path, ns.method, args)
   ```

3. Add `_register_set_args` and update `_register_call_args`:
   ```python
   def _register_set_args(p: argparse.ArgumentParser) -> None:
       p.add_argument("node_path", help="绝对节点路径")
       p.add_argument("prop", help="属性名")
       p.add_argument(
           "value",
           help="JSON 字面量或字符串。例：'42' / '\"hello\"' / '[10, 20]' / 'hello'",
       )
       p.add_argument(
           "--text-value",
           action="store_true",
           help="把 value 当字面字符串，不走 JSON 解析（避开 'null'/'true'/数字 footgun）",
       )


   def _register_call_args(p: argparse.ArgumentParser) -> None:
       p.add_argument("node_path", help="绝对节点路径，如 /root/Main")
       p.add_argument("method", help="节点上的方法名")
       p.add_argument(
           "args",
           nargs="*",
           help="方法参数；每个先按 JSON 解析失败 fallback 字符串",
       )
       p.add_argument(
           "--text-value",
           action="store_true",
           help="把所有 args 当字面字符串，不走 JSON 解析",
       )
   ```

4. Update the `set` `RpcSpec` to use `extra_args=_register_set_args` and drop its inline `positionals` (matching how `call` and `combo` already do).

- [ ] **Step 6.4: Run tests to confirm pass**

Subagent: `coverage run -m pytest python/tests/test_cli.py -k "text_value" -v`.
Expected: 3 PASS.

- [ ] **Step 6.5: Update SKILL.md**

In `python/godot_cli_control/templates/skill/SKILL.md` `### set / call value parsing` section, after the existing footgun examples, add:

```markdown
**Escape hatch (preferred for LLM prompts):** pass `--text-value` to disable JSON parsing entirely:

```bash
godot-cli-control set /root/Label text null --text-value     # ✓ stores the string "null"
godot-cli-control set /root/Flag  on   true --text-value     # ✓ stores the string "true"
godot-cli-control call /root/Game start 42 easy --text-value # call(42, "easy") replaced by call("42", "easy")
```

Use this when generating commands from a template — it removes the three-quote escaping headache (`'"null"'`).
```

- [ ] **Step 6.6: Commit**

```bash
git add python/godot_cli_control/cli.py \
        python/godot_cli_control/templates/skill/SKILL.md \
        python/tests/test_cli.py
git commit -m "feat(cli): set/call --text-value 关闭 JSON 解析

null/true/false/数字字符串 footgun 的解法是 \`'\\\"null\\\"'\` —— 三层
嵌套引号在 LLM prompt 模板里经常翻车。新增 --text-value 让 value/args
强制按字符串处理，去掉这个心智负担。"
```

---

## Task 7: `daemon start` headless via `isatty` autodetect

**Why:** AI agents shell out from non-TTY stdout (CI, pipes, MCP) but currently get a Godot window unless they remember `--headless`. Interactive devs in a terminal still want the window. Autodetect via `sys.stdout.isatty()` covers 90% of real cases without breaking either workflow.

User decision: **isatty autodetect**. Add `--gui` as an explicit opt-in for forced GUI, keep `--headless` as explicit opt-out.

**Files:**
- Modify: `python/godot_cli_control/cli.py` (`_add_daemon_flags`, `cmd_daemon_start`, `cmd_run`)
- Modify: `python/godot_cli_control/templates/skill/SKILL.md` (document)
- Modify: `addons/godot_cli_control/CHANGELOG.md` (BREAKING change banner — Task 9 consolidates)
- Modify: `python/tests/test_cli.py` (test)

**Steps:**

- [ ] **Step 7.1: Write failing tests**

Add to `python/tests/test_cli.py`:

```python
class TestDaemonHeadlessAutodetect:
    def test_default_headless_when_stdout_not_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        ns = type("NS", (), {"headless": False, "gui": False})()
        assert _resolve_headless(ns) is True

    def test_default_gui_when_stdout_is_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        ns = type("NS", (), {"headless": False, "gui": False})()
        assert _resolve_headless(ns) is False

    def test_explicit_headless_wins_over_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        ns = type("NS", (), {"headless": True, "gui": False})()
        assert _resolve_headless(ns) is True

    def test_explicit_gui_wins_over_pipe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from godot_cli_control.cli import _resolve_headless

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        ns = type("NS", (), {"headless": False, "gui": True})()
        assert _resolve_headless(ns) is False

    def test_gui_and_headless_mutually_exclusive(self) -> None:
        from godot_cli_control.cli import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["daemon", "start", "--headless", "--gui"])
```

- [ ] **Step 7.2: Run failing tests**

Subagent: `coverage run -m pytest python/tests/test_cli.py::TestDaemonHeadlessAutodetect -v`.
Expected: 5 FAIL (function and `--gui` flag don't exist yet).

- [ ] **Step 7.3: Implement `_resolve_headless` and add `--gui`**

In `python/godot_cli_control/cli.py`:

1. Add helper near the top (after the EXIT_* constants):
   ```python
   def _resolve_headless(ns: argparse.Namespace) -> bool:
       """决定本次 daemon start 是否走 --headless。

       优先级：显式 --headless > 显式 --gui > stdout.isatty() 自动判。
       isatty=False（pipe / redirect / 非 TTY agent shell）默认 headless；
       isatty=True（开发者交互终端）默认开窗。
       """
       if getattr(ns, "headless", False):
           return True
       if getattr(ns, "gui", False):
           return False
       try:
           return not sys.stdout.isatty()
       except (AttributeError, ValueError):
           # ValueError: I/O operation on closed file（罕见）
           return True  # 安全默认
   ```

2. Modify `_add_daemon_flags`:
   ```python
   def _add_daemon_flags(p: argparse.ArgumentParser) -> None:
       # ... existing options ...
       headless_grp = p.add_mutually_exclusive_group()
       headless_grp.add_argument(
           "--headless",
           action="store_true",
           help="无窗口模式。默认值：stdout 非 TTY 时自动 headless（CI / pipe / agent）。",
       )
       headless_grp.add_argument(
           "--gui",
           action="store_true",
           help="强制开窗。覆盖 isatty 自动判（例如 stdout 是 pipe 仍想看到窗口）。",
       )
   ```
   Remove the old `p.add_argument("--headless", ...)` line. Keep `--record` / `--movie-path` / `--fps` / `--port` / `--idle-timeout` unchanged.

3. Update `cmd_daemon_start` to use `_resolve_headless(ns)` instead of `ns.headless`:
   ```python
   daemon.start(
       record=ns.record,
       movie_path=ns.movie_path,
       headless=_resolve_headless(ns),
       fps=ns.fps,
       port=ns.port,
       idle_timeout=idle_seconds,
   )
   ```

4. Same change in `cmd_run`:
   ```python
   daemon.start(
       record=ns.record,
       movie_path=ns.movie_path,
       headless=_resolve_headless(ns),
       fps=ns.fps,
       port=ns.port,
       idle_timeout=idle_seconds,
   )
   ```

- [ ] **Step 7.4: Run tests to confirm pass**

Subagent: same command as Step 7.2.
Expected: 5 PASS.

- [ ] **Step 7.5: Update SKILL.md**

In `python/godot_cli_control/templates/skill/SKILL.md`:

1. Quickstart section (lines 25–35): keep `daemon start --headless` example but add a note above it:
   ```markdown
   > As of this version, `daemon start` autodetects headless mode by checking `stdout.isatty()`. Pipes, CI, and agent shell-outs run headless by default; an interactive terminal still gets a window. The explicit flags below are only needed to override.
   ```

2. Common pitfalls — add bullet:
   ```markdown
   - **`daemon start` opens a window when I expected headless** — your stdout is a TTY (interactive terminal). Pass `--headless` explicitly, or shell out from a context where stdout is piped.
   ```

- [ ] **Step 7.6: Smoke test**

```bash
# Should NOT spawn a Godot window (stdout is piped):
/Users/kesar/Projects/godot-cli-control/.venv/bin/python -m godot_cli_control daemon start --json 2>&1 | head -3
```

(This will fail to find a Godot project at the repo root; that's expected. The failure envelope just confirms the flag parsed; verify no `--headless` warning prints.)

- [ ] **Step 7.7: Commit**

```bash
git add python/godot_cli_control/cli.py \
        python/godot_cli_control/templates/skill/SKILL.md \
        python/tests/test_cli.py
git commit -m "feat(daemon): headless 默认 isatty 自适应

stdout 非 TTY（pipe / CI / agent shell）默认 headless；
TTY（开发者交互终端）默认开窗。新增 --gui 显式覆盖；
--headless 仍可显式传。

LLM 在 MCP / agent shell 里调 daemon start 不再需要记得加
--headless 才能跑通。"
```

---

## Task 8: Refresh root CLAUDE.md known-issues list

**Why:** The root `CLAUDE.md` has a *已知遗留 issue* section listing exactly the items being fixed in this plan. After landing the fixes the list is stale.

**Files:**
- Modify: `CLAUDE.md` (the known-issues subsection)

**Steps:**

- [ ] **Step 8.1: Replace stale issues list**

In `CLAUDE.md`, replace the entire *已知遗留 issue* section with:

```markdown
## 已知遗留 issue（PR 路过时顺手修）

*（截至 2026-05-11，AI 友好性 review 列出的 7 项已全部 land。下次 review 发现新坑请补到这里。）*
```

- [ ] **Step 8.2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: 清理 CLAUDE.md 已修完的 review 项"
```

---

## Task 9: Consolidated CHANGELOG entry

**Why:** Every code change should land in `[Unreleased]`. Six fixes in one PR collapse into one CHANGELOG block.

**Files:**
- Modify: `addons/godot_cli_control/CHANGELOG.md` ([Unreleased] section)

**Steps:**

- [ ] **Step 9.1: Append entries under existing [Unreleased]**

Add at the end of the `## [Unreleased]` block (before the next version heading):

```markdown
### AI-friendliness review fixes (2026-05-11)

#### Fixed
- **CLI flag position**: `--json` / `--text` / `--port` 现在在 RPC 子命令尾部也接受（之前只能写最前面）。修复每个子 parser 缺 `_add_output_format_flags` 注入。
- **Error code 1004 collision**: `low_level_api.gd` 的 scene tree 超限改用新业务码 `1005 "scene tree too large"`，与 input_simulation `1004 "combo in progress"` 解耦。新增 `error_codes.gd` 集中常量。
- **pytest fixture default port**: `--godot-cli-port` 默认从 9877 改为 0（OS-assigned），与 `daemon start` 默认对齐；多项目并行测试不再撞端口。
- **Addon README error-code table**: 之前只列到 1003，补全 1004 / 1005 / 客户端 -1xxx 段。

#### Added
- `tree --max-nodes <N>`（默认 200）：节点数软上限；超出时响应含 `truncated: true` + `total_nodes`，agent 据此决定分子树。硬墙仍是 5000 节点 → `1005`。
- `set` / `call --text-value`：禁用 JSON 解析、把 value/args 强制按字符串处理，避开 `null` / `true` / `42` 这类字面量被解析成 Variant 类型的 footgun。

#### Changed
- **BREAKING (轻微)**：`daemon start` / `run` 默认 headless 行为改为基于 `sys.stdout.isatty()` 自动判定 —— pipe / CI / agent shell 默认 headless；交互终端默认开窗。新增 `--gui` 强制开窗 flag。`--headless` 仍可显式传，覆盖自动判。脚本里依赖 "默认会开窗" 的需要加 `--gui`。
```

- [ ] **Step 9.2: Commit**

```bash
git add addons/godot_cli_control/CHANGELOG.md
git commit -m "docs(changelog): 记录 AI 友好性 review 7 项修复"
```

---

## Final verification

- [ ] **Final 1: Run full Python test suite**

Dispatch subagent (`model: "sonnet"`):
> Run `coverage run -m pytest python/tests/ -v` from `/Users/kesar/Projects/godot-cli-control`. Report only: PASS/FAIL counts, names of any failing tests, and the line of the first failure if any. Do not dump full output.

Expected: all PASS, coverage ≥ 80% (`fail_under` threshold from pyproject.toml).

- [ ] **Final 2: Render SKILL.md and confirm no template breakage**

```bash
/Users/kesar/Projects/godot-cli-control/.venv/bin/python -c "from godot_cli_control import cli, _version, skills_install; print(skills_install.render_skill(_version.__version__, cli.format_full_help())[:200])"
```

Expected: prints rendered SKILL.md prefix; no `KeyError` / unrendered `{{...}}` placeholders.

- [ ] **Final 3: Confirm GUT tests still build (best-effort)**

If `GODOT_BIN` is set: `GODOT_BIN=$GODOT_BIN ./addons/godot_cli_control/tests/run_gut.sh`. Otherwise skip and rely on the GDScript static checks in T3.6.

- [ ] **Final 4: PR description draft**

Write a PR title + body summarizing the 7 fixes, linking to the review summary in the conversation, calling out the one BREAKING change (headless default).

---

## Risks

- **`--gui` flag is new** — pre-existing scripts that *expected* a window without passing any flag now get one only if stdout is a TTY. If users run daemon-start under `nohup` / `&` / IDE run-buttons that pipe stdout, they'll silently flip to headless. Mitigation: CHANGELOG entry calls this out as BREAKING; SKILL.md pitfalls section explains the override.
- **GDScript constants module** is a new file under `addons/godot_cli_control/bridge/`. Make sure it's included in the wheel via the existing `[tool.hatch.build.targets.wheel.force-include]` glob (`"addons/godot_cli_control" = "godot_cli_control/_plugin"` already covers all files in that dir).
- **Tree truncate hard-vs-soft limit interaction**: if a user passes `--max-nodes 6000`, the GDScript clamps to 5000 hard ceiling and returns 1005. Make sure the new code honors that order: count first, then check soft cap, then check hard cap. T5.7 implementation does this correctly.
- **Test parallelism** with the new `--godot-cli-port=0` default: pytest-xdist users will now have each worker on its own port — that's actually a fix, not a risk, but flag it in the CHANGELOG.
