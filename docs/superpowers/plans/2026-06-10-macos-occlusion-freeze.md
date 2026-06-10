# macOS 遮挡冻帧 / 截图 stale 帧防护 实现计划（#156 子问题 B）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 macOS 对被遮挡窗口的渲染节流——录制默认窗口置顶（B1）、截图抓帧前强制渲染（B2）、截图信封带 `stale_suspect` 风险信号（B3）。

**Architecture:** B1 走录制路径（`daemon.py` args + `cli.py` flag + `game_bridge.gd` `_ready` 设 `always_on_top`）；B2/B3 走截图路径（`low_level_api.gd` `take_screenshot_async`：抓帧前 `force_draw`，算出 `png_buffer` 后比对 hash 标 `stale_suspect`）。

**Tech Stack:** Python 3.10+ / argparse、GDScript 4（GUT）、pytest（含 GUI e2e gated by `GCC_GUI_E2E=1`）。

**Spec:** `docs/superpowers/specs/2026-06-10-macos-occlusion-freeze-design.md`

**测试执行约定（全局规则）：** 所有测试委托 subagent（`model: sonnet`）跑，主会话只收精简结论；**不要** `run_in_background`。GUT 需 `GODOT_BIN`（本机 `~/.local/bin/godot`）。覆盖率门禁用 `coverage run -m pytest`（**不可** `pytest --cov`）；开发期验单测用 `python -m pytest <path>::<test> -v`。GUI e2e 本机真跑（`GCC_GUI_E2E=1`），不可声称缺 GODOT_BIN 跳过。

---

### Task 1: B1 — `daemon.start(always_on_top)` + CLI `--no-always-on-top`

**Files:**
- Modify: `python/godot_cli_control/daemon.py`（`start()` 签名 ~L143、args 构建 ~L245-250）
- Modify: `python/godot_cli_control/cli.py`（`_add_daemon_flags` ~L2598、`cmd_daemon_start` daemon.start 调用 ~L1723、`cmd_run` daemon.start 调用 ~L2089）
- Test: `python/tests/test_daemon.py`

- [ ] **Step 1: 写失败的 pytest 测试**

照搬既有 `test_start_record_writes_movie_path_file_and_args`（~L754）的 `_record_popen` patch 栈，抽一个 module-level helper（放测试文件 helper 区，与 `_setup_start_env` ~L470 同区）+ 三个断言。`_touch_godot_project` / `Daemon` 都是文件已有的。追加：

```python
def _capture_start_args(tmp_path, monkeypatch, **start_kwargs) -> list[str]:
    """跑 daemon.start(**start_kwargs)，返回捕获的 Popen args 列表。
    照搬 test_start_record_writes_movie_path_file_and_args 的 patch 栈（registry
    隔离由模块级 autouse fixture _isolate_registry 负责）。"""
    _touch_godot_project(tmp_path)
    fake_bin = tmp_path / "fake_godot"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)
    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 999_999_700
        returncode = None

        def poll(self) -> None:
            return None

    def _record_popen(args: list[str], **kwargs: Any) -> _FakeProc:
        captured["args"] = args
        return _FakeProc()

    monkeypatch.setattr("godot_cli_control.daemon.find_godot_binary", lambda: str(fake_bin))
    monkeypatch.setattr("godot_cli_control.daemon._ensure_imported", lambda *a, **k: None)
    monkeypatch.setattr("godot_cli_control.daemon.subprocess.Popen", _record_popen)
    monkeypatch.setattr("godot_cli_control.daemon.time.sleep", lambda *_: None)
    monkeypatch.setattr("godot_cli_control.daemon._wait_port_ready", lambda *a, **k: True)

    Daemon(tmp_path).start(**start_kwargs)
    return captured["args"]


def test_start_record_adds_always_on_top_arg_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """record=True 默认 always_on_top → Godot args 含 --game-bridge-always-on-top。"""
    args = _capture_start_args(
        tmp_path, monkeypatch,
        record=True, movie_path=str(tmp_path / "r.avi"), port=29993, always_on_top=True,
    )
    assert "--game-bridge-always-on-top" in args


def test_start_record_no_always_on_top_omits_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """record=True always_on_top=False → 不加该 arg。"""
    args = _capture_start_args(
        tmp_path, monkeypatch,
        record=True, movie_path=str(tmp_path / "r.avi"), port=29992, always_on_top=False,
    )
    assert "--game-bridge-always-on-top" not in args


def test_start_non_record_omits_always_on_top_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 record → always_on_top 无意义，不加该 arg。"""
    args = _capture_start_args(
        tmp_path, monkeypatch, record=False, port=29991, always_on_top=True,
    )
    assert "--game-bridge-always-on-top" not in args
```

