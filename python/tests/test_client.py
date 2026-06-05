"""L2 单元测试：覆盖 GameClient 的网络异常路径（L1 dogfooding 测不到的）。"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, patch

import pytest
import websockets

from godot_cli_control.client import LONG_OP_CLIENT_TIMEOUT, GameClient


# ---- Test 1: proxy=None 显式传给 websockets.connect ----

@pytest.mark.asyncio
async def test_connect_passes_proxy_none_explicitly() -> None:
    """SOCKS 代理防御：connect() 必须显式传 proxy=None。

    防 regression：未来 maintainer 改回 default 会让 all_proxy=socks5://...
    用户的 localhost 连接被代理拦截，client.py docstring 写过这个坑。
    """
    fake_ws = AsyncMock()
    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ) as mock_connect:
        client = GameClient(port=9999)
        try:
            await client.connect(retries=1)
        finally:
            # 防 listen task 泄漏
            if client._listen_task:
                client._listen_task.cancel()
        # 断言 connect 被调用且 proxy=None 在 kwargs 中
        assert mock_connect.called
        _, kwargs = mock_connect.call_args
        assert kwargs.get("proxy") is None, \
            "GameClient.connect() must pass proxy=None to websockets.connect"


@pytest.mark.asyncio
async def test_connect_uses_ipv4_literal_not_localhost() -> None:
    """URL 必须是 ``ws://127.0.0.1:...``，不能用 hostname。

    daemon 端 GameBridge.gd 显式 listen 在 127.0.0.1，IPv6 优先的解析器把
    ``localhost`` 解为 ``::1`` 时会连到无人监听的 socket。client / server /
    探活三处必须都走 v4 字面量。
    """
    fake_ws = AsyncMock()
    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ) as mock_connect:
        client = GameClient(port=9876)
        try:
            await client.connect(retries=1)
        finally:
            if client._listen_task:
                client._listen_task.cancel()
        url = mock_connect.call_args.args[0]
        assert url == "ws://127.0.0.1:9876", \
            f"expected v4 literal URL, got {url!r}"


# ---- Test 2: connect retry 行为 ----

@pytest.mark.asyncio
async def test_connect_retries_on_connection_refused() -> None:
    """前 N 次 ConnectionRefused 后第 N+1 次成功 → connect() 应该返回成功。"""
    fake_ws = AsyncMock()
    side_effects = [ConnectionRefusedError("nope"), ConnectionRefusedError("nope"), fake_ws]
    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(side_effect=side_effects),
    ) as mock_connect:
        client = GameClient(port=9999)
        try:
            await client.connect(retries=5, backoff=0.01, max_wait=0.01)
        finally:
            if client._listen_task:
                client._listen_task.cancel()
        assert mock_connect.call_count == 3


# ---- Test 3: connect retry 全失败 → ConnectionError ----

@pytest.mark.asyncio
async def test_connect_raises_after_retries_exhausted() -> None:
    """所有 retry 失败应该抛 ConnectionError（带原异常 from clause）。"""
    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(side_effect=ConnectionRefusedError("always nope")),
    ):
        client = GameClient(port=9999)
        with pytest.raises(ConnectionError, match="Failed to connect after"):
            await client.connect(retries=2, backoff=0.01, max_wait=0.01)


# ---- Test 4: _listen 退出时清空 pending futures（避免 await 挂死） ----

@pytest.mark.asyncio
async def test_listen_clears_pending_on_disconnect() -> None:
    """_listen 退出（连接关闭）时所有 pending future 必须被 set_exception，
    否则调用方的 await client.request() 会永久挂死到 timeout。"""
    fake_ws = AsyncMock()

    async def fake_iter():
        # iterator 立即结束（模拟连接关闭）
        return
        yield  # 让函数成为 async generator；return 在前，这行永远到不了

    fake_ws.__aiter__ = lambda self: fake_iter()
    fake_ws.close = AsyncMock()

    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ):
        client = GameClient(port=9999)
        await client.connect(retries=1)

        # 注入一个 pending future
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        client._pending["fake_id"] = future

        # 等 listen task 自然退出（iterator 已空）
        if client._listen_task:
            await client._listen_task

        # pending 应该被清空 + future 应该被 set_exception
        assert "fake_id" not in client._pending
        assert future.done()
        with pytest.raises(ConnectionError):
            future.result()


# ---- Test 5: request() timeout 时清理 _pending ----

@pytest.mark.asyncio
async def test_request_timeout_cleans_pending() -> None:
    """request() 超时不应该在 _pending 里留垃圾 entry。"""
    fake_ws = AsyncMock()

    async def hang_iter():
        # async iterator 永久挂起：不会 yield 也不会结束，
        # 模拟"连接活着但不回响应"——request() 必定走 timeout 路径。
        await asyncio.Event().wait()
        yield  # 让函数成为 async generator；return 在前，这行永远到不了

    fake_ws.__aiter__ = lambda self: hang_iter()
    fake_ws.send = AsyncMock()
    fake_ws.close = AsyncMock()

    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ):
        client = GameClient(port=9999)
        await client.connect(retries=1)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await client.request("nonexistent_method", timeout=0.05)
            # 超时后 _pending 应该不再含这次 request 的 id
            assert len(client._pending) == 0
        finally:
            if client._listen_task:
                client._listen_task.cancel()
                try:
                    await client._listen_task
                except asyncio.CancelledError:
                    pass


# ---- P2-6 Test A: connect 把 open_timeout 透传给 websockets.connect ----

@pytest.mark.asyncio
async def test_connect_passes_open_timeout() -> None:
    """单次 websockets handshake 必须有 open_timeout，避免慢握手拖死累计 retry。"""
    fake_ws = AsyncMock()
    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ) as mock_connect:
        client = GameClient(port=9999)
        try:
            await client.connect(retries=1, open_timeout=2.5)
        finally:
            if client._listen_task:
                client._listen_task.cancel()
        assert mock_connect.called
        _, kwargs = mock_connect.call_args
        assert kwargs.get("open_timeout") == 2.5, \
            "GameClient.connect() must forward open_timeout to websockets.connect"


# ---- P2-6 Test B: total_timeout 给整个 retry 循环加硬墙 ----

@pytest.mark.asyncio
async def test_connect_total_timeout_aborts() -> None:
    """所有 retry 都失败时 total_timeout 必须把循环切断，统一抛 ConnectionError。"""
    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(side_effect=ConnectionRefusedError("nope")),
    ):
        client = GameClient(port=9999)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(ConnectionError, match="within"):
            await client.connect(
                retries=1000,
                backoff=0.05,
                max_wait=0.05,
                total_timeout=0.1,
            )
        elapsed = loop.time() - t0
        assert elapsed < 0.5, f"total_timeout 没生效，耗时 {elapsed:.3f}s"


# ---- Test 6（bonus）: websockets.connect 有 proxy kwarg（防版本退化） ----

def test_websockets_connect_supports_proxy_kwarg() -> None:
    """websockets>=14 才有 proxy= kwarg；如果版本退化测试就 fail。

    spec §7.3 风险表 grounding：mock 写法依赖此 kwarg 存在。
    """
    sig = inspect.signature(websockets.connect)
    assert "proxy" in sig.parameters, \
        f"websockets.connect missing 'proxy' kwarg; version {websockets.__version__} too old"


# ---- 0.2.0：RpcError 必须保留服务端 code ----


@pytest.mark.asyncio
async def test_connect_retries_log_at_debug_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """daemon 启动期间的 connection retry 是预期行为，不应该污染 stderr。

    新增于 0.2.0：CLI 默认 JSON-on-stdout 后，``cmd 2>&1 | jq`` 这种常见管道会
    把 retry 行混进去破坏解析。把 logger.warning 降级到 logger.debug，
    最终 ConnectionError 仍然抛出由 dispatcher 信封化。
    """
    import logging

    caplog.set_level(logging.DEBUG, logger="godot_cli_control.client")
    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(side_effect=ConnectionRefusedError("nope")),
    ):
        client = GameClient(port=9999)
        with pytest.raises(ConnectionError):
            await client.connect(retries=2, backoff=0.01, max_wait=0.01)

    # retry 行应该有 —— 但只在 DEBUG 级别
    debug_msgs = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG
        and "Connection attempt" in r.getMessage()
    ]
    assert debug_msgs, "应记录至少一条 retry debug 日志"

    warning_msgs = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "Retrying" in r.getMessage()
    ]
    assert not warning_msgs, (
        f"retry 不应在 WARNING+ 级别打印（会污染 AI 的 jq 管道），"
        f"实际看到：{[r.getMessage() for r in warning_msgs]}"
    )


@pytest.mark.asyncio
async def test_request_raises_rpc_error_with_code() -> None:
    """GD 端 ``_send_error(id, code, msg)`` 返回的 code 不能在客户端被丢掉 ——
    CLI ``--json`` 信封要把它转给 agent 做精确处理（如 1004 combo-in-progress 重试）。"""
    from godot_cli_control.client import RpcError

    fake_ws = AsyncMock()

    async def hang_iter():
        # 让 listen task 保持活着 —— 我们手动通过 _pending 注入响应。
        await asyncio.Event().wait()
        yield  # 让函数成为 async generator；return 在前，这行永远到不了

    fake_ws.__aiter__ = lambda self: hang_iter()
    fake_ws.send = AsyncMock()
    fake_ws.close = AsyncMock()

    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ):
        client = GameClient(port=9999)
        await client.connect(retries=1)
        try:
            # 拿到 request 内部生成的 id 后注入一个错误响应
            loop = asyncio.get_running_loop()

            async def inject_error_after_send():
                await asyncio.sleep(0.01)
                req_id = next(iter(client._pending))
                client._pending[req_id].set_result(
                    {
                        "id": req_id,
                        "error": {"code": 1004, "message": "combo in progress"},
                    }
                )

            loop.create_task(inject_error_after_send())
            with pytest.raises(RpcError) as exc_info:
                await client.request("input_action_press", timeout=2.0)
            assert exc_info.value.code == 1004
            assert exc_info.value.message == "combo in progress"
            # RuntimeError 子类：老代码 except RuntimeError 仍能 catch
            assert isinstance(exc_info.value, RuntimeError)
        finally:
            if client._listen_task:
                client._listen_task.cancel()
                try:
                    await client._listen_task
                except asyncio.CancelledError:
                    pass


@pytest.mark.asyncio
async def test_get_pressed_unwraps_actions_field() -> None:
    """``get_pressed()`` 必须把 GD 返回的 ``{"actions":[...]}`` 解成 list[str]。"""
    fake_ws = AsyncMock()

    async def hang_iter():
        # 让 listen task 保持活着 —— 我们手动通过 _pending 注入响应。
        await asyncio.Event().wait()
        yield  # 让函数成为 async generator；return 在前，这行永远到不了

    fake_ws.__aiter__ = lambda self: hang_iter()
    fake_ws.send = AsyncMock()
    fake_ws.close = AsyncMock()

    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ):
        client = GameClient(port=9999)
        await client.connect(retries=1)
        try:
            loop = asyncio.get_running_loop()

            async def inject_response():
                await asyncio.sleep(0.01)
                req_id = next(iter(client._pending))
                client._pending[req_id].set_result(
                    {"id": req_id, "result": {"actions": ["jump", "attack"]}}
                )

            loop.create_task(inject_response())
            actions = await client.get_pressed()
            assert actions == ["jump", "attack"]
        finally:
            if client._listen_task:
                client._listen_task.cancel()
                try:
                    await client._listen_task
                except asyncio.CancelledError:
                    pass


@pytest.mark.asyncio
async def test_list_input_actions_passes_include_builtin_param() -> None:
    """``list_input_actions(include_builtin=True)`` 必须把参数透传给 RPC，
    否则 GD 端默认过滤 ui_*。"""
    fake_ws = AsyncMock()

    async def hang_iter():
        # 让 listen task 保持活着 —— 我们手动通过 _pending 注入响应。
        await asyncio.Event().wait()
        yield  # 让函数成为 async generator；return 在前，这行永远到不了

    fake_ws.__aiter__ = lambda self: hang_iter()
    fake_ws.send = AsyncMock()
    fake_ws.close = AsyncMock()

    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ):
        client = GameClient(port=9999)
        await client.connect(retries=1)
        try:
            loop = asyncio.get_running_loop()

            async def inject_response():
                await asyncio.sleep(0.01)
                req_id = next(iter(client._pending))
                client._pending[req_id].set_result(
                    {"id": req_id, "result": {"actions": ["jump"]}}
                )

            loop.create_task(inject_response())
            actions = await client.list_input_actions(include_builtin=True)
            assert actions == ["jump"]
            # 校验 send 出去的 payload 里 include_builtin=True
            sent_raw = fake_ws.send.call_args.args[0]
            import json as _json
            sent = _json.loads(sent_raw)
            assert sent["method"] == "list_input_actions"
            assert sent["params"] == {"include_builtin": True}
        finally:
            if client._listen_task:
                client._listen_task.cancel()
                try:
                    await client._listen_task
                except asyncio.CancelledError:
                    pass


@pytest.mark.asyncio
async def test_get_scene_tree_returns_truncate_metadata() -> None:
    """服务端在节点超限时返回 {tree, truncated, total_nodes}，client 透传。"""
    import godot_cli_control.client as client_mod

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


@pytest.mark.asyncio
async def test_get_scene_tree_omits_max_nodes_when_none() -> None:
    """max_nodes=None 时 RPC params 不应携带该 key，保留旧客户端兼容路径。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["params"] = params
        return {"tree": {"name": "root", "type": "Node", "path": "/root"}}

    client = client_mod.GameClient(port=1)
    monkeypatch_target = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.get_scene_tree(depth=2)
    finally:
        client_mod.GameClient.request = monkeypatch_target
    assert captured["params"] == {"depth": 2}
    assert "max_nodes" not in captured["params"]


