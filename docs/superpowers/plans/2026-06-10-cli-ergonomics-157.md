# #157 CLI 体验四改（PR A）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次清掉 #157 的 items 1/2/3/5 —— `run --time-scale` 缺失、`--port` 位置敏感、转码失败 exit 2 过载、`get` sub-path typo 静默 null。

**Architecture:** 三项纯 Python CLI 层改动（argparse + 退出码 + handler），一项 GDScript bridge 改动（sub-path leaf 校验）。每项独立 commit，TDD 红→绿→提交。item 4（emit_signal 逃生门）不在本计划，单独走 PR B。

**Tech Stack:** Python 3.10+ / argparse / pytest（`coverage run -m pytest`）；Godot 4 GDScript / GUT（`addons/godot_cli_control/tests/run_gut.py`，需 `GODOT_BIN`）。

**测试执行规则（全局）：** 任何 pytest / GUT / ruff 一律**委托 subagent（model sonnet）执行，禁 `run_in_background`**，主会话只收精简结论。本机 godot 在 `~/.local/bin`，venv 在仓库根 `.venv`，e2e/GUT 本机须真跑。

---

## File Structure

- `python/godot_cli_control/cli.py` — 加 `EXIT_TRANSCODE_FAILED`、`_add_connection_flags`、跨位置 guard、`run --time-scale` 透传、`run_p` 加 flag、doc 字符串。
- `python/godot_cli_control/daemon.py` — 加 `STOP_RC_TRANSCODE_FAILED=4`，`stop()` 转码失败返回它。
- `python/tests/test_cli.py` — items 5/2 的解析与 guard 测试；补 `cmd_run` Namespace mock 的 `time_scale` 字段。
- `python/tests/test_daemon.py` — item 1 的 `stop()` 返回 4 + drift-guard 测试。
- `addons/godot_cli_control/bridge/low_level_api.gd` — item 3 sub-path leaf 校验。
- `addons/godot_cli_control/tests/gut/test_low_level_api.gd` — item 3 GUT 测试。
- `CLAUDE.md` / `python/godot_cli_control/templates/skill/SKILL.md` — 退出码表 / pitfalls / time-scale / sub-path 文档。
- `.claude/skills/godot-cli-control/SKILL.md`（渲染版）— Task 5 统一重渲染。
- `CHANGELOG.md` — Task 5 记 `[Unreleased]`。

---

## Task 1: item 5 — `run` 透传 `--time-scale`

**Files:**
- Modify: `python/godot_cli_control/cli.py`（`run_p` argparse ~2906-2919；`cmd_run` 的 `daemon.start(...)` 调用 2090-2098）
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: 写失败测试**

在 `python/tests/test_cli.py` 加（解析层 + 透传层各一）：

```python
def test_run_parses_time_scale():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["run", "script.py", "--time-scale", "5"])
    assert ns.time_scale == 5.0


def test_run_passes_time_scale_to_daemon_start(tmp_path, monkeypatch, capsys):
    """cmd_run 必须把 --time-scale 透传进 daemon.start（与 daemon start 对称）。"""
    from godot_cli_control import cli

    script = tmp_path / "s.py"
    script.write_text("def run(bridge):\n    pass\n")

    captured = {}

    class _FakeDaemon:
        def __init__(self, *a, **k):
            pass

        def is_running(self):
            return False

        def start(self, **kwargs):
            captured.update(kwargs)
            return 1

        def current_port(self):
            return 12345

        def stop(self):
            return 0

    import godot_cli_control.daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "Daemon", _FakeDaemon)
    # 脚本执行打桩成功，避免真连 daemon
    monkeypatch.setattr(cli, "_exec_user_script", lambda *a, **k: 0)

    ns = cli.build_parser().parse_args(
        ["run", str(script), "--time-scale", "5"]
    )
    rc = cli.cmd_run(ns)
    assert rc == 0
    assert captured.get("time_scale") == 5.0
```

- [ ] **Step 2: 跑测试看红**

