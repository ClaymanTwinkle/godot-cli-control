# daemon stop 优雅退出 flush AVI 尾帧 实现计划（#156 子问题 A）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `daemon stop` 在 SIGTERM 之前先经 RPC `quit` 让 Godot 优雅退出，使 Movie Maker flush AVI 尾帧（消除 4-6s 丢帧）；RPC 不通自动降级现有 `_terminate()`。

**Architecture:** bridge 新增内部 `quit` RPC（退出动作抽成可注入 Callable 以便测试）；`GameClient.quit()` 把「收到响应」和「响应前断连」都判成功；`daemon._graceful_quit()` 发 RPC 后轮询进程退出，失败无缝降级。`quit` 不暴露 CLI（契约 4 有意例外，`stop` 已是 canonical surface）。

**Tech Stack:** GDScript 4 (GUT 单测)、Python 3.10+ (asyncio + websockets)、pytest / pytest-asyncio、ffmpeg/ffprobe（e2e）。

**Spec:** `docs/superpowers/specs/2026-06-10-daemon-stop-graceful-quit-design.md`

**测试执行约定（全局规则）：** 所有测试套件委托 subagent（`model: sonnet`）跑，主会话只收精简结论；不要 `run_in_background`。GUT 需 `GODOT_BIN`（本机 `~/.local/bin/godot`）。覆盖率门禁用 `coverage run -m pytest`（**不可** `pytest --cov`）；开发期验单个测试用 `python -m pytest <path>::<test> -v` 即可。

---

### Task 1: bridge `quit` RPC + 可注入退出动作

**Files:**
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`（成员声明区 ~L39 后、`_register_methods` L204、新增 `_handle_quit`）
- Test: `addons/godot_cli_control/tests/gut/test_game_bridge.gd`

- [ ] **Step 1: 写失败的 GUT 测试**

在 `test_game_bridge.gd` 末尾追加（`before_each` 已构造 `_bridge` 为 `TestableGameBridge`，已塞 stub api 并调过 `_register_methods()`；`_dispatch(raw)` helper 在 L159-166 发消息并返回末帧）：

```gdscript
func test_quit_registered_responds_ok_and_invokes_quit_action() -> void:
	# 退出动作替换成 spy，避免 get_tree().quit() 把测试进程带走
	var quit_called := [false]
	_bridge._quit_action = func() -> void: quit_called[0] = true
	var f: Dictionary = _dispatch('{"id":"q1","method":"quit","params":{}}')
	assert_eq(str(f.get("id", "MISSING")), "q1", "响应应回带请求 id")
	assert_has(f, "result")
	assert_true(bool(f.result.get("ok", false)), "quit 应回 {ok:true}")
	assert_true(quit_called[0], "quit handler 应调用注入的退出动作")
```

> 注：若 `_dispatch` helper 名与签名不同，按 L159-166 实际名调用（它返回 `captured_frames[-1]`）。

- [ ] **Step 2: 跑测试确认失败**

委托 subagent 跑：`GODOT_BIN=~/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py -gtest=test_game_bridge.gd`
Expected: FAIL —— `quit` 未注册（dispatch 得到 `-32601 Unknown method`），或 `_quit_action` 成员不存在。

- [ ] **Step 3: 实现 bridge quit RPC**

成员声明区（`game_bridge.gd` L39 `var _methods` 之后）加可注入退出动作：

```gdscript
# quit RPC（#156）：退出动作抽成可注入 Callable，GUT 里替换成 spy 不真退。
# 默认 get_tree().quit() —— SceneTree.quit() 延迟到当前帧末执行，dispatcher 已在
# 本帧同步发出 {ok} 响应，给 ws 一次 poll flush 机会；client 端另有「断连=成功」兜底。
var _quit_action: Callable = func() -> void: get_tree().quit()
```

`_register_methods()`（L258 `step_frames` 之后，函数结尾前）加注册：

```gdscript
	# quit（#156，sync）：内部 RPC，无对应 CLI 子命令——用户语义即 daemon stop
	# （契约 4 有意例外）。daemon stop 在 SIGTERM 前发此 RPC 让 Movie Maker flush 尾帧。
	_methods["quit"] = {"callable": _handle_quit, "kind": "sync"}
```

新增 handler（放在 `_wrap_screenshot` 附近，L264 后）：

```gdscript
# quit handler（#156）：请求引擎优雅退出。返回 {ok} 让 dispatcher 正常发响应，
# 退出动作走 _quit_action（默认 get_tree().quit()，帧末生效）。
func _handle_quit(_params: Dictionary) -> Dictionary:
	_quit_action.call()
	return {"ok": true}
