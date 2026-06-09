"""GameClient - WebSocket client for connecting to Godot GameBridge."""

import asyncio
import base64
import json
import logging
import os
import uuid
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

DEFAULT_PORT: int = 9877

# 长操作（wait_game_time / combo）的客户端 wall-time 生死线。
#
# 这类操作的「完成」是 game-time 维度（Godot 帧数推进），与 wall time 关系不可
# 预测：Movie Maker (--write-movie) 模式下 wall ≈ 4-5× game，且随分辨率/fps/盘速
# 漂移。任何 seconds-scaled 公式都会在某个组合下假超时（issue #45）。
#
# 改用固定大值后：正常完成时 server 自然回包；server 真死循环时由 600s 兜底
# （而不是 55s 把还活着的录像炸掉）；死连接由 websockets 库的 ping/pong 心跳
# 在 ~40s 内独立检测，与本上限无关。
LONG_OP_DEFAULT_TIMEOUT: float = 600.0


def _resolve_long_op_timeout() -> float:
    """长操作生死线，可被 ``GODOT_CLI_LONG_OP_TIMEOUT`` env 覆盖（issue #69）。

    默认 ``LONG_OP_DEFAULT_TIMEOUT``（600s）。合法录像 > 10min 等少数长操作可
    设环境变量调高（如 ``GODOT_CLI_LONG_OP_TIMEOUT=1800``）。非正数 / 非数字一律
    忽略并回退默认——这是个兜底生死线，绝不能因 env 写错被设成 0 把正常操作秒杀。
    """
    raw = os.environ.get("GODOT_CLI_LONG_OP_TIMEOUT")
    if not raw:
        return LONG_OP_DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "GODOT_CLI_LONG_OP_TIMEOUT=%r 不是数字，回退默认 %.0fs",
            raw, LONG_OP_DEFAULT_TIMEOUT,
        )
        return LONG_OP_DEFAULT_TIMEOUT
    if value <= 0:
        logger.warning(
            "GODOT_CLI_LONG_OP_TIMEOUT=%r 必须 > 0，回退默认 %.0fs",
            raw, LONG_OP_DEFAULT_TIMEOUT,
        )
        return LONG_OP_DEFAULT_TIMEOUT
    return value