# ---- issue #45: wait_game_time / combo 不能给 game-time 操作设 wall-time 上限 ----
#
# 旧公式 seconds*3+10 假设 wall ≤ 3× game，在 Movie Maker (--write-movie) 模式下
# 实测 wall ≈ 4-5× game，必假超时（bridge.wait(15) → 55s timeout < 60s+ wall）。
# 治本：去掉 seconds-scaled wall 上限，固定一个生死线，死连接靠 ws ping/pong。
#
# 测试用「两次差距大的 seconds 拿到同一 timeout」直接证明 decoupling，
# 比单点等值断言更稳——后者跟「巧合 == 常量」的假阳性形态无法区分。


@pytest.mark.asyncio
async def test_wait_game_time_client_timeout_decoupled_from_seconds() -> None:
    """issue #45: wait_game_time 客户端 timeout 必须与 seconds 解耦。"""
    import godot_cli_control.client as client_mod

    captured: list = []

    async def fake_request(self, method, params=None, timeout=30.0):
        captured.append(timeout)
        return {"success": True}

    client = client_mod.GameClient(port=1)
    monkeypatch_target = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.wait_game_time(1.0)
        await client.wait_game_time(120.0)
    finally:
        client_mod.GameClient.request = monkeypatch_target
    assert captured == [LONG_OP_CLIENT_TIMEOUT, LONG_OP_CLIENT_TIMEOUT], (
        f"wait_game_time 应固定使用 {LONG_OP_CLIENT_TIMEOUT}s 生死线、与 seconds 无关，"
        f"实际 {captured}"
    )