委托 subagent（sonnet）跑：`cd python && ../.venv/bin/python -m pytest tests/test_cli.py::test_run_parses_time_scale tests/test_cli.py::test_run_passes_time_scale_to_daemon_start -q`
预期：FAIL（`ns` 无 `time_scale` 属性 / `captured` 无 `time_scale`）。

- [ ] **Step 3: 实现**

3a. `run_p`（cli.py ~2909 `_add_instance_name_flag(run_p)` 之后、`_add_output_format_flags(run_p)` 2919 之前）加，镜像 `start_p` 2808-2813：

```python
    run_p.add_argument(
        "--time-scale",
        type=_time_scale_arg,
        default=None,
        help="启动即设 Engine.time_scale（>0 且 <=100），整套 e2e 提速用（同 daemon start）",
    )
```

3b. `cmd_run` 的 `daemon.start(...)`（2090-2098）补一行 `time_scale=`：

```python
                daemon.start(
                    record=ns.record,
                    movie_path=ns.movie_path,
                    headless=_resolve_headless(ns, force_gui_hint=force_gui_hint),
                    fps=ns.fps,
                    port=ns.port,
                    idle_timeout=idle_seconds,
                    time_scale=getattr(ns, "time_scale", None),
                    always_on_top=ns.always_on_top,
                )
```

- [ ] **Step 4: 跑测试看绿 + 全套 test_cli**

委托 subagent（sonnet）：
1. `../.venv/bin/python -m pytest tests/test_cli.py::test_run_parses_time_scale tests/test_cli.py::test_run_passes_time_scale_to_daemon_start -q` → PASS
2. **全套** `../.venv/bin/python -m pytest tests/test_cli.py -q` → 全绿（命中既知坑：若有 `cmd_run` 的手构造 `Namespace(...)` mock 缺 `time_scale` → AttributeError，**补 `time_scale=None` 字段**；见 memory test-cli-namespace-mock-needs-new-fields）。

预期：若全套有红，按上条补字段后转绿。

- [ ] **Step 5: 提交**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py
git commit -m "feat(cli): run 透传 --time-scale（#157 item5）"
```

---

## Task 2: item 2 — RPC 子命令接受后置 `--port` / `--instance`

**Files:**
- Modify: `python/godot_cli_control/cli.py`（新增 `_add_connection_flags`；RPC 循环 ~3016 调用；`main()` RPC 块 ~3245 后加 guard）
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: 写失败测试**

```python
def test_rpc_subcommand_accepts_trailing_port():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["exists", "/root/Foo", "--port", "9999"])
    assert ns.port == 9999


def test_rpc_subcommand_leading_port_still_works():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["--port", "9999", "exists", "/root/Foo"])
    assert ns.port == 9999


def test_rpc_subcommand_trailing_instance_all():
    from godot_cli_control import cli
    ns = cli.build_parser().parse_args(["exists", "/root/Foo", "--instance", "all"])
    assert ns.instance == "all"