```

- [ ] **Step 4: 跑测试确认通过**

委托 subagent 跑同上命令。
Expected: PASS（`test_quit_registered_responds_ok_and_invokes_quit_action` 绿；其余 game_bridge 测试不回归）。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/bridge/game_bridge.gd addons/godot_cli_control/tests/gut/test_game_bridge.gd
git commit -m "feat(bridge): quit RPC 优雅退出（#156 子问题 A）

内部 RPC，退出动作抽成可注入 Callable 便于 GUT 测试。不暴露 CLI
（契约 4 有意例外，daemon stop 已是 canonical surface）。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `GameClient.quit()` —— 断连即成功

**Files:**
- Modify: `python/godot_cli_control/client.py`（`request` 方法 L242-263 之后追加 `quit`）
- Test: `python/tests/test_client.py`

- [ ] **Step 1: 写失败的 pytest 测试**

在 `test_client.py` 末尾追加（沿用文件现有 fake-ws + `_pending` 注入风格，参考 `test_request_raises_rpc_error_with_code` L339 起；client 用 `GameClient(port=...)`，手动塞 `client._ws` 与 `_listen` 任务）：

```python
@pytest.mark.asyncio
async def test_quit_returns_on_response() -> None:
    """quit() 收到对应 id 响应即成功返回。"""
    client = GameClient(port=1)

    async def hang_iter():
        await asyncio.sleep(3600)
        yield  # never

    fake_ws = _make_fake_ws(hang_iter())  # 见文件内既有 helper；若无则内联构造
    client._ws = fake_ws
    client._listen_task = asyncio.create_task(client._listen())
    try:
        async def inject_ok_after_send():
            await asyncio.sleep(0.01)
            req_id = next(iter(client._pending))
            client._pending[req_id].set_result({"id": req_id, "result": {"ok": True}})

        asyncio.create_task(inject_ok_after_send())
        await asyncio.wait_for(client.quit(timeout=2.0), timeout=2.0)  # 不抛即成功
    finally:
        client._listen_task.cancel()


@pytest.mark.asyncio
async def test_quit_returns_on_connection_closed() -> None:
    """daemon 退出关连接：_listen set_exception(ConnectionError) → quit() 当成功返回。"""
    client = GameClient(port=1)
    req_id_box = {}

    async def hang_iter():
        await asyncio.sleep(3600)
        yield

    fake_ws = _make_fake_ws(hang_iter())
    client._ws = fake_ws
    client._listen_task = asyncio.create_task(client._listen())
    try:
        async def close_after_send():
            await asyncio.sleep(0.01)
            req_id_box["id"] = next(iter(client._pending))
            # 模拟 _listen finally：断连时对 pending future set_exception(ConnectionError)
            for fut in client._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Connection closed by server"))

        asyncio.create_task(close_after_send())
        await asyncio.wait_for(client.quit(timeout=2.0), timeout=2.0)  # 不抛即成功
    finally:
        client._listen_task.cancel()


@pytest.mark.asyncio
async def test_quit_raises_on_timeout() -> None:
    """既无响应也不断连 → quit() 超时抛 TimeoutError，且不在 _pending 留垃圾。"""
    client = GameClient(port=1)

    async def hang_iter():
        await asyncio.sleep(3600)
        yield

    fake_ws = _make_fake_ws(hang_iter())
    client._ws = fake_ws
    client._listen_task = asyncio.create_task(client._listen())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.quit(timeout=0.05)
        assert len(client._pending) == 0
    finally:
        client._listen_task.cancel()