@pytest.mark.asyncio
async def test_combo_client_timeout_decoupled_from_steps_total() -> None:
    """issue #45: combo() 与 wait_game_time 同源问题，client timeout 同样与 total 解耦。"""
    import godot_cli_control.client as client_mod

    captured: list = []

    async def fake_request(self, method, params=None, timeout=30.0):
        captured.append(timeout)
        return {"success": True}

    client = client_mod.GameClient(port=1)
    monkeypatch_target = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.combo([{"action": "x", "duration": 0.5}])
        await client.combo([{"action": "x", "duration": 60.0}])
    finally:
        client_mod.GameClient.request = monkeypatch_target
    assert captured == [LONG_OP_CLIENT_TIMEOUT, LONG_OP_CLIENT_TIMEOUT]


@pytest.mark.asyncio
async def test_get_properties_sends_correct_rpc_params() -> None:
    """``get_properties`` 必须发出 method="get_properties"、params={"path": ..., "properties": [...]}。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"values": {}}

    client = client_mod.GameClient(port=1)
    monkeypatch_target = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.get_properties("/root/Player", ["position", "visible"])
    finally:
        client_mod.GameClient.request = monkeypatch_target
    assert captured["method"] == "get_properties"
    assert captured["params"] == {"path": "/root/Player", "properties": ["position", "visible"]}


@pytest.mark.asyncio
async def test_get_properties_unwraps_values_to_bare_value_map() -> None:
    """服务端 ``{"values": {prop: {"value": ..., "type"?: ...}}}`` 必须被解包成
    ``{prop: 裸value}`` 映射；非 dict 容错分支（如 ``"weird": 42``）也需透传。"""
    import godot_cli_control.client as client_mod

    async def fake_request(self, method, params=None, timeout=30.0):
        return {
            "values": {
                "position": {"value": [1, 2], "type": "Vector2"},
                "visible": {"value": True},
                "weird": 42,  # 非 dict，容错：直接透传
            }
        }

    client = client_mod.GameClient(port=1)
    monkeypatch_target = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.get_properties("/root/Player", ["position", "visible", "weird"])
    finally:
        client_mod.GameClient.request = monkeypatch_target
    assert result == {"position": [1, 2], "visible": True, "weird": 42}


@pytest.mark.asyncio
async def test_connect_locks_ws_ping_keepalive() -> None:
    """issue #45 治本依赖：去掉 wall 上限后，死连接靠 ws ping/pong 检测。

    显式锁住 ping_interval / ping_timeout（与 websockets 默认对齐），
    防库升级把默认改了导致死连接卡 600s。
    """
    fake_ws = AsyncMock()
    with patch(
        "godot_cli_control.client.websockets.connect",
        new=AsyncMock(return_value=fake_ws),
    ) as mock_connect:
        client = GameClient(port=9999)
        try:
            await client.connect(retries=1)
        finally:
            if client._listen_task:
                client._listen_task.cancel()
        _, kwargs = mock_connect.call_args
        assert kwargs.get("ping_interval") == 20, \
            "GameClient.connect() must lock ping_interval (ws keepalive)"
        assert kwargs.get("ping_timeout") == 20, \
            "GameClient.connect() must lock ping_timeout (ws keepalive)"


# ---- issue #96: wait_property / wait_signal / wait_frames client 包装 ----


@pytest.mark.asyncio
async def test_wait_property_sends_correct_rpc_params() -> None:
    """wait_property 必须发出 method="wait_property"，参数全部透传（含 op/tolerance）。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        captured["timeout"] = timeout
        return {"matched": True, "value": 500, "waited": 0.12}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.wait_property(
            "/root/Player", "position:x", 500,
            op="gt", timeout=3.0, tolerance=0.5,
        )
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "wait_property"
    assert captured["params"] == {
        "path": "/root/Player", "property": "position:x", "value": 500,
        "op": "gt", "timeout": 3.0, "tolerance": 0.5,
    }
    # client 侧 timeout = server timeout + 5s grace
    assert captured["timeout"] == 3.0 + 5.0
    assert result == {"matched": True, "value": 500, "waited": 0.12}


