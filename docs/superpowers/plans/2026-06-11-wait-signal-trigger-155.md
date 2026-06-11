# wait-signal `--trigger` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `wait-signal` 加 `--trigger '<subcommand>'`，同一连接内完成 `arm → 触发子命令 → 等信号/超时`，从协议层根除「先后台挂再触发」的竞态与三步 shell 模板。

**Architecture:** 方案 A（arm-ack 中间帧）。服务端 `wait_signal` 升级 `async_with_id`，`arm_ack:true` 时 `connect` 后先发 `{id, armed:true}` 进度帧；client `_listen` 识别 armed 帧并在 wait_signal 协程里 `await on_armed()`（执行 trigger，靠同连接复用现有 GameClient）；CLI 用 `build_parser` + `RPC_BY_NAME` 复用解析器执行 trigger 子命令。详见 `docs/superpowers/specs/2026-06-11-wait-signal-trigger-155-design.md`。

**Tech Stack:** GDScript（Godot 4 addon）、Python asyncio（GameClient）、argparse（CLI）、GUT（GDScript 单测）、pytest-asyncio（Python 单测）。

---

## 关键命名（跨 Task 必须一致）

| 符号 | 位置 | 作用 |
|---|---|---|
| `_send_armed(id)` | game_bridge.gd | 发 `{id, armed:true}` 中间帧（不动 `_in_flight`） |
| `_send_armed_callback` / `_send_response_callback` | wait_api.gd | setup 注入的两个 Callable |
| `self._armed_events: dict[str, asyncio.Event]` | client.py | req_id → armed 到达事件 |
| `_unwrap_response(msg)` | client.py | msg → result 或 raise RpcError（request 与 trigger 路径共用） |
| `_wait_signal_with_trigger(...)` | client.py | on_armed 路径的专用编排 |
| `on_armed` | client/cli | arm 完成后执行的 async 回调 |
| `_parse_trigger(s)` | cli.py | trigger 字符串 → `(RpcSpec, Namespace)`，失败 raise ValueError |
| `ns._trigger_spec` / `ns._trigger_ns` | cli.py | preflight 预解析缓存（同 `_preflight_combo` 的 `ns._combo_steps` 模式） |
| `trigger_result` | 信封 | 命中信封新增字段 = trigger 子命令的 result |

---

## File Structure

- **Modify** `addons/godot_cli_control/bridge/game_bridge.gd` — `wait_signal` kind 改 `async_with_id`、`_wait_api.setup(...)` 扩参、新增 `_send_armed`
- **Modify** `addons/godot_cli_control/bridge/wait_api.gd` — `setup` 扩参、`wait_signal_async` 改 `(params, id)` 回调式
- **Modify** `addons/godot_cli_control/tests/gut/test_wait_api.gd` — 适配回调式 + 新增 armed 帧测试
- **Modify** `python/godot_cli_control/client.py` — `_armed_events`、`_listen` 识别 armed、`_unwrap_response`、`_wait_signal_with_trigger`、`wait_signal(on_armed=...)`
- **Modify** `python/tests/test_client.py` — client 编排单测
- **Modify** `python/godot_cli_control/cli.py` — `--trigger` flag、`_parse_trigger`、`_preflight_wait_signal` 扩展、`cmd_wait_signal` 编排、RpcSpec extra_args、formatter
- **Modify** `python/tests/test_cli.py` / `test_cli_helpers.py` — preflight + 编排单测
- **Modify** `python/tests/e2e/...` — 本机真跑 e2e（沿用现有 e2e 结构）
- **Modify** SKILL.md 模板 + 渲染版 + CHANGELOG

---

## Task 1: 服务端 wait_signal 升级 async_with_id + armed 帧

**Files:**
- Modify: `addons/godot_cli_control/bridge/wait_api.gd`（`setup` ~31、`wait_signal_async` ~347）
- Modify: `addons/godot_cli_control/bridge/game_bridge.gd`（`_wait_api.setup` ~69、`_methods["wait_signal"]` ~234、新增 `_send_armed`）
- Test: `addons/godot_cli_control/tests/gut/test_wait_api.gd`

- [ ] **Step 1: 写失败的 GUT 测试（armed 帧 + 回调式终帧）**