def test_cross_position_port_and_instance_conflict(monkeypatch, capsys):
    """--instance 前置 + --port 后置：两个 mutex 都照不到，guard 兜成 usage 错。"""
    from godot_cli_control import cli
    monkeypatch.setattr(
        sys, "argv",
        ["godot-cli-control", "--instance", "client1", "exists", "/root/Foo", "--port", "5"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 64
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == -1003
```

（文件顶部若未 import `sys` / `json` / `pytest`，按现有 import 补；多数 test_cli 已有。）

- [ ] **Step 2: 跑测试看红**

委托 subagent（sonnet）：`../.venv/bin/python -m pytest tests/test_cli.py -k "trailing_port or leading_port or trailing_instance_all or cross_position" -q`
预期：trailing/instance FAIL（`unrecognized arguments: --port`）；leading PASS；cross_position FAIL（无 guard，会往下走连接逻辑）。

- [ ] **Step 3: 实现**

3a. cli.py 加 `_add_connection_flags`（放在 `_add_output_format_flags` 2651 定义附近，如其后）：

```python
def _add_connection_flags(p: argparse.ArgumentParser) -> None:
    """RPC 子命令的后置 ``--port`` / ``--instance``（#157）。

    与 ``_add_output_format_flags`` 同款：``default=argparse.SUPPRESS`` 使这两个
    flag 只在显式后置传入时才写 namespace，不覆盖顶层 ``conn_grp`` 的
    ``default=None``。这样 RPC 子命令既能写 ``--port N <cmd>``（顶层），也能写
    ``<cmd> ... --port N``（本处），消灭「--port 必须前置」pitfall。per-subparser
    mutex 守同位置同给；跨位置同给（一前一后）由 ``main()`` 的 guard 兜——顶层
    mutex 与本处 mutex 都照不到跨位置。
    """
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--port",
        type=int,
        default=argparse.SUPPRESS,
        help="（亦可后置）RPC 连接的 GameBridge 端口；与 --instance 互斥",
    )
    grp.add_argument(
        "--instance",
        type=_instance_arg_allow_all,
        default=argparse.SUPPRESS,
        help="（亦可后置）目标实例名（all=广播）；与 --port 互斥",
    )
```

3b. RPC 循环里调用——cli.py 3016 `_add_output_format_flags(sp)` 之后加一行：

```python
        _add_output_format_flags(sp)
        _add_connection_flags(sp)
```

3c. `main()` 的 RPC 块加 guard——cli.py 3245（preflight 的 `sys.exit(EXIT_USAGE)` 那段结束）之后、3249 `if ns.instance == "all":` 之前插入：

```python
        # #157：跨位置 --port + --instance 同给——顶层 mutex 只管前置同给、
        # subparser mutex 只管后置同给，二者跨位置（一前一后）都照不到。两者
        # default 均 None，非 None 即「显式给过」→ 用法错（与 argparse mutex 同级）。
        if ns.port is not None and ns.instance is not None:
            msg = "--port 与 --instance 互斥：二选一（指定端口或实例名）"
            if fmt == OUTPUT_JSON:
                _emit_error_payload(CLIENT_CODE_USAGE, msg)
            else:
                print(f"错误：{msg}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
```

- [ ] **Step 4: 跑测试看绿 + 全套 test_cli**

委托 subagent（sonnet）：
1. `../.venv/bin/python -m pytest tests/test_cli.py -k "trailing_port or leading_port or trailing_instance_all or cross_position" -q` → PASS
2. **全套** `../.venv/bin/python -m pytest tests/test_cli.py -q` → 全绿（重点回归：`--instance all` 广播、`--port` 前置、各 RPC 子命令仍正常 parse）。

- [ ] **Step 5: 提交**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py
git commit -m "feat(cli): RPC 子命令接受后置 --port/--instance + 跨位置冲突 guard（#157 item2）"
```

---

## Task 3: item 1 — 转码失败专用退出码 4

**Files:**
- Modify: `python/godot_cli_control/daemon.py`（模块常量 ~46；`stop()` 324 docstring + 359）
- Modify: `python/godot_cli_control/cli.py`（exit 常量 50-51；`cmd_run` finally 2140-2143；`cmd_daemon_stop` 单停注释 1882-1883）
- Modify: `CLAUDE.md`（原则 3 退出码表）
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（退出码表 / payload / 录制段）
- Test: `python/tests/test_daemon.py`

- [ ] **Step 1: 写失败测试**

在 `python/tests/test_daemon.py` 加：

```python
def test_transcode_failed_returns_dedicated_rc(tmp_path, monkeypatch):
    """daemon stop 转码失败 → 返回 STOP_RC_TRANSCODE_FAILED(4)，进程已停。"""
    from godot_cli_control import daemon as daemon_mod

    d = daemon_mod.Daemon(tmp_path)
    # 伪造「进程已优雅退出」+「有待转码 movie」+「转码失败」
    monkeypatch.setattr(d, "read_pid", lambda: 4321)
    monkeypatch.setattr(daemon_mod, "_process_alive", lambda pid: True)
    monkeypatch.setattr(daemon_mod, "_process_is_godot", lambda pid: True)
    monkeypatch.setattr(d, "_graceful_quit", lambda pid: True)
    monkeypatch.setattr(d, "_cleanup_state_files", lambda: None)
    d.movie_path_file.parent.mkdir(parents=True, exist_ok=True)
    movie = tmp_path / "rec.avi"
    movie.write_text("x")
    d.movie_path_file.write_text(str(movie))
    monkeypatch.setattr(daemon_mod, "_transcode_movie", lambda mp, cd: False)

    assert d.stop() == daemon_mod.STOP_RC_TRANSCODE_FAILED == 4


def test_transcode_failed_exit_code_single_source():
    """cli.EXIT_TRANSCODE_FAILED 与 daemon.STOP_RC_TRANSCODE_FAILED 不许 drift。"""
    from godot_cli_control import cli, daemon
    assert cli.EXIT_TRANSCODE_FAILED == daemon.STOP_RC_TRANSCODE_FAILED == 4
```

（`d.movie_path_file` 路径属性若与上面构造不符，按 `Daemon.__init__` 实际属性名调整 —— 实现者读 `daemon.py` 的 `Daemon.__init__` 确认 `movie_path_file` / `control_dir` 字段，使 fixture 走到 359 那段。）

- [ ] **Step 2: 跑测试看红**

委托 subagent（sonnet）：`../.venv/bin/python -m pytest tests/test_daemon.py::test_transcode_failed_returns_dedicated_rc tests/test_daemon.py::test_transcode_failed_exit_code_single_source -q`
预期：FAIL（`STOP_RC_TRANSCODE_FAILED` / `EXIT_TRANSCODE_FAILED` 未定义）。

- [ ] **Step 3: 实现**

3a. daemon.py 模块常量（在 ~46 `_GRACEFUL_RPC_TIMEOUT = 2.0` 附近）：

```python
# #157：录制 daemon 已正常停止、原始 AVI 保留、仅 ffmpeg 转 mp4 失败时 stop() 的返回码。
# 专用 4（不复用 infra 的 2）——脚本可用 rc==4 直接判「只差转码」，2 留给真 infra 故障。
STOP_RC_TRANSCODE_FAILED = 4
```

3b. daemon.py `stop()` docstring（324）：

```python
        """停止 daemon。返回 exit code（0 成功；STOP_RC_TRANSCODE_FAILED=4 转码失败但进程已停）。"""
```

3c. daemon.py 359 `rc = 2` → `rc = STOP_RC_TRANSCODE_FAILED`：

```python
                if not _transcode_movie(movie_path, self.control_dir):
                    rc = STOP_RC_TRANSCODE_FAILED
```

3d. cli.py exit 常量（50 `EXIT_PARTIAL = 3` 之后、51 `EXIT_USAGE = 64` 之前）：

```python
EXIT_TRANSCODE_FAILED = 4  # #157：daemon 已正常停止、原始 AVI 保留、仅 ffmpeg 转码失败
# （进程已停、信封仍 ok:true + daemon_stop_warning）。值须等于 daemon.STOP_RC_TRANSCODE_FAILED，
# drift 由 test_transcode_failed_exit_code_single_source 守。
```

3e. cli.py `cmd_run` finally（2140-2143）—— 文案点明转码失败：

```python
                else:
                    if stop_rc != 0:
                        # stop() 现仅返回 0 或 STOP_RC_TRANSCODE_FAILED(4)：4 = ffmpeg
                        # 转码失败但进程已停、原始 AVI 保留。让 envelope 携带这一信号。
                        stop_warning = (
                            f"daemon stop rc={stop_rc} "
                            "(mp4 transcode failed; raw recording preserved)"
                        )
```

（`if exit_code == 0 and stop_rc != 0: exit_code = stop_rc` 不动——天然透传 4。）

3f. cli.py `cmd_daemon_stop` 单停注释（1882-1883）：把 `2=ffmpeg 转码失败` 改 `4=ffmpeg 转码失败`：

```python
        # rc 0=正常停 / 4=ffmpeg 转码失败但 daemon 已停。两种都算"stopped"，
        # 把 rc 透出让 agent 决定要不要 retry transcode。
```

3g. **CLAUDE.md** 原则 3 退出码表：① 把 `2 = ...` 行末的「；`daemon stop` ffmpeg 转码失败也是 2」删除；② 新增一行：

```
   - 4 = `daemon stop` / `run` 进程已正常停止、原始 AVI 保留、仅 ffmpeg 转 mp4 失败（信封仍 ok:true + daemon_stop_warning）。注意 `daemon stop --all` 聚合**不计**此项（聚合仍只 DaemonError → 3），转码失败只反映在 per-entry rc
```

3h. **SKILL.md 模板** 改四处（实现者用 Edit，逐字匹配现文）：
- 行 17：`... / 2 (connection, timeout) / 64 (usage)` → `... / 2 (connection, timeout) / 4 (recording saved, transcode failed) / 64 (usage)`
- 退出码表 row 2（行 54）：删除其中 `Also: **`daemon stop` returns 2** when ... still exits 2.` 整句（转码相关），保留 infra 部分；并在 row 2 后新增 row 4：
  `| 4 | **Recording-only soft failure**: \`daemon stop\` / \`run\` stopped the process cleanly and kept the raw \`.avi\`, but the \`ffmpeg\` \`.avi\`→\`.mp4\` transcode failed (\`.cli_control/ffmpeg.log\` has details). Envelope stays \`ok:true\` with a \`daemon_stop_warning\`. \`daemon stop --all\` does **not** fold this into its aggregate (still \`0|3\`); it shows as that entry's \`rc:4\`. |`
- 行 88 `daemon stop --all` payload：把 `"rc": 0|3` 一句补注 per-entry 可为 4：`Each entry's \`rc\` is the per-instance stop result (a transcode-only failure shows as \`4\`); the top-level \`rc\` is the aggregate (\`0|3\`, \`3\` only on a hard stop failure).`
- 行 445：`exits with code \`2\`` → `exits with code \`4\``

- [ ] **Step 4: 跑测试看绿 + 回归**

委托 subagent（sonnet）：
1. `../.venv/bin/python -m pytest tests/test_daemon.py -q` → 全绿（**grep 既有断言**：若 test_daemon 有期望转码失败 rc==2 的旧用例，改 2→4）。
2. `../.venv/bin/python -m pytest tests/ -k "run or stop or transcode" -q` → 全绿。

- [ ] **Step 5: 提交**

```bash
git add python/godot_cli_control/daemon.py python/godot_cli_control/cli.py python/tests/test_daemon.py CLAUDE.md python/godot_cli_control/templates/skill/SKILL.md
git commit -m "feat(cli): 转码失败专用 exit 4（#157 item1）"
```

---

## Task 4: item 3 — `get` sub-path leaf typo fail-loud（仅 vector 系）

**Files:**
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd`（`_read_property` 137-146；新增 const + helper）
- Test: `addons/godot_cli_control/tests/gut/test_low_level_api.gd`

- [ ] **Step 1: 写失败测试**

在 `addons/godot_cli_control/tests/gut/test_low_level_api.gd` 末尾加（沿用文件既有 `_api` / `add_child_autofree` setup）：

```gdscript
# ── sub-path leaf fail-loud（#157，仅 vector 系）─────────────────

func test_subpath_valid_vector2_leaf_reads_scalar() -> void:
	var node := Node2D.new()
	node.name = "SubPathV2"
	node.position = Vector2(3, 7)
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()),
		"property": "position:y",
	})
	assert_does_not_have(result, "error")
	assert_eq(result.get("value"), 7.0)