@pytest.mark.asyncio
async def test_wait_signal_sends_correct_rpc_params() -> None:
    """wait_signal 必须发出 method="wait_signal"，超时 = server timeout + 5s。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        captured["timeout"] = timeout
        return {"emitted": True, "args": []}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.wait_signal("/root/Area", "door_opened", timeout=4.0)
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "wait_signal"
    assert captured["params"] == {"path": "/root/Area", "signal": "door_opened", "timeout": 4.0}
    assert captured["timeout"] == 4.0 + 5.0
    assert result == {"emitted": True, "args": []}


@pytest.mark.asyncio
async def test_wait_frames_sends_correct_rpc_params_and_calculates_timeout() -> None:
    """wait_frames 的 client timeout = max(30, frames/10 + 10)。"""
    import godot_cli_control.client as client_mod

    captured: list = []

    async def fake_request(self, method, params=None, timeout=30.0):
        captured.append({"method": method, "params": params, "timeout": timeout})
        return {"success": True, "frames": params.get("frames", 0)}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        # 3 frames: max(30, 3/10+10)=max(30, 10.3)=30
        await client.wait_frames(3)
        # 300 frames: max(30, 300/10+10)=max(30, 40)=40
        await client.wait_frames(300, physics=True)
    finally:
        client_mod.GameClient.request = orig

    assert captured[0]["method"] == "wait_frames"
    assert captured[0]["params"] == {"frames": 3, "physics": False}
    assert captured[0]["timeout"] == 30.0

    assert captured[1]["params"] == {"frames": 300, "physics": True}
    assert captured[1]["timeout"] == 40.0


# ---- issue #98: scene_reload / scene_change client 包装 ----


@pytest.mark.asyncio
async def test_scene_reload_sends_correct_rpc_params() -> None:
    """scene_reload 必须发出 method="scene_reload"，params={"timeout": 10.0}，RPC timeout=15.0。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        captured["timeout"] = timeout
        return {"scene_path": "res://Main.tscn", "name": "Main"}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.scene_reload()
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "scene_reload"
    assert captured["params"] == {"timeout": 10.0}
    assert captured["timeout"] == 10.0 + 5.0
    assert result == {"scene_path": "res://Main.tscn", "name": "Main"}