`test_wait_api.gd` 现有 `wait_signal` 测试直接断言 `wait_signal_async(...)` 返回值。改为注入 spy 回调收集帧。在文件顶部 spy 基础设施（若无则加）：

```gdscript
# 收集服务端发出的帧：armed 中间帧走 _armed_ids，终帧走 _final_by_id
var _armed_ids: Array = []
var _final_by_id: Dictionary = {}

func _spy_send_armed(id: String) -> void:
    _armed_ids.append(id)

func _spy_send_response(id: String, result: Dictionary) -> void:
    _final_by_id[id] = result

func _wire_spies() -> void:
    _armed_ids = []
    _final_by_id = {}
    _api.setup(_api._read_property_fn, _spy_send_response, _spy_send_armed)
```

新增测试（arm_ack 发 armed + 终帧）：

```gdscript
func test_wait_signal_arm_ack_emits_armed_frame_then_final() -> void:
    _wire_spies()
    var emitter: Node = autofree(Node.new())
    emitter.add_user_signal("ping")
    get_tree().root.add_child(emitter)
    emitter.name = "PingEmitter155"
    # async_with_id：不返回，回调发帧
    _api.wait_signal_async({"path": "/root/PingEmitter155", "signal": "ping",
        "timeout": 2.0, "arm_ack": true}, "REQ1")
    await get_tree().process_frame  # 让 connect + armed 帧发出
    assert_eq(_armed_ids, ["REQ1"], "arm_ack 应先发一条 armed 帧")
    assert_false(_final_by_id.has("REQ1"), "未 emit 时不应有终帧")
    emitter.emit_signal("ping")
    await get_tree().process_frame
    assert_true(_final_by_id.has("REQ1"), "emit 后应发终帧")
    assert_true(_final_by_id["REQ1"]["emitted"], "终帧 emitted=true")
```

不传 arm_ack 零回归：

```gdscript
func test_wait_signal_without_arm_ack_emits_no_armed_frame() -> void:
    _wire_spies()
    var emitter: Node = autofree(Node.new())
    emitter.add_user_signal("ping")
    get_tree().root.add_child(emitter)
    emitter.name = "PingEmitter155B"
    _api.wait_signal_async({"path": "/root/PingEmitter155B", "signal": "ping",
        "timeout": 2.0}, "REQ2")
    await get_tree().process_frame
    assert_eq(_armed_ids, [], "不传 arm_ack 不发 armed 帧（零回归）")
    emitter.emit_signal("ping")
    await get_tree().process_frame
    assert_true(_final_by_id["REQ2"]["emitted"], "仍正常发终帧")
```

arm 阶段校验失败先发 error、不发 armed：

```gdscript
func test_wait_signal_arm_ack_node_missing_no_armed_frame() -> void:
    _wire_spies()
    _api.wait_signal_async({"path": "/root/NoSuch155", "signal": "x",
        "timeout": 1.0, "arm_ack": true}, "REQ3")
    await get_tree().process_frame
    assert_eq(_armed_ids, [], "节点不存在：armed 帧前就发 error，不发 armed")
    assert_true(_final_by_id["REQ3"].has("error"), "终帧是 error")
    assert_eq(_final_by_id["REQ3"]["error"]["code"], CliControlErrorCodes.NODE_NOT_FOUND)
```

适配现有 7 个 wait_signal 测试：把 `var result = await _api.wait_signal_async({...})` 改为 `_wire_spies(); _api.wait_signal_async({...}, "RID"); await ...; var result = _final_by_id["RID"]`（逐个改，断言不变）。

- [ ] **Step 2: 运行 GUT 看新测试失败**

Run（subagent，需 `GODOT_BIN`）：`python addons/godot_cli_control/tests/run_gut.py -gtest=test_wait_api.gd`
Expected: 新测试 FAIL（`wait_signal_async` 现签名 `(params)` 返回 dict，传 `(params, id)` 报参数不符 / spy 收不到帧）。

- [ ] **Step 3: 改 `wait_api.gd` setup 扩参**

```gdscript
# 注入：读属性、发终帧、发 armed 中间帧（后两者 async_with_id 用）
var _read_property_fn: Callable = Callable()
var _send_response_callback: Callable = Callable()
var _send_armed_callback: Callable = Callable()


func setup(read_property: Callable, send_response := Callable(), send_armed := Callable()) -> void:
    _read_property_fn = read_property
    _send_response_callback = send_response
    _send_armed_callback = send_armed
```

