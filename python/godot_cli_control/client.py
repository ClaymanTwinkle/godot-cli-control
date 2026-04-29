"""GameClient - WebSocket client for connecting to Godot GameBridge."""

import asyncio
import base64
import json
import logging
import uuid
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

DEFAULT_PORT: int = 9877


class RpcError(RuntimeError):
    """Raised when GameBridge returns a JSON-RPC error response.

    保留服务端返回的 ``code`` 字段，方便上层（CLI ``--json`` 信封、调用方
    针对特定错误码 retry）做精确处理。继承 ``RuntimeError`` 以保持向后
    兼容——既有 ``except RuntimeError`` 仍能 catch。
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GameClient:
    """WebSocket client that connects to Godot's GameBridge service."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self._port = port
        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._listen_task: asyncio.Task | None = None

    async def __aenter__(self) -> "GameClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    async def connect(
        self,
        retries: int = 10,
        backoff: float = 1.0,
        max_wait: float = 3.0,
        open_timeout: float = 5.0,
        total_timeout: float | None = None,
    ) -> None:
        """Connect to GameBridge with exponential backoff retry.

        显式 ``proxy=None``：GameBridge 永远跑在 localhost，而 websockets>=14
        会自动读取 ``all_proxy``/``http_proxy``/``https_proxy`` 环境变量并把
        localhost 连接也塞进 SOCKS/HTTP 代理，导致 TCP 被代理立刻 EOF、
        抛 ``InvalidMessage: did not receive a valid HTTP response``。用户
        shell 里的 ``all_proxy=socks5://127.0.0.1:7897`` 就是典型触发点；
        显式传 ``proxy=None`` 比依赖 ``no_proxy`` 顺序更稳（no_proxy 对
        localhost 的匹配规则各库不一致）。

        URL 用 ``127.0.0.1`` 而非 ``localhost``：daemon 端 GameBridge.gd 是
        显式 ``listen(_port, "127.0.0.1")``（仅 v4），双栈 Linux 上 glibc
        解析 ``localhost`` 可能先返回 ``::1``，让 websockets 试 IPv6 命中
        没监听的 socket，回退到 IPv4 之间出现握手延迟甚至失败。直接走 v4
        字面量与监听地址、与 ``daemon._wait_port_ready`` 的探活地址三处对齐。

        ``open_timeout`` 限制单次 websockets handshake 时长，防止慢握手
        把累计 retry 时间拖到失控。``total_timeout`` 给整个 retry 循环
        加一道硬墙：默认 ``None`` 行为不变；非 ``None`` 时用
        ``asyncio.wait_for`` 包裹，超时统一抛 ``ConnectionError``。
        """
        if total_timeout is None:
            await self._connect_loop(retries, backoff, max_wait, open_timeout)
            return
        try:
            await asyncio.wait_for(
                self._connect_loop(retries, backoff, max_wait, open_timeout),
                timeout=total_timeout,
            )
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"Failed to connect within {total_timeout}s"
            ) from e

    async def _connect_loop(
        self,
        retries: int,
        backoff: float,
        max_wait: float,
        open_timeout: float,
    ) -> None:
        for attempt in range(retries):
            try:
                self._ws = await websockets.connect(
                    f"ws://127.0.0.1:{self._port}",
                    proxy=None,
                    open_timeout=open_timeout,
                )
                self._listen_task = asyncio.create_task(self._listen())
                logger.info("Connected to GameBridge on port %d", self._port)
                return
            except (
                ConnectionRefusedError,
                OSError,
                websockets.exceptions.InvalidMessage,
            ) as e:
                if attempt < retries - 1:
                    wait = min(backoff * (2**attempt), max_wait)
                    # 用 debug 而非 warning：daemon 启动期间的 retry 是预期行为，
                    # 不该污染 stderr —— CLI 默认 JSON 输出后 ``cmd 2>&1 | jq`` 会
                    # 把 retry 行混进去。最终 ConnectionError 仍然抛出，由 dispatcher
                    # 信封化到 stdout，agent 在那里能拿到完整失败信号。
                    logger.debug(
                        "Connection attempt %d failed: %s. Retrying in %.1fs...",
                        attempt + 1, e, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise ConnectionError(
                        f"Failed to connect after {retries} attempts"
                    ) from e

    async def disconnect(self) -> None:
        """Disconnect from GameBridge."""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Disconnected from GameBridge")

    async def _listen(self) -> None:
        """Background task that listens for messages from GameBridge.

        finally 里清空所有 pending future —— 涵盖 ConnectionClosed、
        CancelledError 以及任何其它退出路径，防止调用方的 await 在
        connection 消失后挂死到超时。
        """
        assert self._ws is not None
        close_reason: str | None = None
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    req_id = msg["id"]
                    if req_id in self._pending:
                        self._pending[req_id].set_result(msg)
                        del self._pending[req_id]
        except websockets.ConnectionClosed:
            close_reason = "Connection closed by server"
            logger.info(close_reason)
        finally:
            reason = close_reason or "listen task stopped"
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(reason))
            self._pending.clear()

    async def request(
        self, method: str, params: dict | None = None, timeout: float = 30.0
    ) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        assert self._ws is not None, "Not connected"
        req_id = str(uuid.uuid4())[:8]
        msg = {"id": req_id, "method": method, "params": params or {}}
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._ws.send(json.dumps(msg))
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise
        if "error" in response:
            err = response["error"]
            raise RpcError(
                int(err.get("code", -1)),
                str(err.get("message", "")),
            )
        return response.get("result", {})

    # ---- Low-level API ----

    async def click(self, path: str) -> dict:
        return await self.request("click", {"path": path})

    async def get_property(self, path: str, prop: str) -> Any:
        result = await self.request(
            "get_property", {"path": path, "property": prop}
        )
        return result.get("value")

    async def set_property(self, path: str, prop: str, value: Any) -> dict:
        return await self.request(
            "set_property", {"path": path, "property": prop, "value": value}
        )

    async def call_method(
        self, path: str, method: str, args: list | None = None
    ) -> Any:
        result = await self.request(
            "call_method",
            {"path": path, "method": method, "args": args or []},
        )
        return result.get("result")

    async def get_text(self, path: str) -> str:
        result = await self.request("get_text", {"path": path})
        return result.get("text", "")

    async def node_exists(self, path: str) -> bool:
        result = await self.request("node_exists", {"path": path})
        return result.get("exists", False)

    async def is_visible(self, path: str) -> bool:
        result = await self.request("is_visible", {"path": path})
        return result.get("visible", False)

    async def get_children(
        self, path: str, type_filter: str = ""
    ) -> list[dict]:
        result = await self.request(
            "get_children", {"path": path, "type_filter": type_filter}
        )
        return result.get("children", [])

    async def screenshot(self) -> bytes:
        """Take a screenshot and return PNG bytes."""
        result = await self.request("screenshot")
        return base64.b64decode(result.get("image", ""))

    async def get_scene_tree(self, depth: int = 5) -> dict:
        return await self.request("get_scene_tree", {"depth": depth})

    async def wait_for_node(self, path: str, timeout: float = 5.0) -> bool:
        result = await self.request(
            "wait_for_node",
            {"path": path, "timeout": timeout},
            timeout=timeout + 5.0,
        )
        return result.get("found", False)

    async def wait_game_time(self, seconds: float) -> dict:
        """按 Godot game time 等待 N 秒。

        Movie Maker (--write-movie) 模式下 wall time 比 game time 慢约 2-3×，
        客户端 timeout 用 seconds * 3 + 10 安全系数，与 combo() 一致。
        seconds <= 0 时客户端短路返回，不发 RPC。
        """
        if seconds <= 0:
            return {"success": True}
        return await self.request(
            "wait_game_time",
            {"seconds": seconds},
            timeout=seconds * 3.0 + 10.0,
        )

    # ---- Input simulation API ----

    async def action_press(self, action: str) -> dict:
        return await self.request("input_action_press", {"action": action})

    async def action_release(self, action: str) -> dict:
        return await self.request("input_action_release", {"action": action})

    async def action_tap(self, action: str, duration: float = 0.1) -> dict:
        return await self.request(
            "input_action_tap", {"action": action, "duration": duration}
        )

    async def hold(self, action: str, duration: float) -> dict:
        return await self.request(
            "input_hold", {"action": action, "duration": duration}
        )

    async def combo(self, steps: list[dict]) -> dict:
        total = sum(
            s.get("duration", 0) or s.get("wait", 0) for s in steps
        )
        # movie maker (--write-movie) 模式下 Godot 渲染比实时慢（大分辨率 +
        # MJPEG 编码开销），客户端墙钟等待需要 3× 安全系数 + 10s base，
        # 避免 combo 还没在游戏时间跑完就被 TimeoutError 打断。
        return await self.request(
            "input_combo", {"steps": steps}, timeout=total * 3.0 + 10.0
        )

    async def combo_cancel(self) -> dict:
        return await self.request("input_combo_cancel")

    async def release_all(self) -> dict:
        return await self.request("input_release_all")

    async def get_pressed(self) -> list[str]:
        """Return the list of input actions currently held by the simulator.

        Wraps the ``input_get_pressed`` RPC; server returns
        ``{"actions": [...]}`` (already the dedup'd union of momentary +
        timed-hold actions). Returns a fresh ``list[str]`` so callers can
        mutate without affecting the cached response.
        """
        result = await self.request("input_get_pressed")
        return [str(a) for a in result.get("actions", [])]

    async def list_input_actions(
        self, include_builtin: bool = False
    ) -> list[str]:
        """List InputMap actions defined in the running project.

        ``include_builtin=False`` (default) drops Godot's ``ui_*`` actions
        so the result is just the project's own actions — what an AI agent
        actually wants to see.
        """
        result = await self.request(
            "list_input_actions", {"include_builtin": include_builtin}
        )
        return [str(a) for a in result.get("actions", [])]
