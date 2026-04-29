"""L2 单元测试：覆盖 GameClient 的网络异常路径（L1 dogfooding 测不到的）。"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, patch

import pytest
import websockets

from godot_cli_control.client import GameClient


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
        yield  # noqa: unreachable

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
        yield  # noqa: unreachable

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
        yield  # noqa: unreachable

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
        yield  # noqa: unreachable

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
        yield  # noqa: unreachable

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