```

> `_make_fake_ws(...)`：若 `test_client.py` 已有同类 helper（构造带 `send`/异步迭代的假 ws），复用它；没有则内联一个最小 fake：一个对象，`async def send(self, s): pass`，`__aiter__` 走传入的 async gen。`send` 默认成功（这三例不测发送期断连）。

- [ ] **Step 2: 跑测试确认失败**

委托 subagent：`python -m pytest python/tests/test_client.py -k quit -v`
Expected: FAIL —— `AttributeError: 'GameClient' object has no attribute 'quit'`。

- [ ] **Step 3: 实现 `GameClient.quit()`**

在 `client.py` `request()`（L263 `return response.get("result", {})`）之后追加：

```python
    async def quit(self, timeout: float = 5.0) -> None:
        """请 daemon 优雅退出（让 Movie Maker flush AVI 尾帧）。

        成功 = 收到响应，或响应到达前连接被 daemon 关闭（已退出）。两者都正常返回。
        仅当 timeout 内既无响应又未断连时抛 asyncio.TimeoutError，由上层降级 SIGTERM。
        不复用 request()——daemon quit 后大概率收不到响应，严格 await 会误判超时。
        """
        assert self._ws is not None, "Not connected"
        req_id = str(uuid.uuid4())[:8]
        msg = {"id": req_id, "method": "quit", "params": {}}
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._ws.send(json.dumps(msg))
        except websockets.ConnectionClosed:
            self._pending.pop(req_id, None)
            return  # 连接已关 = daemon 已退出 = 成功
        try:
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise
        except ConnectionError:
            # _listen 在断连时对 pending future set_exception(ConnectionError)
            # （client.py 的 _listen finally）= daemon 已退出 = 成功。
            return
```

- [ ] **Step 4: 跑测试确认通过**

委托 subagent：`python -m pytest python/tests/test_client.py -k quit -v`
Expected: PASS（3 个 quit 测试绿）。

- [ ] **Step 5: 提交**

```bash
git add python/godot_cli_control/client.py python/tests/test_client.py
git commit -m "feat(client): GameClient.quit() 断连即成功（#156 子问题 A）

发 quit RPC，收到响应或响应前断连都判成功；仅纯超时抛错。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: daemon `_graceful_quit` + `stop()` 改造

**Files:**
- Modify: `python/godot_cli_control/daemon.py`（顶部 import；`stop()` L313-352 插入；新增 `_graceful_quit` 方法 + 模块级 `_send_quit`）
- Test: `python/tests/test_daemon.py`

- [ ] **Step 1: 写失败的 pytest 测试**

在 `test_daemon.py` 末尾追加（沿用文件 monkeypatch 风格；`_isolate_registry` autouse 已隔离 registry；`GodotDaemon` 构造与现有 `test_stop_*` 一致）：

```python
def test_graceful_quit_returns_false_when_port_missing(tmp_path, monkeypatch):
    """无 port 文件 → 不尝试 RPC，直接降级（返回 False）。"""
    d = _make_daemon(tmp_path)  # 与现有 stop 测试同款构造 helper；若无则直接 GodotDaemon(...)
    # 不写 port_file
    assert d._graceful_quit(pid=4321) is False


def test_graceful_quit_true_when_process_dies(tmp_path, monkeypatch):
    """RPC 成功 + 进程在窗口内退出 → True。"""
    d = _make_daemon(tmp_path)
    d.port_file.write_text("9999")
    monkeypatch.setattr("godot_cli_control.daemon.asyncio.run", lambda coro: None)
    # coro 未被 await，关闭它避免 "coroutine never awaited" 警告
    monkeypatch.setattr("godot_cli_control.daemon._send_quit", lambda *a, **k: _noop_coro())
    monkeypatch.setattr("godot_cli_control.daemon._reap_if_dead", lambda pid: True)
    assert d._graceful_quit(pid=4321, graceful_timeout=0.5) is True


def test_graceful_quit_false_on_rpc_error(tmp_path, monkeypatch):
    """连不上 / RPC 抛错 → False（降级），不向上抛。"""
    d = _make_daemon(tmp_path)
    d.port_file.write_text("9999")
    monkeypatch.setattr("godot_cli_control.daemon._send_quit", lambda *a, **k: _noop_coro())

    def boom(coro):
        coro.close()
        raise ConnectionError("refused")

    monkeypatch.setattr("godot_cli_control.daemon.asyncio.run", boom)
    assert d._graceful_quit(pid=4321, graceful_timeout=0.5) is False


def test_graceful_quit_false_when_process_survives(tmp_path, monkeypatch):
    """RPC 成功但进程没在窗口内退出 → 超时 False。"""
    d = _make_daemon(tmp_path)
    d.port_file.write_text("9999")
    monkeypatch.setattr("godot_cli_control.daemon.asyncio.run", lambda coro: None)
    monkeypatch.setattr("godot_cli_control.daemon._send_quit", lambda *a, **k: _noop_coro())
    monkeypatch.setattr("godot_cli_control.daemon._reap_if_dead", lambda pid: False)
    assert d._graceful_quit(pid=4321, graceful_timeout=0.2) is False


def test_stop_graceful_success_skips_terminate(tmp_path, monkeypatch):
    """优雅退出成功时不调 _terminate。"""
    d = _make_daemon(tmp_path)
    d.pid_file.write_text("4321")
    monkeypatch.setattr("godot_cli_control.daemon._process_alive", lambda pid: True)
    monkeypatch.setattr("godot_cli_control.daemon._process_is_godot", lambda pid: True)
    monkeypatch.setattr(d, "_graceful_quit", lambda pid: True)
    called = {"terminate": 0}
    monkeypatch.setattr(d, "_terminate", lambda pid: called.__setitem__("terminate", 1))
    rc = d.stop()
    assert called["terminate"] == 0
    assert rc == 0


def test_stop_falls_back_to_terminate_on_graceful_failure(tmp_path, monkeypatch):
    """优雅退出失败时降级调 _terminate。"""
    d = _make_daemon(tmp_path)
    d.pid_file.write_text("4321")
    monkeypatch.setattr("godot_cli_control.daemon._process_alive", lambda pid: True)
    monkeypatch.setattr("godot_cli_control.daemon._process_is_godot", lambda pid: True)
    monkeypatch.setattr(d, "_graceful_quit", lambda pid: False)
    called = {"terminate": 0}
    monkeypatch.setattr(d, "_terminate", lambda pid: called.__setitem__("terminate", 1))
    d.stop()
    assert called["terminate"] == 1
```