- [ ] **Step 4: 改 `wait_api.gd` `wait_signal_async` 为 `(params, id)` 回调式**

把现 `func wait_signal_async(params: Dictionary) -> Dictionary:` 整体改为（每个 `return X` → 回调发帧后 `return`）：

```gdscript
func wait_signal_async(params: Dictionary, id: String) -> void:
    var path: String = params.get("path", "") as String
    var node: Node = get_tree().root.get_node_or_null(path)
    if node == null:
        _send_response_callback.call(id, _node_not_found(path))
        return
    var signal_name: String = params.get("signal", "") as String
    if signal_name.is_empty():
        _send_response_callback.call(id, _err(CliControlErrorCodes.INVALID_PARAMS, "Missing 'signal' parameter"))
        return
    if not node.has_signal(signal_name):
        _send_response_callback.call(id, _err(CliControlErrorCodes.SIGNAL_NOT_FOUND, "Signal not found: %s" % signal_name))
        return
    var timeout_raw2: Variant = params.get("timeout", 5.0)
    if not (timeout_raw2 is int or timeout_raw2 is float):
        _send_response_callback.call(id, _err(CliControlErrorCodes.INVALID_PARAMS, "'timeout' must be a number"))
        return
    var timeout: float = float(timeout_raw2)
    if timeout < 0.0 or timeout > _MAX_WAIT_SECONDS:
        _send_response_callback.call(id, _err(CliControlErrorCodes.INVALID_PARAMS, "timeout must be 0..%s" % _MAX_WAIT_SECONDS))
        return
    var argc: int = 0
    for sig: Dictionary in node.get_signal_list():
        if sig["name"] == signal_name:
            argc = (sig["args"] as Array).size()
            break
    if argc > _SignalCapture.MAX_ARGS:
        _send_response_callback.call(id, _err(CliControlErrorCodes.INVALID_PARAMS,
            "signal '%s' has %d args (max %d supported)" % [signal_name, argc, _SignalCapture.MAX_ARGS]))
        return
    var capture: _SignalCapture = _SignalCapture.new()
    var cb: Callable = capture.callable_for(argc)
    node.connect(signal_name, cb, CONNECT_ONE_SHOT)
    # arm 完成同步点：connect 后才发 armed 帧（issue #155）
    if bool(params.get("arm_ack", false)) and _send_armed_callback.is_valid():
        _send_armed_callback.call(id)
    var start_ms: int = Time.get_ticks_msec()
    var reason: String = "timeout"
    while not capture.fired:
        if float(Time.get_ticks_msec() - start_ms) / 1000.0 >= timeout:
            break
        await get_tree().process_frame
        if not is_instance_valid(node):
            reason = "node_freed"
            break
    if not capture.fired:
        if is_instance_valid(node) and node.is_connected(signal_name, cb):
            node.disconnect(signal_name, cb)
        _send_response_callback.call(id, {"emitted": false, "reason": reason})
        return
    var encoded_args: Array = []
    for arg: Variant in capture.args:
        encoded_args.append(CliControlVariantCodec.encode(arg))
    _send_response_callback.call(id, {"emitted": true, "args": encoded_args})
```

- [ ] **Step 5: 改 `game_bridge.gd`（kind + setup + _send_armed）**

`_methods["wait_signal"]`（~234）：

```gdscript
_methods["wait_signal"] = {"callable": _wait_api.wait_signal_async, "kind": "async_with_id"}
```

`_wait_api.setup(...)`（~69）：

```gdscript
_wait_api.setup(_low_level_api._read_property, _on_async_response, _send_armed)
```

新增 `_send_armed`（放在 `_on_async_response` 附近，~358）：

```gdscript
# armed 中间帧（issue #155）：arm 完成同步点。不动 _in_flight —— 它不是终态响应，
# 终帧仍走 _on_async_response。client 据此在同连接发 trigger 子命令再等终帧。
func _send_armed(id: String) -> void:
    _send_json({"id": id, "armed": true})
```

- [ ] **Step 6: 运行 GUT 看全绿**

Run（subagent）：`python addons/godot_cli_control/tests/run_gut.py -gtest=test_wait_api.gd`
Expected: PASS（新 3 测试 + 适配后的 7 旧测试全绿）。