func test_subpath_typo_vector2_leaf_returns_1002_with_valid_list() -> void:
	var node := Node2D.new()
	node.name = "SubPathV2Typo"
	node.position = Vector2(3, 7)
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()),
		"property": "position:z",  # Vector2 无 z
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.PROPERTY_NOT_FOUND)
	assert_string_contains(result["error"]["message"], "valid leaves: x, y")


func test_subpath_typo_vector3_leaf_returns_1002() -> void:
	var node := Node3D.new()
	node.name = "SubPathV3Typo"
	node.position = Vector3(1, 2, 3)
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()),
		"property": "position:w",  # Vector3 无 w
	})
	assert_has(result, "error")
	assert_eq(result["error"]["code"], CliControlErrorCodes.PROPERTY_NOT_FOUND)


func test_subpath_uncovered_compound_type_passes_through() -> void:
	# Color 未纳入白名单 → 退回 get_indexed 现状：合法 leaf 仍读到值，不误杀。
	var node := Node2D.new()
	node.name = "SubPathColor"
	node.modulate = Color(0.1, 0.2, 0.3, 0.4)
	add_child_autofree(node)
	var result: Dictionary = _api.handle_get_property({
		"path": str(node.get_path()),
		"property": "modulate:r",
	})
	assert_does_not_have(result, "error")
	assert_almost_eq(result.get("value"), 0.1, 0.0001)