测试辅助（放文件顶部 helper 区，若 `_make_daemon` 已存在则复用既有的）：

```python
async def _noop_coro() -> None:
    return None


def _make_daemon(tmp_path):
    """构造一个 control_dir 指向 tmp 的 GodotDaemon（与现有 stop 测试一致）。"""
    from godot_cli_control.daemon import GodotDaemon
    return GodotDaemon(project_root=tmp_path)
```

> 落地前先 `grep -n "_make_daemon\|GodotDaemon(" python/tests/test_daemon.py` 对齐既有构造方式（参数名 / 是否传 instance），避免重复定义冲突。

- [ ] **Step 2: 跑测试确认失败**

委托 subagent：`python -m pytest python/tests/test_daemon.py -k "graceful or stop_graceful or fall_back or falls_back" -v`
Expected: FAIL —— `AttributeError: ... has no attribute '_graceful_quit'`。

- [ ] **Step 3: 实现 daemon 侧**

`daemon.py` 顶部 import 区加（与现有 `import asyncio` 去重；若未导入则加）：

```python
import asyncio
```

模块级新增（放 `_transcode_movie` 之类模块函数附近）：

```python
async def _send_quit(port: int, timeout: float) -> None:
    """连 GameBridge 发 quit RPC。短连接超时——daemon 半死时快速失败降级。"""
    from .client import GameClient

    client = GameClient(port=port)
    try:
        await client.connect(
            retries=5, backoff=0.2, max_wait=0.5, open_timeout=1.0, total_timeout=2.0
        )
        await client.quit(timeout=timeout)
    finally:
        await client.disconnect()
```

`GodotDaemon` 内新增方法（放 `_terminate` L461 附近）：

```python
    def _graceful_quit(self, pid: int, graceful_timeout: float = 5.0) -> bool:
        """SIGTERM 之前先 RPC quit 优雅退出（让 Movie Maker flush AVI 尾帧），
        再轮询进程真正退出。

        任何失败——port 缺失 / 连不上 / RPC 超时 / 进程未在窗口内退出——一律返回
        False，由 stop() 降级 _terminate。绝不抛异常阻断 stop。
        """
        port = self._read_int(self.port_file)
        if port is None:
            return False
        try:
            asyncio.run(_send_quit(port, graceful_timeout))
        except Exception as e:  # noqa: BLE001 — 连不上/超时/协议错一律降级
            print(f"优雅退出失败，降级 SIGTERM：{e}", file=sys.stderr)
            return False
        # RPC 已请求退出，轮询进程消失（_reap_if_dead 正确回收 zombie 子进程，#67）
        deadline = time.time() + graceful_timeout
        while time.time() < deadline:
            if _reap_if_dead(pid):
                return True
            time.sleep(0.2)
        return False
```

`stop()`（L335-339 区，`print(f"关闭 Godot (PID {pid})...")` 之后、`self._terminate(pid)` 之前）改为：

```python
        print(f"关闭 Godot (PID {pid})...", file=sys.stderr)
        # 先尝试 RPC 优雅退出（flush AVI 尾帧，#156）；不通则降级 SIGTERM。
        if not self._graceful_quit(pid):
            self._terminate(pid)
        self._cleanup_state_files()
        print("Godot 已停止", file=sys.stderr)
```