- [ ] **Step 7: Commit**

```bash
git add addons/godot_cli_control/bridge/wait_api.gd addons/godot_cli_control/bridge/game_bridge.gd addons/godot_cli_control/tests/gut/test_wait_api.gd
git commit -m "feat(155): wait_signal 升级 async_with_id + arm_ack armed 中间帧（服务端）"
```

---

## Task 2: Python client — on_armed 编排 + _listen armed 帧识别

**Files:**
- Modify: `python/godot_cli_control/client.py`（`__init__` ~97、`_listen` ~221、`request` ~242、`wait_signal` ~489）
- Test: `python/tests/test_client.py`

- [ ] **Step 1: 写失败的单测（armed → on_armed → 终帧 resolve）**

`test_client.py` 现有用 fake ws 的 pattern（沿用 `test_wait_signal_sends_correct_rpc_params` 附近的脚手架）。加：

```python
@pytest.mark.asyncio
async def test_wait_signal_trigger_runs_on_armed_between_armed_and_final() -> None:
    """arm_ack 路径：收 armed 帧 → 执行 on_armed → 收终帧才 resolve，
    且终帧带回 emitted/args。"""
    client = GameClient(port=1)
    # 用可控的双向通道模拟服务端：先回 armed 帧，待 on_armed 触发后回终帧
    sent: list[dict] = []
    armed_seen = asyncio.Event()

    class FakeWS:
        def __init__(self) -> None:
            self._q: asyncio.Queue = asyncio.Queue()
        async def send(self, raw: str) -> None:
            msg = json.loads(raw)
            sent.append(msg)
            if msg["method"] == "wait_signal":
                await self._q.put({"id": msg["id"], "armed": True})
            if msg["method"] == "input_action_tap":
                # trigger 成功 → 触发 wait_signal 终帧
                self._tap_id = msg["id"]
                await self._q.put({"id": msg["id"], "result": {"tapped": True}})
                await self._q.put({"id": self._ws_signal_id,
                                   "result": {"emitted": True, "args": [42]}})
        def __aiter__(self): return self
        async def __anext__(self) -> str:
            item = await self._q.get()
            return json.dumps(item)

    fake = FakeWS()
    client._ws = fake  # type: ignore[assignment]
    # 记录 wait_signal 的 id 供 FakeWS 回终帧
    orig_send = fake.send
    async def tracking_send(raw: str) -> None:
        msg = json.loads(raw)
        if msg["method"] == "wait_signal":
            fake._ws_signal_id = msg["id"]
        await orig_send(raw)
    fake.send = tracking_send  # type: ignore
    client._listen_task = asyncio.ensure_future(client._listen())

    triggered: list[bool] = []
    async def on_armed() -> None:
        triggered.append(True)
        await client.request("input_action_tap", {"action": "interact"})

    result = await client.wait_signal("/root/A", "ping", timeout=2.0, on_armed=on_armed)
    assert triggered == [True]
    assert result == {"emitted": True, "args": [42]}
    client._listen_task.cancel()
```

加 trigger 失败短路测试：

```python
@pytest.mark.asyncio
async def test_wait_signal_trigger_failure_propagates_and_stops_waiting() -> None:
    """on_armed 抛 RpcError → wait_signal 传播该异常、不再等信号。"""
    client = GameClient(port=1)
    # FakeWS 只回 armed 帧；on_armed 直接抛
    ...（同上脚手架，但 on_armed: async def -> raise RpcError(1001, "boom")）
    with pytest.raises(RpcError) as ei:
        await client.wait_signal("/root/A", "ping", timeout=2.0, on_armed=on_armed)
    assert ei.value.code == 1001
```

（注：脚手架细节按 test_client.py 现有 fake-ws helper 对齐；若已有 `make_fake_client` 之类工具优先复用。）

- [ ] **Step 2: 运行看失败**

Run（subagent）：`.venv/bin/python -m pytest python/tests/test_client.py -k wait_signal_trigger -v`
Expected: FAIL（`wait_signal()` 不接受 `on_armed` → TypeError）。

- [ ] **Step 3: 改 `client.py` — `__init__` 加 `_armed_events`**

`__init__`（~97，`self._pending` 旁）：

```python
self._armed_events: dict[str, asyncio.Event] = {}
```