@pytest.mark.asyncio
async def test_scene_reload_custom_timeout_sends_correct_rpc_params() -> None:
    """scene_reload(timeout=3.0) 发出 params={"timeout": 3.0}，RPC timeout=8.0。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        captured["timeout"] = timeout
        return {"scene_path": "res://Main.tscn", "name": "Main"}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.scene_reload(timeout=3.0)
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "scene_reload"
    assert captured["params"] == {"timeout": 3.0}
    assert captured["timeout"] == 3.0 + 5.0


@pytest.mark.asyncio
async def test_scene_change_sends_correct_rpc_params() -> None:
    """scene_change 必须发出 method="scene_change"，params={"path": ..., "timeout": ...}，RPC timeout=timeout+5。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        captured["timeout"] = timeout
        return {"scene_path": "res://a.tscn", "name": "A"}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.scene_change("res://a.tscn", timeout=3.0)
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "scene_change"
    assert captured["params"] == {"path": "res://a.tscn", "timeout": 3.0}
    assert captured["timeout"] == 3.0 + 5.0
    assert result == {"scene_path": "res://a.tscn", "name": "A"}


# ---- issue #102: time_scale / pause / unpause / step_frames client 包装 ----


@pytest.mark.asyncio
async def test_time_scale_no_arg_sends_empty_params() -> None:
    """time_scale() 无参时 params 必须是 {}（不带 "value" 键）——GDScript 用 has("value") 区分读写。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        captured["timeout"] = timeout
        return {"time_scale": 1.0}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.time_scale()
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "time_scale"
    assert captured["params"] == {}, "无参时 params 必须是空字典，不得含 'value' 键"
    assert captured["timeout"] == 30.0, "time_scale 不应传自定义 RPC timeout（用默认值）"
    assert result == {"time_scale": 1.0}


@pytest.mark.asyncio
async def test_time_scale_with_value_sends_value_param() -> None:
    """time_scale(2.5) 发出 params={"value": 2.5}。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"time_scale": 2.5}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.time_scale(2.5)
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "time_scale"
    assert captured["params"] == {"value": 2.5}