# 模块级解析一次：进程生命周期内 env 不变；测试需要时改 env 后调
# ``_resolve_long_op_timeout()`` 拿新值，或直接给 request() 传 timeout。
LONG_OP_CLIENT_TIMEOUT: float = _resolve_long_op_timeout()


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

    def __init__(self, port: int | None = None, instance: str | None = None) -> None:
        # port=None（无显式端口）→ 从 .cli_control/ auto-discover，找不到再回退
        # DEFAULT_PORT。daemon 默认 OS 自动分配端口，所以照 README 写
        # ``GameClient()`` 单连接脚本的用户/agent 必须经由这条发现路径才能连上
        # （issue #91）。发现逻辑收敛在 daemon.discover_port，CLI 走同一入口。
        # 延迟 import 避免 client 在 package import 时拉起 daemon→registry 这条
        # 较重的依赖链。
        #
        # instance：命名实例名（如 "server"/"client"）。仅在 port=None 时透传给
        # discover_port；显式 port 全权优先，instance 不触发任何 IO。
        # InstanceAmbiguityError 不吞，直接抛给库用户——run 脚本作者需自己决定
        # 连哪个实例（传 instance= 消歧）。
        if port is None:
            from .daemon import discover_port

            port = discover_port(instance=instance) or DEFAULT_PORT
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
                    # 显式锁住 ws 心跳——issue #45 治本依赖：long ops 去掉
                    # seconds-scaled wall 上限后，死连接靠这层独立检测。
                    # 与 websockets 当前默认对齐，防库升级悄悄改默认。
                    ping_interval=20,  # 每 20s 无流量发一次 ping
                    ping_timeout=20,   # pong 20s 未回 → 关连接（≈40s 内可发现死链）
                    # 放开库默认 1MB 消息上限（issue #149）：hiDPI 全屏截图的
                    # base64 轻松超 1MB，库会以 close 1009 关连接 → 误报 -1001
                    # 连接错，症状像随机失败。daemon 是 localhost 上自家进程
                    # （outbound 默认 10MB 封顶），信任其输出。screenshot 新协议
                    # 已走服务端落盘，这里兜旧 addon base64 回退 + bytes API。
                    max_size=None,
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
        except websockets.ConnectionClosed as e:
            # 带上 close code/reason（issue #149 排查教训）：1009 (message too
            # big) 之类的根因不能被笼统的 "Connection closed" 吞掉——否则
            # 症状呈现为随机连接失败，下游对着 -1001 排查极贵。str(e) 形如
            # "received 1009 (message too big) ...; then sent ..."。
            close_reason = f"Connection closed by server: {e}"
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

    async def get_properties(self, path: str, props: list[str]) -> dict[str, Any]:
        """同帧原子读多个属性（issue #100），返回 {prop: 裸 value} 映射。

        type 字段是给 CLI JSON 信封的（agent 消费）；Python 层要 type 时直接
        ``await client.request("get_properties", ...)`` 拿原始 result。
        """
        result = await self.request(
            "get_properties", {"path": path, "properties": list(props)}
        )
        return {
            k: (v.get("value") if isinstance(v, dict) else v)
            for k, v in result.get("values", {}).items()
        }

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

    async def find_nodes(
        self,
        node_type: str | None = None,
        text: str | None = None,
        text_contains: str | None = None,
        name_pattern: str | None = None,
        from_path: str | None = None,
        limit: int = 20,
    ) -> dict:
        """服务端单次遍历搜索节点（issue #153），替代客户端 children+get_text
        递归 BFS（程序化匿名 UI ``@Button@12`` 唯一可行的定位方式；录制模式下
        每个 RPC 等帧渲染，几十次往返折成一次）。

        过滤器 AND 语义，至少给一个：``node_type``（按类继承匹配，也认
        class_name 脚本类）、``text``（text 属性精确）与 ``text_contains``
        （子串）二选一、``name_pattern``（节点名通配 ``*``/``?``，大小写敏感）。
        ``from_path`` 限定子树（默认全树 /root，含 autoload 与弹窗）；
        ``limit`` 默认 20（服务端上限 500），超出附 ``truncated: true``
        （tree 同款信号）。返回 ``{"matches": [{name,type,path,text?,visible?}],
        "truncated"?}``，matches 按 BFS 浅层优先排序。未设的过滤器不进
        params——服务端按 key 缺失判断未启用。"""
        params: dict = {"limit": limit}
        if node_type:
            params["type"] = node_type
        if text:
            params["text"] = text
        if text_contains:
            params["text_contains"] = text_contains
        if name_pattern:
            params["name_pattern"] = name_pattern
        if from_path:
            params["from"] = from_path
        return await self.request("find_nodes", params)

    async def screenshot(self, node: str | None = None) -> bytes:
        """Take a screenshot and return PNG bytes.

        ``node``（issue #101）：传节点绝对路径时按该节点的屏幕 AABB 裁剪，
        产出小图供像素级断言。节点在屏幕外 → 1011；算不出边界 → 1010。
        只要裁剪 region 信息时用 :meth:`screenshot_raw`。
        """
        result = await self.screenshot_raw(node)
        return base64.b64decode(result.get("image", ""))

    async def screenshot_raw(
        self, node: str | None = None, path: str | None = None
    ) -> dict:
        """screenshot 的原始响应。

        ``region`` 仅在传 ``node`` 时出现，是实际裁剪到的视口像素矩形
        （已与视口求交，可能小于节点 AABB）。CLI 信封需要它，故与
        :meth:`screenshot` 的 bytes 便捷形式拆开。

        ``path``（issue #149）：传**绝对路径**让 daemon 直接把 PNG 落盘
        （同机，localhost-only），响应只回 ``{"path", "bytes"}`` 元数据，
        base64 不过 WS——大图不再受消息大小限制。父目录须已存在（CLI /
        bridge 调用前已 mkdir）。旧 addon 不认识该参数，会照旧返回
        ``{"image": <base64>}``，调用方需兜底（CLI 与 bridge 都有 fallback）。
        """
        params: dict = {}
        if node:
            params["node"] = node
        if path:
            params["path"] = path
        return await self.request("screenshot", params)

    async def sprite_info(self, path: str) -> dict:
        """渲染态聚合查询（issue #101）：Sprite2D / AnimatedSprite2D / TextureRect。

        返回 texture/图集区域/翻转/帧号/modulate/visible 聚合；非 sprite 类
        节点 → 1010。headless 下可用（纯属性读，不依赖真渲染）。
        """
        return await self.request("sprite_info", {"path": path})

    async def errors(self, since: int = 0, limit: int = 100) -> dict:
        """结构化 push_error / push_warning 增量查询（issue #103）。

        返回 ``{"errors": [...], "marker": int, "dropped": int, "truncated": bool}``；
        ``since`` 传上次的 ``marker`` 只看新增，``limit=0`` 是纯基线查询
        （拿 marker 不取数据）。需 Godot 4.5+（Logger API），老引擎 → 1012。
        """
        return await self.request("errors", {"since": since, "limit": limit})

    async def get_scene_tree(
        self, depth: int = 5, max_nodes: int | None = None, path: str | None = None
    ) -> dict:
        """读取场景树（可选软上限、可选子树根）。

        ``max_nodes``：``None`` 走服务端默认（硬墙 5000，无 ``truncated`` 字段）；
        传正整数 N 时，节点数超 N 时响应附加 ``{"truncated": true, "total_nodes": M}``。
        服务端会把传入值 clamp 到 5000 上限，超过仍走 1005 错误。

        ``path``（issue #150）：``None`` 时根为当前场景（``current_scene``）；
        传绝对节点路径（如 ``/root/GameUI``）则以该节点为子树根，从 ``/root``
        解析（与 ``get_children`` 同世界观）；路径不存在时服务端返回 1001。
        """
        params: dict = {"depth": depth}
        if max_nodes is not None:
            params["max_nodes"] = max_nodes
        if path is not None:
            params["path"] = path
        return await self.request("get_scene_tree", params)

    async def wait_for_node(self, path: str, timeout: float = 5.0) -> bool:
        result = await self.request(
            "wait_for_node",
            {"path": path, "timeout": timeout},
            timeout=timeout + 5.0,
        )
        return result.get("found", False)

    async def wait_property(
        self,
        path: str,
        prop: str,
        value: Any,
        op: str = "eq",
        timeout: float = 5.0,
        tolerance: float = 0.0,
    ) -> dict:
        """等属性满足条件（issue #96）。返回 {"matched": bool, ...}，超时不抛。"""
        return await self.request(
            "wait_property",
            {
                "path": path, "property": prop, "value": value,
                "op": op, "timeout": timeout, "tolerance": tolerance,
            },
            timeout=timeout + 5.0,
        )

    async def wait_signal(self, path: str, signal: str, timeout: float = 5.0) -> dict:
        """等信号发射（issue #96）。返回 {"emitted": bool, "args": [...]}，超时不抛。"""
        return await self.request(
            "wait_signal",
            {"path": path, "signal": signal, "timeout": timeout},
            timeout=timeout + 5.0,
        )

    async def wait_frames(self, frames: int, physics: bool = False) -> dict:
        """等 N 帧（issue #96）。client wall-time 按最低 10fps 估算 + 10s grace。"""
        return await self.request(
            "wait_frames",
            {"frames": frames, "physics": physics},
            timeout=max(30.0, frames / 10.0 + 10.0),
        )

    async def scene_reload(self, timeout: float = 10.0) -> dict:
        """重载当前场景并等新场景 ready（issue #98）。

        成功返回 {"scene_path": ..., "name": ...}；无 current scene /
        等 ready 超时走 RPC error（1008 SCENE_UNAVAILABLE），会抛异常。
        """
        return await self.request(
            "scene_reload", {"timeout": timeout}, timeout=timeout + 5.0
        )

    async def scene_change(self, path: str, timeout: float = 10.0) -> dict:
        """切换到指定场景并等新场景 ready（issue #98）。

        成功返回 {"scene_path": ..., "name": ...}；path 须为 res:// 或
        uid:// 资源路径，不存在/加载失败/超时 → 1008，会抛异常。
        """
        return await self.request(
            "scene_change", {"path": path, "timeout": timeout}, timeout=timeout + 5.0
        )

    async def time_scale(self, value: float | None = None) -> dict:
        """读 / 写 Engine.time_scale（issue #102）。

        value=None 纯读。合法域 (0, 100]，越界 → -32602。
        返回 {"time_scale": x}。wait_game_time 跟随 time_scale，
        套件整体倍速时 wait 语义不变、墙钟变快。
        """
        params: dict = {} if value is None else {"value": value}
        return await self.request("time_scale", params)

    async def pause(self) -> dict:
        """暂停 SceneTree（issue #102）。幂等；返回 {"paused": True}。"""
        return await self.request("pause", {})

    async def unpause(self) -> dict:
        """恢复 SceneTree（issue #102）。幂等；返回 {"paused": False}。"""
        return await self.request("unpause", {})

    async def step_frames(self, frames: int, physics: bool = False) -> dict:
        """paused 前置下确定性推进 N 帧再停（issue #102）。

        未 pause → 1009 NOT_PAUSED。返回 {"stepped": N, "paused": True}。
        网络超时对齐 wait_frames（最低 10fps 估算 + 10s grace）。
        """
        return await self.request(
            "step_frames",
            {"frames": frames, "physics": physics},
            timeout=max(30.0, frames / 10.0 + 10.0),
        )

    async def wait_game_time(self, seconds: float) -> dict:
        """按 Godot game time 等待 N 秒。

        客户端用固定的长操作生死线（issue #45）：game-time 与 wall-time 比值受
        录像模式 / 分辨率 / 盘速 / fps 影响不可预测，任何 seconds-scaled 公式都会
        在某个组合下假超时。死连接由 ws ping/pong 兜底。生死线默认 600s，可被
        ``GODOT_CLI_LONG_OP_TIMEOUT`` env 覆盖（issue #69）——每次调用现取，
        让 env 改动即时生效。seconds <= 0 时客户端短路返回，不发 RPC。
        """
        if seconds <= 0:
            return {"success": True}
        return await self.request(
            "wait_game_time",
            {"seconds": seconds},
            timeout=_resolve_long_op_timeout(),
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
        # 长操作生死线（issue #45）：见 wait_game_time 注释——同样的 game-time /
        # wall-time 不对齐问题。默认 600s，可被 GODOT_CLI_LONG_OP_TIMEOUT 覆盖
        # （issue #69），每次调用现取。
        return await self.request(
            "input_combo", {"steps": steps}, timeout=_resolve_long_op_timeout()
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

    # 鼠标按钮名 → Godot MOUSE_BUTTON_* 常量（服务端期望 int）。issue #154。
    _MOUSE_BUTTONS = {"left": 1, "right": 2, "middle": 3}

    async def click_at(
        self,
        x: float = 0.0,
        y: float = 0.0,
        *,
        node: str | None = None,
        button: str = "left",
        double: bool = False,
    ) -> dict:
        """坐标级鼠标点击（issue #154）：down→up。

        ``node`` 给定时取其屏幕中心点（忽略 x/y），否则用字面 viewport 物理像素
        坐标。``button`` ∈ left/right/middle。区别于 ``click``（节点级 UI 点击）：
        走真实事件管线，能命中依赖光标位置的 _gui_input 控件。
        """
        if button not in self._MOUSE_BUTTONS:
            raise ValueError(
                f"click_at: 未知 button {button!r}（可选 left/right/middle）"
            )
        params: dict = {"button": self._MOUSE_BUTTONS[button], "double": double}
        if node is not None:
            params["node"] = node
        else:
            params["x"] = x
            params["y"] = y
        return await self.request("click_at", params)

    async def mouse_move(
        self, x: float = 0.0, y: float = 0.0, *, node: str | None = None
    ) -> dict:
        """坐标级鼠标移动（issue #154）：产生一个带 relative 的 InputEventMouseMotion。

        ``node`` 给定时移到其屏幕中心点，否则用字面 viewport 物理像素坐标。
        """
        params: dict = {}
        if node is not None:
            params["node"] = node
        else:
            params["x"] = x
            params["y"] = y
        return await self.request("mouse_move", params)

    async def drag(
        self,
        x1: float = 0.0,
        y1: float = 0.0,
        x2: float = 0.0,
        y2: float = 0.0,
        *,
        from_node: str | None = None,
        to_node: str | None = None,
        button: str = "left",
        duration: float = 0.3,
        steps: int = 10,
    ) -> dict:
        """坐标级拖拽（issue #154 P2）：start 按下 → 按 duration/steps 插值移动 → end 松开。

        两端各自二选一：``from_node`` / ``to_node`` 给定时取对应节点屏幕中心点
        （忽略字面坐标），否则用字面 viewport 物理像素坐标。``button`` ∈
        left/right/middle。``duration`` 是 game-time（受 Engine.time_scale，与
        combo 同语义），``steps`` 是插值分段数。

        客户端用长操作生死线（同 combo / wait_game_time，issue #45）：drag 完成
        是 game-time 维度，wall-time 不可预测；死连接靠 ws ping/pong 兜底。
        """
        if button not in self._MOUSE_BUTTONS:
            raise ValueError(
                f"drag: 未知 button {button!r}（可选 left/right/middle）"
            )
        params: dict = {
            "button": self._MOUSE_BUTTONS[button],
            "duration": duration,
            "steps": steps,
        }
        if from_node is not None:
            params["from_node"] = from_node
        else:
            params["x1"] = x1
            params["y1"] = y1
        if to_node is not None:
            params["to_node"] = to_node
        else:
            params["x2"] = x2
            params["y2"] = y2
        return await self.request(
            "drag", params, timeout=_resolve_long_op_timeout()
        )

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