```

- [ ] **Step 2: 跑测试看红**

委托 subagent（sonnet）：`GODOT_BIN=~/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py`（或同款只跑 test_low_level_api）。
预期：typo 两例 FAIL（现状静默返回 `{"value": null}`，无 error）；valid / passthrough 两例 PASS。

- [ ] **Step 3: 实现**

3a. low_level_api.gd 加 script 级 const（放在文件常量区，如 `_read_property` 上方）：

```gdscript
# #157：内置复合 Variant 的封闭 leaf 集——sub-path typo fail-loud 用。只收录能
# 100% 枚举完整的类型（vector 系，x/y/z/w 铁稳零漏列）；Color/Rect2/Transform 等
# leaf 集大、需实证完整后再加（follow-up）。键为 typeof()，值为合法 leaf 名。
const _SUBPATH_CLOSED_LEAVES := {
	TYPE_VECTOR2: ["x", "y"],
	TYPE_VECTOR2I: ["x", "y"],
	TYPE_VECTOR3: ["x", "y", "z"],
	TYPE_VECTOR3I: ["x", "y", "z"],
	TYPE_VECTOR4: ["x", "y", "z", "w"],
	TYPE_VECTOR4I: ["x", "y", "z", "w"],
}
```

3b. `_read_property` docstring（132-136）改写 + 144-145 替换为先校验：

```gdscript
## 读单个属性，支持 sub-path（"position:x"，与 set 侧对称走 get_indexed）。
## 返回 {"value": Variant} 或 {"error": ...}。
## sub-path：先逐段 walk 校验 leaf（封闭复合类型 typo → 1002，#157）；遇开放/
## 未收录类型即停、退回 get_indexed 现状（leaf 非法仍静默 null，零回归）。
func _read_property(node: Node, property: String) -> Dictionary:
	if property.is_empty():
		return _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'property' parameter")
	var is_sub_path: bool = ":" in property
	var top_level: String = _top_level_of(property)
	if not _has_property(node, top_level):
		return _err(CliControlErrorCodes.PROPERTY_NOT_FOUND, "Property not found: %s" % top_level)
	if is_sub_path:
		var leaf_err: Dictionary = _validate_sub_path_leaves(node, property)
		if leaf_err.has("error"):
			return leaf_err
		return {"value": node.get_indexed(NodePath(property))}
	return {"value": node.get(property)}