@pytest.mark.asyncio
async def test_pause_sends_correct_rpc() -> None:
    """pause() 发出 method="pause"，params={}。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"paused": True}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.pause()
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "pause"
    assert captured["params"] == {}
    assert result == {"paused": True}


@pytest.mark.asyncio
async def test_unpause_sends_correct_rpc() -> None:
    """unpause() 发出 method="unpause"，params={}。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"paused": False}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.unpause()
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "unpause"
    assert captured["params"] == {}
    assert result == {"paused": False}


@pytest.mark.asyncio
async def test_step_frames_sends_correct_params_and_calculates_timeout() -> None:
    """step_frames 的 client timeout = max(30, frames/10 + 10)。"""
    import godot_cli_control.client as client_mod

    captured: list = []

    async def fake_request(self, method, params=None, timeout=30.0):
        captured.append({"method": method, "params": params, "timeout": timeout})
        return {"stepped": params.get("frames", 0), "paused": True}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        # 5 frames: max(30, 5/10+10)=max(30, 10.5)=30
        await client.step_frames(5)
        # 600 frames: max(30, 600/10+10)=max(30, 70)=70
        await client.step_frames(600, physics=True)
    finally:
        client_mod.GameClient.request = orig

    assert captured[0]["method"] == "step_frames"
    assert captured[0]["params"] == {"frames": 5, "physics": False}
    assert captured[0]["timeout"] == 30.0

    assert captured[1]["method"] == "step_frames"
    assert captured[1]["params"] == {"frames": 600, "physics": True}
    assert captured[1]["timeout"] == 70.0