> `Any` 已在文件顶部 import（既有测试用 `**kwargs: Any`）。`record=True` 必须传合法 `.avi` `movie_path`，否则 `start()` 先因 `--record requires --movie-path` 报错。

- [ ] **Step 2: 跑测试确认失败**

委托 subagent：`python -m pytest python/tests/test_daemon.py -k always_on_top -v`
Expected: FAIL —— `daemon.start()` 不接受 `always_on_top` 参数（`TypeError`），或 arg 未加。

- [ ] **Step 3: 实现**

`daemon.py` `start()` 签名（keyword-only 区，与 `time_scale` 同级）加：

```python
        always_on_top: bool = True,
```

args 构建处（record 追加 `--write-movie` 那块，~L245 `if record:` 内或紧邻）加：

```python
        if record and always_on_top:
            # macOS 遮挡窗口会冻帧、Movie Maker 照写 stale 帧（#156 子问题 B）；
            # 录制默认置顶根治。--no-always-on-top 可关。
            args.append("--game-bridge-always-on-top")
```

`cli.py` `_add_daemon_flags(p)`（~L2598，紧跟 `--record` / `--movie-path` 注册）加：

```python
    p.add_argument(
        "--no-always-on-top",
        action="store_false",
        dest="always_on_top",
        default=True,
        help="录制时不强制窗口置顶（默认 --record 置顶，防 macOS 遮挡窗口冻帧 / Movie "
        "Maker 写 stale 帧）。仅 --record 时有意义。",
    )
```

`cmd_daemon_start`（~L1723 `daemon.start(`）与 `cmd_run`（~L2089 `daemon.start(`）两处调用都加：

```python
            always_on_top=ns.always_on_top,
```

- [ ] **Step 4: 跑测试确认通过**

委托 subagent：`python -m pytest python/tests/test_daemon.py -k always_on_top -v`（3 green）。再 `python -m pytest python/tests/test_daemon.py -q` 确认无回归。

- [ ] **Step 5: 提交**

```bash
git add python/godot_cli_control/daemon.py python/godot_cli_control/cli.py python/tests/test_daemon.py
git commit -m "feat(daemon): record 默认 always_on_top + --no-always-on-top（#156 子问题 B / B1）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: B1 — bridge `_ready` 设置 `always_on_top` + e2e 验证

**Files:**
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`（`_ready` ~L87-99，`_register_methods()` 之后、`await _wait_first_frame_ready()` 之前）
- Test: `python/tests/test_e2e_record.py`（GUI e2e，复用 `_run_cli` / `godot_project`）

- [ ] **Step 1: 写 e2e 测试（机制端到端验证）**

`get /root always_on_top` 能读 Window 属性，所以 B1 可自动验证。在 `test_e2e_record.py` 追加（对齐既有 `_run_cli(project, *args)` 风格）：