```

3c. 新增 helper（放在 `_read_property` 之后）：

```gdscript
## sub-path leaf fail-loud（#157）：逐段 walk。当前段值是封闭复合类型且 leaf 不在
## 其合法集 → 1002（message 带合法 leaf 列表）；遇开放/未收录类型即停、放行（退回
## get_indexed 现状，零回归）。leaf 合法则用已校验前缀走 get_indexed 取下一段值续 walk
## （前缀合法故非 null-from-typo）。返回 {} 放行 / {"error": ...} 命中 typo。
func _validate_sub_path_leaves(node: Node, property: String) -> Dictionary:
	var segments: PackedStringArray = property.split(":", false)
	# segments[0] = top_level（调用方已 _has_property 校验存在）
	var current: Variant = node.get(segments[0])
	for i in range(1, segments.size()):
		var t: int = typeof(current)
		if not _SUBPATH_CLOSED_LEAVES.has(t):
			return {}  # 开放/未收录类型：停止校验，放行
		var valid: Array = _SUBPATH_CLOSED_LEAVES[t]
		var leaf: String = segments[i]
		if not (leaf in valid):
			return _err(
				CliControlErrorCodes.PROPERTY_NOT_FOUND,
				"Sub-path leaf not found: %s (valid leaves: %s)" % [property, ", ".join(valid)]
			)
		current = node.get_indexed(NodePath(":".join(segments.slice(0, i + 1))))
	return {}