# ---- issue #101: sprite_info / screenshot --node client 包装 ----


@pytest.mark.asyncio
async def test_sprite_info_sends_path() -> None:
    """sprite_info 发出 method="sprite_info"，params={"path": ...}，原样返回聚合。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"type": "Sprite2D", "frame": 2}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.sprite_info("/root/Game/Sprite")
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "sprite_info"
    assert captured["params"] == {"path": "/root/Game/Sprite"}
    assert result == {"type": "Sprite2D", "frame": 2}


@pytest.mark.asyncio
async def test_screenshot_without_node_keeps_empty_params() -> None:
    """screenshot() 旧契约不破：params={}（不带 node 键），返回解码后的 bytes。"""
    import base64 as _b64

    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"image": _b64.b64encode(b"\x89PNGdata").decode()}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        data = await client.screenshot()
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "screenshot"
    assert captured["params"] == {}, "无 node 时不得带 node 键（老 addon 兼容）"
    assert data == b"\x89PNGdata"


@pytest.mark.asyncio
async def test_screenshot_with_node_sends_node_param() -> None:
    """screenshot(node=...) 发出 params={"node": ...}。"""
    import base64 as _b64

    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["params"] = params
        return {"image": _b64.b64encode(b"crop").decode(), "region": [1, 2, 3, 4]}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        data = await client.screenshot(node="/root/S")
    finally:
        client_mod.GameClient.request = orig
    assert captured["params"] == {"node": "/root/S"}
    assert data == b"crop"


@pytest.mark.asyncio
async def test_screenshot_raw_exposes_region() -> None:
    """screenshot_raw 返回原始响应（含 region），供 CLI 信封透出裁剪框。"""
    import base64 as _b64

    import godot_cli_control.client as client_mod

    async def fake_request(self, method, params=None, timeout=30.0):
        return {"image": _b64.b64encode(b"crop").decode(), "region": [10, 20, 30, 40]}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        raw = await client.screenshot_raw("/root/S")
    finally:
        client_mod.GameClient.request = orig
    assert raw["region"] == [10, 20, 30, 40]


# ---- issue #103: errors 增量查询 client 包装 ----


@pytest.mark.asyncio
async def test_errors_sends_since_and_limit() -> None:
    """errors(since=42, limit=10) 发出 method="errors"，params 完整。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"errors": [], "marker": 42, "dropped": 0, "truncated": False}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        result = await client.errors(since=42, limit=10)
    finally:
        client_mod.GameClient.request = orig
    assert captured["method"] == "errors"
    assert captured["params"] == {"since": 42, "limit": 10}
    assert result["marker"] == 42


@pytest.mark.asyncio
async def test_errors_defaults() -> None:
    """errors() 默认 since=0 limit=100。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["params"] = params
        return {"errors": [], "marker": 0, "dropped": 0, "truncated": False}

    client = client_mod.GameClient(port=1)
    orig = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.errors()
    finally:
        client_mod.GameClient.request = orig
    assert captured["params"] == {"since": 0, "limit": 100}