```python
def test_record_sets_window_always_on_top(godot_project: Path) -> None:
    """daemon start --record 默认置窗口 always_on_top；读 /root:always_on_top 应为 True。"""
    project = godot_project
    avi = project / "aot.avi"
    start = _run_cli(
        project, "daemon", "start",
        "--record", "--movie-path", str(avi), "--fps", str(_FPS),
        timeout=120,
    )
    assert start["ok"] is True and start["result"]["started"], start
    try:
        got = _run_cli(project, "get", "/root", "always_on_top")
        assert got["ok"] is True, got
        assert got["result"]["value"] is True, f"record 应置 always_on_top：{got}"
    finally:
        _run_cli(project, "daemon", "stop", timeout=60)


def test_record_no_always_on_top_opt_out(godot_project: Path) -> None:
    """--no-always-on-top → /root:always_on_top 为 False。"""
    project = godot_project
    avi = project / "aot_off.avi"
    start = _run_cli(
        project, "daemon", "start",
        "--record", "--movie-path", str(avi), "--fps", str(_FPS),
        "--no-always-on-top",
        timeout=120,
    )
    assert start["ok"] is True and start["result"]["started"], start
    try:
        got = _run_cli(project, "get", "/root", "always_on_top")
        assert got["ok"] is True, got
        assert got["result"]["value"] is False, f"--no-always-on-top 应不置顶：{got}"
    finally:
        _run_cli(project, "daemon", "stop", timeout=60)
```

> 对齐既有测试的 `get` 命令字段（`get <path> <prop>` 返回 `result.value`；先看 `test_record_produces_valid_mp4` 与一个现成 `get` 调用确认信封形状）。若两个测试共享 module-scope `godot_project` 需先幂等 `daemon stop` 保底（参考子问题 A 的 e2e 做法）。

- [ ] **Step 2: 跑测试确认失败**

委托 subagent（本机真跑，**不可**声称缺 GODOT_BIN 跳过）：
`GCC_GUI_E2E=1 GODOT_BIN=~/.local/bin/godot python -m pytest python/tests/test_e2e_record.py -k always_on_top -v`
Expected: FAIL —— bridge 未设 always_on_top，`/root:always_on_top` 为 False（默认）。

- [ ] **Step 3: 实现**

`game_bridge.gd` `_ready()` 中，`_register_methods()`（~L87）之后、`await _wait_first_frame_ready()`（~L99）之前加：

```gdscript
	# 录制防遮挡冻帧（#156 子问题 B）：daemon 传 --game-bridge-always-on-top 时置顶窗口。
	# macOS 对被遮挡窗口节流渲染 → Movie Maker 写 stale 帧；置顶根治。失焦无碍、遮挡才致命。
	if _has_cli_flag("--game-bridge-always-on-top"):
		get_window().always_on_top = true
```

- [ ] **Step 4: 跑测试确认通过**

委托 subagent：`GCC_GUI_E2E=1 GODOT_BIN=~/.local/bin/godot python -m pytest python/tests/test_e2e_record.py -k always_on_top -v`（2 green）。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/bridge/game_bridge.gd python/tests/test_e2e_record.py
git commit -m "feat(bridge): record 时置 always_on_top + e2e 验证（#156 子问题 B / B1）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: B2 + B3 — screenshot force_draw + stale_suspect

**Files:**
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd`（成员声明区、新 `_mark_screenshot_stale_check`、`take_screenshot_async` ~L584-643）
- Test: `addons/godot_cli_control/tests/gut/test_low_level_api.gd`（B3 纯逻辑单测）；windowed e2e（B2，本机/CI macOS job 验 screenshot 不破坏）

- [ ] **Step 1: 写失败的 GUT 测试（B3 hash 判定纯逻辑）**

`test_low_level_api.gd` 的 `before_each`（~L40）已构造 `_api`（LowLevelApi）。追加：

```gdscript
func test_stale_check_first_call_not_stale() -> void:
	var buf := PackedByteArray([1, 2, 3])
	assert_false(_api._mark_screenshot_stale_check(buf), "首张截图永不 stale")

func test_stale_check_same_bytes_is_stale() -> void:
	var buf := PackedByteArray([1, 2, 3])
	_api._mark_screenshot_stale_check(buf)  # 第一张，记录
	assert_true(_api._mark_screenshot_stale_check(buf), "相同字节 → stale_suspect")

func test_stale_check_different_bytes_not_stale() -> void:
	_api._mark_screenshot_stale_check(PackedByteArray([1, 2, 3]))
	assert_false(_api._mark_screenshot_stale_check(PackedByteArray([4, 5, 6])), "不同字节 → 非 stale")