- [ ] **Step 4: 跑测试确认通过**

委托 subagent：`python -m pytest python/tests/test_daemon.py -k "graceful or stop_graceful or fall_back or falls_back" -v`
Expected: PASS（6 个新测试绿，现有 stop 测试不回归）。

- [ ] **Step 5: 全量单测 + 覆盖率门禁**

委托 subagent：`coverage run -m pytest python/tests/ && coverage report`
Expected: 全绿；覆盖率 ≥ 80%（`fail_under=80`）。

- [ ] **Step 6: 提交**

```bash
git add python/godot_cli_control/daemon.py python/tests/test_daemon.py
git commit -m "feat(daemon): stop 先 RPC 优雅退出再降级 SIGTERM（#156 子问题 A）

_graceful_quit 发 quit RPC 后轮询进程退出，让 Movie Maker flush AVI
尾帧；port 缺失/连不上/超时/进程不退一律降级 _terminate，永不阻断 stop。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 本机真 e2e —— stop 后 AVI 尾帧不丢

**Files:**
- Modify: `python/tests/test_e2e_record.py`（追加一个测试，复用既有 `godot_project` fixture + `_ffprobe`）
- 依赖：本机真 Godot + 真显示（`GCC_GUI_E2E=1`）+ ffmpeg/ffprobe。

- [ ] **Step 1: 写 e2e 测试**

复用 `test_e2e_record.py` 既有结构（`_ffprobe` L121、`godot_project` fixture L136、`_FPS`），追加：

```python
def test_graceful_stop_preserves_tail_frames(godot_project: Path) -> None:
    """daemon stop 优雅退出后产物时长不缺尾段。

    旧 SIGTERM 路径会丢 Movie Maker AVI 写缓冲尾帧（实测 4-6s，短录制甚至产出
    ~150KB 静止画面）；优雅退出后产物时长应贴近录制窗口。复用既有 `_run_cli`
    （返回解析后的 JSON 信封 dict）/ `_ffprobe` / `_FPS` / `_WAIT_SECONDS`。
    """
    project = godot_project
    avi = project / "tail.avi"
    mp4 = avi.with_suffix(".mp4")

    start = _run_cli(
        project, "daemon", "start",
        "--record", "--movie-path", str(avi), "--fps", str(_FPS),
        timeout=120,
    )
    assert start["ok"] is True and start["result"]["started"], start

    try:
        # 按 game-time 推进录像帧（与 Movie Maker 帧对齐）
        wt = _run_cli(project, "wait-time", str(_WAIT_SECONDS))
        assert wt["ok"] is True, wt
    finally:
        # daemon stop → 优雅退出 flush 尾帧 + 转码
        stop = _run_cli(project, "daemon", "stop", timeout=60)
        assert stop["ok"] is True, stop
        assert stop["result"].get("rc") == 0, f"转码应成功：{stop}"

    assert mp4.exists() and mp4.stat().st_size > 0, f"应产出非空 mp4（avi 存在={avi.exists()}）"
    duration = float(_ffprobe(mp4).get("format", {}).get("duration", 0.0))
    # 尾帧完整守护：优雅退出后时长应贴近录制窗口；退回 SIGTERM 会丢 4-6s 尾帧使其显著偏短。
    assert duration >= _WAIT_SECONDS * 0.6, (
        f"产物 {duration:.2f}s 远低于录制 {_WAIT_SECONDS}s —— 疑似尾帧丢失（优雅退出未生效）"
    )
```

> 该测试与既有 `test_record_produces_valid_mp4`（L155，只验 `duration > 0`）共用 fixture/helper，增量是**对比录制窗口与产物时长**来专门守护尾帧。`* 0.6` 给编码 GOP / 转码取整留余量；落地后按本机实测微调（若 `_WAIT_SECONDS` 太短不足以区分尾帧丢失，可在本测试内临时拉长 wait-time）。

- [ ] **Step 2: 本机真跑（不可声称缺 GODOT_BIN 跳过）**

委托 subagent（`model: sonnet`，本机有 `~/.local/bin/godot` + ffmpeg）：
`GCC_GUI_E2E=1 GODOT_BIN=~/.local/bin/godot python -m pytest python/tests/test_e2e_record.py::test_graceful_stop_preserves_tail_frames -v`
Expected: PASS（产物时长 ≥ `_WAIT_SECONDS * 0.6`）。若 FAIL 且时长明显偏短 → 优雅退出未生效，回 Task 1/3 排查（确认 stop 日志没打 "SIGTERM 超时"）。

- [ ] **Step 3: 提交**

```bash
git add python/tests/test_e2e_record.py
git commit -m "test(e2e): daemon stop 优雅退出保住 AVI 尾帧（#156 子问题 A）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 文档同步（CHANGELOG + SKILL.md + 重渲染）