- [ ] **Step 4: 改 `_listen` 识别 armed 帧**

`_listen` 的 `async for` 体（~221）：

```python
async for raw in self._ws:
    msg = json.loads(raw)
    if "id" in msg:
        req_id = msg["id"]
        # issue #155 armed 中间帧：有 id、有 armed、无 result/error → 不 resolve
        if msg.get("armed") and req_id in self._armed_events:
            self._armed_events[req_id].set()
            continue
        if req_id in self._pending:
            self._pending[req_id].set_result(msg)
            del self._pending[req_id]
```

- [ ] **Step 5: 抽 `_unwrap_response` 并让 `request` 复用**

新增 staticmethod（放 `request` 上方）：

```python
@staticmethod
def _unwrap_response(response: dict) -> dict:
    if "error" in response:
        err = response["error"]
        raise RpcError(int(err.get("code", -1)), str(err.get("message", "")))
    return response.get("result", {})
```

`request` 末尾改用它：

```python
        return self._unwrap_response(response)
```

- [ ] **Step 6: 加 `_wait_signal_with_trigger` 并改 `wait_signal`**

`wait_signal`（~489）：

```python
async def wait_signal(
    self, path: str, signal: str, timeout: float = 5.0,
    on_armed: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    """等信号发射（issue #96）。返回 {"emitted":bool,"args":[...]}，超时不抛。

    on_armed（issue #155）：传入则走 arm_ack 路径——收到服务端 armed 帧后
    await on_armed()（同连接执行 trigger），再等终帧。
    """
    if on_armed is None:
        return await self.request(
            "wait_signal", {"path": path, "signal": signal, "timeout": timeout},
            timeout=timeout + 5.0,
        )
    return await self._wait_signal_with_trigger(path, signal, timeout, on_armed)


async def _wait_signal_with_trigger(
    self, path: str, signal: str, timeout: float,
    on_armed: Callable[[], Awaitable[None]],
) -> dict:
    assert self._ws is not None, "Not connected"
    req_id = str(uuid.uuid4())[:8]
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict] = loop.create_future()
    armed = asyncio.Event()
    self._pending[req_id] = future
    self._armed_events[req_id] = armed
    try:
        await self._ws.send(json.dumps({
            "id": req_id, "method": "wait_signal",
            "params": {"path": path, "signal": signal, "timeout": timeout, "arm_ack": True},
        }))
        armed_task = loop.create_task(armed.wait())
        done, _ = await asyncio.wait(
            {armed_task, future}, timeout=timeout + 5.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if future in done:
            # arm 阶段就出终帧（通常是 error：节点/信号不存在）→ 不触发
            armed_task.cancel()
            return self._unwrap_response(future.result())
        if not armed.is_set():
            armed_task.cancel()
            raise asyncio.TimeoutError
        # armed 到达：同连接执行 trigger（失败抛 → 传播 → 停止等信号）
        await on_armed()
        response = await asyncio.wait_for(future, timeout=timeout + 5.0)
        return self._unwrap_response(response)
    finally:
        self._pending.pop(req_id, None)
        self._armed_events.pop(req_id, None)
```

确认 `client.py` 顶部已 import：`from collections.abc import Awaitable, Callable`（无则加）。

- [ ] **Step 7: 运行看通过**

Run（subagent）：`.venv/bin/python -m pytest python/tests/test_client.py -k wait_signal -v`
Expected: PASS（新 2 测试 + 现有 wait_signal 测试不回归）。

- [ ] **Step 8: Commit**

```bash
git add python/godot_cli_control/client.py python/tests/test_client.py
git commit -m "feat(155): GameClient.wait_signal on_armed 编排 + _listen armed 帧识别"
```

---

## Task 3: CLI — --trigger flag + _parse_trigger + cmd_wait_signal 编排

**Files:**
- Modify: `python/godot_cli_control/cli.py`（`_preflight_wait_signal` ~447、`cmd_wait_signal` ~774、`_fmt_wait_signal_text` ~966、wait-signal RpcSpec ~1621；新增 `_register_wait_signal_args` / `_parse_trigger`）
- Test: `python/tests/test_cli.py`、`python/tests/test_cli_helpers.py`

- [ ] **Step 1: 写失败的 preflight 单测**

`test_cli.py`（preflight 段）新增：