```

- [ ] **Step 4: 跑测试看绿 + 全 GUT 回归**

委托 subagent（sonnet）：`GODOT_BIN=~/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py` → test_low_level_api 全绿，**且 test_wait_api / 其余 GUT 无回归**（`wait-prop /root/Player position:x` 这类合法 sub-path 不能被误杀）。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/bridge/low_level_api.gd addons/godot_cli_control/tests/gut/test_low_level_api.gd
git commit -m "feat(bridge): get sub-path leaf typo fail-loud（vector 系，#157 item3）"
```

---

## Task 5: 文档收尾 + SKILL 渲染 + 全套验证

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（item 2/5 的 pitfalls / time-scale；item 1 已在 Task 3 改）
- Modify: `CHANGELOG.md`
- Regenerate: `.claude/skills/godot-cli-control/SKILL.md`（及 `.codex/...` 若仓内存在）

- [ ] **Step 1: SKILL.md 模板补 item 2 / item 5 文档**

实现者用 Edit 逐字匹配：
- **time-scale 不对称**（行 ~90）：把 `**Asymmetry**: \`run <script>\` mode does not support \`--time-scale\` as a startup flag — inside the script call \`bridge.time_scale(5)\` on the first line instead; or use \`daemon start --time-scale 5\` beforehand and connect the script to the already-running daemon.` 改为：`\`run <script>\` also accepts \`--time-scale N\` (passed through to the auto-started daemon, #157) — equivalent to \`bridge.time_scale(N)\` on the script's first line.`
- **`--port` 双 flag pitfall**（行 ~596）：把 `(auto-discovered ...). **Must come before the subcommand.**` 末句改为 `Accepts both positions now (#157): \`--port N <subcommand>\` or \`<subcommand> ... --port N\`.`
- **`--port`/`--instance` 互斥**（行 ~140）：句末补 `— giving both in any position order is a \`-1003\` usage / exit 64 error.`
- **`get` sub-path 描述**（行 ~258）：在 sub-path 那句后补 `A typo'd leaf on a closed compound type (Vector2/3/4 family) now fails loud with \`1002\` listing the valid leaves; other compound types (Color, Transform, …) still return \`{"value": null}\` for an unknown leaf (open leaf set — can't validate without false positives).`
- 若模板 pitfalls 有「Sub-path reading a non-existent leaf returns null」独立条目（实现者 grep `non-existent leaf` / `silently returns null`），改写为「vector 系已 fail-loud；其余类型仍 null」。

- [ ] **Step 2: CHANGELOG `[Unreleased]` 记四条**

在 `CHANGELOG.md` 的 `[Unreleased]` 段补（沿用既有 Added/Changed 小标题风格）：

