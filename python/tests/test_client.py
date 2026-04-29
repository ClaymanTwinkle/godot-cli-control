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