```python
class TestWaitSignalTriggerPreflight:
    def _ns(self, **kw):
        base = {"timeout": None, "trigger": None, "node_path": "/root/A",
                "signal_name": "ping"}
        base.update(kw)
        return type("NS", (), base)()

    def test_valid_trigger_caches_spec_and_ns(self) -> None:
        from godot_cli_control import cli
        ns = self._ns(trigger="tap interact")
        cli._preflight_wait_signal(ns)
        assert ns._trigger_spec.name == "tap"
        assert ns._trigger_ns.cmd == "tap"

    def test_empty_trigger_rejected(self) -> None:
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="   "))

    def test_non_rpc_trigger_rejected(self) -> None:
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="daemon start"))

    def test_nested_wait_trigger_rejected(self) -> None:
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="wait-signal /root/B x"))

    def test_trigger_subcommand_preflight_runs(self) -> None:
        # combo 无 steps → 其自身 preflight 抛 → 包成 wait-signal 的 ValueError
        from godot_cli_control import cli
        with pytest.raises(ValueError):
            cli._preflight_wait_signal(self._ns(trigger="combo"))
```

- [ ] **Step 2: 运行看失败**

Run（subagent）：`.venv/bin/python -m pytest python/tests/test_cli.py -k WaitSignalTriggerPreflight -v`
Expected: FAIL（`_preflight_wait_signal` 不认 `trigger` / 无 `_parse_trigger`）。

- [ ] **Step 3: 加 `_parse_trigger` + 扩 `_preflight_wait_signal`**

`cli.py` 顶部 import 区加 `import shlex`（按字母序）。新增（放 `_preflight_wait_signal` 上方）：

```python
def _parse_trigger(trigger: str) -> tuple[RpcSpec, argparse.Namespace]:
    """把 --trigger 字符串解析成 (RpcSpec, ns) 并跑其自身 preflight。

    复用主 parser（契约 #4：trigger 是真正的 CLI 子命令，不是 shell 透传）。
    非法 / 非 RPC / 嵌套 wait-* / 子命令 preflight 失败一律 raise ValueError
    → 上层 EXIT_USAGE / -1003 / 64。
    """
    parts = shlex.split(trigger)
    if not parts:
        raise ValueError("--trigger 不能为空")
    parser = build_parser()
    try:
        parsed = parser.parse_args(parts)
    except SystemExit as e:
        raise ValueError(f"--trigger 解析失败: {trigger!r}") from e
    cmd = getattr(parsed, "cmd", None)
    if cmd not in RPC_BY_NAME:
        raise ValueError(f"--trigger 必须是 RPC 子命令，不支持 {cmd!r}（daemon/run/init 不可作触发）")
    if cmd.startswith("wait"):
        raise ValueError(f"--trigger 不能嵌套 wait-* 子命令（{cmd}）")
    spec = RPC_BY_NAME[cmd]
    if spec.preflight is not None:
        spec.preflight(parsed)  # 抛 ValueError 直接上浮
    return spec, parsed


def _preflight_wait_signal(ns: argparse.Namespace) -> None:
    if ns.timeout is not None:
        timeout = _require_float(ns.timeout, "wait-signal", "timeout")
        if not 0 <= timeout <= 3600:
            raise ValueError(f"wait-signal: timeout 必须在 0..3600 秒，收到 {timeout}")
    trigger = getattr(ns, "trigger", None)
    if trigger:
        # 预解析缓存（同 _preflight_combo 的 ns._combo_steps 模式），handler 直接用
        ns._trigger_spec, ns._trigger_ns = _parse_trigger(trigger)
```

- [ ] **Step 4: 运行看 preflight 通过**

Run（subagent）：`.venv/bin/python -m pytest python/tests/test_cli.py -k WaitSignalTriggerPreflight -v`
Expected: PASS。

- [ ] **Step 5: 写失败的 cmd_wait_signal 编排单测**

`test_cli_helpers.py`（`test_cmd_wait_signal_matched_passes_timeout` 附近）新增：