```markdown
### Added
- `run <script>` 支持 `--time-scale N`，与 `daemon start` 对称（透传给自动启动的 daemon）（#157）。
- RPC 子命令的 `--port` / `--instance` 现可后置（`<subcommand> ... --port N`），不再强制前置；跨位置同给二者报 `-1003` usage 错（#157）。

### Changed
- 转码失败专用退出码 **4**（`daemon stop` / `run` 进程已正常停止、原始 AVI 保留、仅 ffmpeg 转 mp4 失败），从过载的 exit 2 拆出；信封仍 `ok:true` + `daemon_stop_warning`。`daemon stop --all` 聚合不计此项（仍 `0|3`），只反映在 per-entry rc（#157）。
- `get` sub-path 读封闭复合类型（Vector2/3/4 系）的 typo leaf 现 fail-loud 报 `1002`（列出合法 leaf），不再静默返回 null（#157）。
```

- [ ] **Step 3: 重渲染仓内 SKILL.md**

委托 subagent（sonnet）跑（**`COLUMNS=80` + Python 3.12**，CI skill-render-drift 以 3.12 为准）：

```bash
COLUMNS=80 python3.12 -c "from godot_cli_control import skills_install; skills_install.render_skill(...)"
```

实现者按 `skills_install.render_skill` 实际签名渲染到 `.claude/skills/godot-cli-control/SKILL.md`（与 `test_skills_install.py` 用法对齐）；先 `python -c "from godot_cli_control import cli; print(cli.format_full_help())"` 确认 help 渲染不崩。

- [ ] **Step 4: 全套验证（委托 subagent，sonnet，禁后台）**

1. `cd python && coverage run -m pytest && coverage report`（覆盖率 ≥ 80）
2. `python addons/godot_cli_control/tests/run_gut.py`（`GODOT_BIN=~/.local/bin/godot`）全绿
3. `ruff check python/`（CI 用，本地 .venv 可能没装 → 用 `python3.12 -m ruff` 或临时 pip 装；务必跑，pytest 绿 ≠ lint 干净）
4. `pytest python/tests/test_skills_install.py -q`（init 注入不崩）
5. `git diff --stat` 确认仅预期文件改动

- [ ] **Step 5: 提交**

```bash
git add CHANGELOG.md python/godot_cli_control/templates/skill/SKILL.md .claude/skills/godot-cli-control/SKILL.md
git commit -m "docs(157): CHANGELOG + SKILL.md 同步（exit4 / 后置 --port / run --time-scale / sub-path）"
```

---

## 收尾分诊（PR 前）

- **item 3 follow-up**：Color/Rect2/Transform 等的 sub-path leaf 校验未纳入 PR A（需实证完整 leaf 集）。按全局分诊规则：先 `gh issue list --search "sub-path leaf"` 查重，无则开一条 issue（带：现状=仅 vector 系 fail-loud、位置 `low_level_api.gd:_SUBPATH_CLOSED_LEAVES`、建议=逐类型 GUT 实证完整集后加、优先级低）。
- 越界发现（如 `cmd_run` 缺 `--time-scale` 之外的其它 daemon-start flag 不对称）记清单，PR 前统一分诊。
- 串行 base main；`gh pr merge --auto`（main required check = `ci-ok`，真等绿）；挂完用 `autoMergeRequest` 非 null 自检。

## Self-Review（已过）

- **Spec 覆盖**：items 5/2/1/3 各对应 Task 1/2/3/4，文档 Task 5。✓
- **占位扫描**：fixture 字段名（Task3 Step1）与 render_skill 签名（Task5 Step3）标注「实现者读实际签名」——非逻辑占位，是「匹配现有代码」指令。✓
- **类型一致**：`STOP_RC_TRANSCODE_FAILED`(daemon) / `EXIT_TRANSCODE_FAILED`(cli) 双常量 + drift-guard 测试；`_add_connection_flags` / `_validate_sub_path_leaves` 命名前后一致。✓