```

- [ ] **Step 2: 跑测试确认失败**

委托 subagent：`GODOT_BIN=~/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py -gtest=test_low_level_api.gd`
Expected: FAIL —— `_mark_screenshot_stale_check` 未定义。

- [ ] **Step 3: 实现（B3 方法 + B2 force_draw + 接线）**

`low_level_api.gd` 成员声明区（`var` 成员处；常量区之后）加：

```gdscript
# screenshot stale 检测（#156 子问题 B / B3）：本次 png 字节与上次相同 → stale_suspect。
# _has_prev 哨兵：避免首张恰好 hash 命中 _last==0 误判。
var _last_screenshot_hash: int = 0
var _has_prev_screenshot: bool = false
```

新方法（放 `take_screenshot_async` 附近）：

```gdscript
# 比对本次 png 字节与上张：相同则可疑 stale（风险提示，非确定断言——游戏真静止时
# 也相同）。更新 state 并返回是否 stale。抽成纯方法便于 GUT 单测（#156 子问题 B / B3）。
func _mark_screenshot_stale_check(png_buffer: PackedByteArray) -> bool:
	var h: int = png_buffer.hash()
	var stale: bool = _has_prev_screenshot and h == _last_screenshot_hash
	_last_screenshot_hash = h
	_has_prev_screenshot = true
	return stale
```

`take_screenshot_async`：dummy 检测（~L584 `var dummy: bool = ...`）之后、抓帧 for 循环（~L587）之前加 B2：

```gdscript
	# macOS 遮挡窗口会冻帧、get_image() 拿旧画面（#156 子问题 B / B2）；抓帧前主动
	# 强制渲染一帧绕过节流。dummy/headless 无 GPU，不调。
	if not dummy:
		RenderingServer.force_draw()
```

算出 `png_buffer`（~L617 `var png_buffer ... = image.save_png_to_buffer()`）之后、`save_path` 分支（~L623）之前加 B3：

```gdscript
	# stale 风险信号（#156 子问题 B / B3）：与上张字节相同 → 标 stale_suspect。
	# path 直写与 base64 两路 return 的 response 都会带上。
	if _mark_screenshot_stale_check(png_buffer):
		response["stale_suspect"] = true
```

- [ ] **Step 4: 跑 GUT 确认通过**

委托 subagent：`GODOT_BIN=~/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py -gtest=test_low_level_api.gd`（3 stale 测试 green，无回归）。

- [ ] **Step 5: B2 windowed e2e 验证（force_draw 不破坏抓帧）**

委托 subagent（本机真跑）：录或起一个 GUI daemon → 连续两次 `screenshot` 写文件 → 断言两个文件非空有效。复用 `test_e2e_record.py` 或现有 windowed screenshot e2e 入口（先 `grep -n "screenshot" python/tests/test_e2e*.py` 找现成 GUI screenshot 测试；若有就确认它在加 force_draw 后仍 PASS，否则追加一个最小：起 GUI daemon → `screenshot out.png` → 断言 `out.png` 存在非空）。
`GCC_GUI_E2E=1 GODOT_BIN=~/.local/bin/godot python -m pytest python/tests/ -k "screenshot and e2e" -v`（或对应入口）→ PASS。
> 诚实边界：真·遮挡冻帧无法自动复现；此步只验 force_draw 不破坏正常抓帧。「遮挡时真出新帧」靠本机手动验证（PR 描述记录）。

- [ ] **Step 6: 提交**

```bash
git add addons/godot_cli_control/bridge/low_level_api.gd addons/godot_cli_control/tests/gut/test_low_level_api.gd
git commit -m "feat(screenshot): 抓帧前 force_draw + stale_suspect 信号（#156 子问题 B / B2+B3）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 文档同步（CHANGELOG + SKILL.md + 重渲染）

**Files:**
- Modify: `addons/godot_cli_control/CHANGELOG.md`（`[Unreleased]`）
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（录制 + 截图章节）
- Regenerate: `.claude/skills/godot-cli-control/SKILL.md`

- [ ] **Step 1: CHANGELOG**