```python
def test_cmd_wait_signal_trigger_invokes_handler_and_attaches_result() -> None:
    """有 trigger：on_armed 调 trigger_spec.handler(client, trigger_ns)，
    命中信封带 trigger_result。"""
    client = object()
    trigger_spec = SimpleNamespace(
        handler=AsyncMock(return_value={"tapped": True}))
    trigger_ns = SimpleNamespace(cmd="tap")
    ns = _ns(node_path="/root/A", signal_name="ping", timeout=None,
             trigger="tap interact")
    ns._trigger_spec = trigger_spec
    ns._trigger_ns = trigger_ns

    captured = {}
    async def fake_wait_signal(path, signal, timeout, on_armed=None):
        captured["on_armed"] = on_armed
        await on_armed()  # 模拟 armed 帧到达
        return {"emitted": True, "args": [1]}
    client_obj = SimpleNamespace(wait_signal=fake_wait_signal)

    result = _run(cli.cmd_wait_signal(client_obj, ns))
    trigger_spec.handler.assert_awaited_once_with(client_obj, trigger_ns)
    assert result == {"emitted": True, "args": [1], "trigger_result": {"tapped": True}}


def test_cmd_wait_signal_no_trigger_unchanged() -> None:
    client_obj = SimpleNamespace(
        wait_signal=AsyncMock(return_value={"emitted": True, "args": []}))
    ns = _ns(node_path="/root/A", signal_name="ping", timeout=None, trigger=None)
    result = _run(cli.cmd_wait_signal(client_obj, ns))
    assert result == {"emitted": True, "args": []}
    client_obj.wait_signal.assert_awaited_once_with("/root/A", "ping", timeout=5.0)
```

（`_ns` 需含新字段 `trigger`——参见 memory「test_cli Namespace mock 缺字段坑」，给 mock 补 `trigger`。）

- [ ] **Step 6: 运行看失败**

Run（subagent）：`.venv/bin/python -m pytest python/tests/test_cli_helpers.py -k wait_signal -v`
Expected: FAIL（`cmd_wait_signal` 未处理 trigger）。

- [ ] **Step 7: 改 `cmd_wait_signal`**

```python
async def cmd_wait_signal(client: GameClient, ns: argparse.Namespace) -> dict:
    timeout = float(ns.timeout) if ns.timeout else 5.0
    if not getattr(ns, "trigger", None):
        return await client.wait_signal(ns.node_path, ns.signal_name, timeout=timeout)
    trigger_spec: RpcSpec = ns._trigger_spec  # preflight 已解析缓存
    trigger_ns: argparse.Namespace = ns._trigger_ns
    box: dict = {}

    async def on_armed() -> None:
        box["result"] = await trigger_spec.handler(client, trigger_ns)

    result = dict(await client.wait_signal(
        ns.node_path, ns.signal_name, timeout=timeout, on_armed=on_armed))
    result["trigger_result"] = box.get("result")
    return result
```

- [ ] **Step 8: 加 `--trigger` flag（extra_args）+ 挂到 RpcSpec + formatter**

新增 registrar（放 `_register_wait_frames_args` 附近）：

```python
def _register_wait_signal_args(p: argparse.ArgumentParser) -> None:
    """wait-signal 的 --trigger：arm 完成后同连接执行的一条 RPC 子命令（issue #155）。"""
    p.add_argument(
        "--trigger", default=None,
        help="arm 后在同一连接内执行的一条 RPC 子命令，如 --trigger 'tap interact'；"
             "多步用 combo。消除『先后台挂再触发』的竞态。",
    )
```

wait-signal RpcSpec（~1621）加 `extra_args=_register_wait_signal_args,` 并更新 description 末尾：在现 description 尾加一句 `带 --trigger 时同连接 arm→触发→等，无需 shell 后台。`。

`_fmt_wait_signal_text`（~966）带 trigger_result 时附注：

```python
def _fmt_wait_signal_text(r: dict) -> str:
    base = "emitted" if r.get("emitted") else "timeout"
    if "trigger_result" in r:
        return f"{base} (trigger ok)"
    return base
```

- [ ] **Step 9: 运行 CLI 单测全绿**

Run（subagent）：`.venv/bin/python -m pytest python/tests/test_cli.py python/tests/test_cli_helpers.py -q`
Expected: PASS（含 memory 提醒的 test_cli 全套，确认 Namespace mock 补了 `trigger` 字段无 AttributeError 回归）。