**Files:**
- Modify: `addons/godot_cli_control/CHANGELOG.md`（`[Unreleased]` 段）
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（录制章节）
- Regenerate: 仓内 `.claude/skills/godot-cli-control/SKILL.md` 等渲染副本

- [ ] **Step 1: CHANGELOG**

在 `addons/godot_cli_control/CHANGELOG.md` `[Unreleased]` 顶部加：

```markdown
- **`daemon stop` 优雅退出，录制尾帧不再丢**（#156 子问题 A）：`daemon stop` 现在在 SIGTERM 之前先经内部 `quit` RPC 让 Godot 正常退出，Movie Maker 在正常 quit 路径上 flush AVI 写缓冲——消除 SIGTERM 直杀丢 4-6s 尾帧的问题（下游录 demo 不必再垫 `wait(4.0)` 牺牲段）。RPC 不通（daemon 挂死 / 连不上 / 5s 超时 / 进程不退）自动无缝降级原 SIGTERM→SIGKILL 路径，退出码语义不变。`quit` 是 stop 的内部 RPC，不单列 CLI 子命令。需要新 addon RPC——老项目跑一次 `init` 同步。
```

- [ ] **Step 2: SKILL.md 模板**

`python/godot_cli_control/templates/skill/SKILL.md` 录制章节：弱化/删除「脚本末尾垫 `wait(4.0)` 牺牲尾段」的 workaround 说明，改述「`daemon stop` 已优雅退出，Movie Maker 尾帧安全；无需再垫牺牲段」。先 `grep -n "wait(4\|尾帧\|牺牲\|flush\|Movie\|tail" python/godot_cli_control/templates/skill/SKILL.md` 定位现有措辞再改。

- [ ] **Step 3: 验证模板渲染不崩 + init 注入**

委托 subagent：
```
python -c "from godot_cli_control import cli; print(cli.format_full_help()[:200])"
python -m pytest python/tests/test_skills_install.py -v
```
Expected: 不抛异常；`test_skills_install` 全绿。

- [ ] **Step 4: 重渲染仓内 skill 副本**

用 CI `skill-render-drift` job（`.github/workflows/ci.yml`）的官方修复命令（**Python 3.12 必须**——argparse 折行随小版本变；`COLUMNS=80` 锁宽度；版本行 CI 比对时归一化，本地 dev 版本号不影响）：

```bash
COLUMNS=80 python3.12 -c "from godot_cli_control import cli; from godot_cli_control.skills_install import render_skill; from godot_cli_control._version import version; open('.claude/skills/godot-cli-control/SKILL.md','w').write(render_skill(version, cli.format_full_help()))"
```

> 仓内只 track `.claude/skills/godot-cli-control/SKILL.md` 这一份渲染副本（无 `.codex` 渲染版），故直接写该路径而非 `install_skills`（后者会额外建 `.codex`）。改完 `git diff .claude/skills` 确认只是预期的录制章节变更。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/CHANGELOG.md python/godot_cli_control/templates/skill/SKILL.md .claude/skills
git commit -m "docs(156): CHANGELOG + SKILL.md 同步优雅退出（#156 子问题 A）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾（实施完成后，开 PR 前）

- [ ] **ruff lint**（本机 .venv 未必装 ruff，按 memory `ci-ruff-not-in-local-venv`）：`ruff check python/` 干净。
- [ ] **全量 GUT + pytest** 委托 subagent 跑一遍，确认无回归。
- [ ] **分诊收尾**（CLAUDE.md「实施收尾：先分诊，再开 Issue」）：盘点越界发现 / 未覆盖场景；能当场修的修，有后果的按门槛开 issue。
- [ ] **PR**：base `main`，单 PR（本仓串行 base main、不用 stacked PR），`gh pr merge --auto` 等 `ci-ok` 绿自动合。
- [ ] **#156 不关闭**：本 PR 只交付子问题 A，PR 描述里写明 B（macOS 冻帧）仍 open，留后续。
```