`[Unreleased]` 下，`### Added` 加：

```markdown
- **screenshot `stale_suspect` 风险信号 + `--no-always-on-top`**（#156 子问题 B）：`screenshot` 信封新增可选 `stale_suspect: true`——本次截图字节与上一次完全相同（**可能** stale 也可能游戏真静止，风险提示非确定断言），agent 据此决定重试 / force。`daemon start --record` / `run --record` 新增 `--no-always-on-top` opt-out。
```

`### Changed` 加：

```markdown
- **macOS 遮挡冻帧防护**（#156 子问题 B）：`daemon start --record` 现在默认置窗口 `always_on_top`（macOS 对被遮挡窗口节流渲染 → Movie Maker 写 stale 帧 / 截图拿旧画面，置顶根治；`--no-always-on-top` 关）；`screenshot` 服务端抓帧前 `RenderingServer.force_draw()` 主动出新帧绕过遮挡节流。下游不再需要 `set always_on_top` + osascript / `grab_focus` + `wait-frames` 用户侧 workaround。需要新 addon 行为——老项目跑一次 `init` 同步。
```

- [ ] **Step 2: SKILL.md 模板**

`grep -n "always_on_top\|grab_focus\|osascript\|wait-frames\|遮挡\|stale\|occlu\|录制\|screenshot" python/godot_cli_control/templates/skill/SKILL.md` 定位现有 workaround。删 / 改：录制章节的 `always_on_top`+osascript、截图章节的 `grab_focus`+`wait-frames` workaround，改述：record 默认置顶（`--no-always-on-top` opt-out）、screenshot 默认 force_draw、信封 `stale_suspect` 字段含义（与上张相同时出现，风险提示非确定 stale）。简洁准确，不 bloat。

- [ ] **Step 3: 验证模板渲染 + init 注入**

委托 subagent：
```
python -c "from godot_cli_control import cli; print(cli.format_full_help()[:200])"
python -m pytest python/tests/test_skills_install.py -v
```
Expected: 不抛异常；`test_skills_install` 全绿。

- [ ] **Step 4: 重渲染仓内副本（CI 官方命令，Python 3.12 + COLUMNS=80）**

```bash
COLUMNS=80 python3.12 -c "from godot_cli_control import cli; from godot_cli_control.skills_install import render_skill; from godot_cli_control._version import version; open('.claude/skills/godot-cli-control/SKILL.md','w').write(render_skill(version, cli.format_full_help()))"
```
确认 `python3.12 --version` 可用；`git diff .claude/skills` 只显示预期的录制/截图章节变更（+ 可能归一化的 version 行）。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/CHANGELOG.md python/godot_cli_control/templates/skill/SKILL.md .claude/skills
git commit -m "docs(156): CHANGELOG + SKILL.md 同步遮挡冻帧防护（#156 子问题 B）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾（实施完成后，开 PR 前）

- [ ] **本机手动验证（遮挡场景，自动化测不到）**：录制时拖另一窗口遮挡目标 → 产物不再是静止单帧（`ffprobe` 时长正常 + 肉眼看有运动）；截图前遮挡 → force_draw 后仍拿到新帧（连续截图 SHA 不同）。结论写进 PR 描述。
- [ ] **ruff lint**（本机 .venv 未必装 ruff）：`ruff check python/` 干净。
- [ ] **全量 GUT + pytest** 委托 subagent 跑一遍，确认无回归 + 覆盖率 ≥ 80%。
- [ ] **分诊收尾**（CLAUDE.md「实施收尾：先分诊，再开 Issue」）：盘点越界发现 / 未覆盖场景；能当场修的修，有后果的按门槛开 issue。
- [ ] **PR**：base `main`，单 PR（本仓串行 base main、不用 stacked PR）。PR 描述写明手动验证结论 + 测试诚实边界（自动化只覆盖机制）。
- [ ] **#156 关闭判定**：A（#165）+ B 都落地后，#156 可关闭——在本 PR 描述里说明「本 PR 交付子问题 B，连同已合并的 A 一起覆盖 #156 全部范围，合并后关闭 #156」。
```