- [ ] **Step 10: Commit**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py python/tests/test_cli_helpers.py
git commit -m "feat(155): wait-signal --trigger 解析复用 + cmd 编排 + preflight（CLI）"
```

---

## Task 4: e2e + 文档 + 收尾

**Files:**
- Modify: `python/tests/e2e/`（沿用现有 e2e fixture：`godot_daemon` / `bridge`，或 CLI 子进程 e2e 的现有写法）
- Modify: `python/godot_cli_control/templates/skill/SKILL.md` + `.claude/skills/godot-cli-control/SKILL.md`
- Modify: `addons/godot_cli_control/CHANGELOG.md`

- [ ] **Step 1: 写 e2e（本机真跑）**

在现有 e2e 套件加一例：起 daemon、用 CLI 子进程跑
`wait-signal /root/<emitter> <signal> --timeout 3 --trigger 'emit-signal /root/<emitter> <signal>'`（或 `tap` 命中一个由按键触发的信号），断言 exit 0 + 信封 `emitted:true` + `trigger_result` 存在。参考现有 e2e 对 daemon + CLI 子进程的封装（不要新造框架）。先确认现有 e2e 文件结构再落点。

- [ ] **Step 2: 运行 e2e（subagent，禁后台，本机真跑）**

Run（subagent，`GODOT_BIN`/`.venv` 就绪）：对应 e2e 节点 `-v`。
Expected: PASS。macOS 冷启动 flake 见 memory，必要时 `--lf` 重跑。

- [ ] **Step 3: 更新 SKILL.md（模板 + 渲染版同步）**

把 wait-signal 的「先后台挂再触发」pitfall 段升级：保留无 `--trigger` 时的竞态说明，新增 `--trigger` 一等公民用法示例
（`wait-signal /root/Area door_opened --timeout 3 --trigger 'tap interact'`）。模板与 `.claude/skills/...` 渲染版做**相同的字面修改**（cli_help 区不动则无 3.12 折行风险；若改了命令表/usage 则用 `COLUMNS=80` + Python 3.12 重渲染）。

- [ ] **Step 4: CHANGELOG**

`addons/godot_cli_control/CHANGELOG.md` 的 `[Unreleased] / ### Added` 顶部加一条：`--trigger` 同连接 arm→触发→等，消灭 shell 后台三步模板与竞态；需新 addon 能力（arm_ack armed 帧）→ 老项目 `init` 同步。

- [ ] **Step 5: 全量验证 + drift**

Run（subagent）：`coverage run -m pytest`（覆盖率门槛 80）、`uvx ruff check python/`、本地 Python 3.12 复现 `skill-render-drift`。
Expected: 全绿、覆盖率 ≥80、drift 空。

- [ ] **Step 6: Commit + PR**

```bash
git add -A
git commit -m "feat(155): e2e + SKILL.md/CHANGELOG 同步 wait-signal --trigger"
# PR：base main，Closes #155
```

---

## Self-Review

- **Spec coverage**：① CLI 表面 → Task 3 Step 8；② 协议 arm_ack/armed 帧 → Task 1；③ client 编排 → Task 2；④ 错误/退出码（arm 失败/trigger 解析/trigger 运行期）→ Task 1 Step1(arm err) + Task 3 Step1(解析) + Task 2 Step1(运行期短路传播，经 `_run_rpc` 收口成 ok:false/exit 1）；⑤ 测试三层 → Task 1/2/3 + Task 4 e2e；⑥ addon 同步 + 文档 → Task 4。无缺口。
- **Placeholder 扫描**：Task 2 Step1 第二个测试的脚手架以 `...` 省略——执行时复用第一个测试的 FakeWS 脚手架，仅 on_armed 改为 `raise RpcError`；这是「重复已给代码」而非未定义内容。其余步骤代码完整。
- **Type 一致**：`_send_armed`/`_send_armed_callback`/`_armed_events`/`_unwrap_response`/`_wait_signal_with_trigger`/`_parse_trigger`/`ns._trigger_spec`/`ns._trigger_ns`/`trigger_result` 跨 Task 命名统一（见顶部命名表）。
- **退出码自洽**：trigger 运行期 RpcError 经 `_run_rpc` 的 `except Exception` → `_rpc_failure_envelope` → ok:false + exit（RPC 错=1）；命中走 `_exit_from_wait_signal`（emitted=0/否则1）；trigger 解析失败走 `_preflight` → EXIT_USAGE/64。三者不撞。
