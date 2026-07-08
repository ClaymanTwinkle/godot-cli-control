"""同步 Bridge —— 封装 GameClient，让 Python 脚本只需写操作逻辑。

用法：脚本只需定义 run(bridge) 函数即可。

    def run(bridge):
        bridge.click("/root/MyScene/StartButton")
        bridge.wait(2)
        bridge.hold("run", 1.5)
        bridge.tap("attack")
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from .client import GameClient


class GameBridge:
    """同步接口，内部通过事件循环调用异步 GameClient。"""

    def __init__(self, port: int | None = None, instance: str | None = None) -> None:
        # port=None → GameClient 从 .cli_control/ auto-discover（issue #91）。
        # daemon 默认 OS 自动分配端口，所以 ``GameBridge()`` 无参数也能连上正在
        # 跑的 daemon，与 README 的单连接脚本示例一致。
        # instance：命名实例名（如 "server"），透传给 GameClient.instance；
        # 显式 port 优先，instance 仅在 port=None 时生效（见 client.py）。
        self._loop = asyncio.new_event_loop()
        # 构造/连接失败时回收 loop —— 多实例歧义（InstanceAmbiguityError）在
        # __init__ 抛出是预期用法路径，不能让每次报错都泄漏一个 event loop。
        try:
            self._client = GameClient(port=port, instance=instance)
            self._loop.run_until_complete(
                self._client.connect(retries=15, backoff=1.0, total_timeout=60.0)
            )
        except BaseException:
            self._loop.close()
            raise

    def close(self) -> None:
        self._loop.run_until_complete(self._client.disconnect())
        self._loop.close()

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    # ── 等待 ──

    def wait(self, seconds: float) -> None:
        """按 Godot game time 等待指定秒数。

        在 --write-movie 模式下，此方法等待的是 game time（录像帧数基准），
        而非 wall time，确保录像内容与脚本期望对齐。
        """
        self._run(self._client.wait_game_time(seconds))

    def wait_game_time(self, seconds: float) -> None:
        """``wait`` 的别名，与 ``GameClient.wait_game_time`` 同名对齐（issue #60）。"""
        self.wait(seconds)

    # ── 场景树 ──

    def tree(
        self, depth: int = 3, max_nodes: int | None = None, path: str | None = None
    ) -> dict:
        """获取场景树。``path``（#150）：传绝对节点路径取该子树，省略则取当前场景。"""
        return self._run(
            self._client.get_scene_tree(depth=depth, max_nodes=max_nodes, path=path)
        )

    def get_scene_tree(
        self, depth: int = 3, max_nodes: int | None = None, path: str | None = None
    ) -> dict:
        """``tree`` 的别名，与 ``GameClient.get_scene_tree`` 同名对齐（issue #60）。"""
        return self.tree(depth=depth, max_nodes=max_nodes, path=path)

    def node_exists(self, path: str) -> bool:
        """检查节点是否存在。"""
        return self._run(self._client.node_exists(path))

    def is_visible(self, path: str) -> bool:
        """检查节点是否可见（CanvasItem.is_visible_in_tree）。"""
        return self._run(self._client.is_visible(path))

    def get_text(self, path: str) -> str:
        """读取 Label / Button 等节点的 ``text`` 属性（带空字符串兜底）。"""
        return self._run(self._client.get_text(path))

    def wait_for_node(self, path: str, timeout: float = 5.0) -> bool:
        """等待节点出现。"""
        return self._run(self._client.wait_for_node(path, timeout=timeout))

    def wait_property(
        self,
        path: str,
        prop: str,
        value: Any,
        op: str = "eq",
        timeout: float = 5.0,
        tolerance: float = 0.0,
    ) -> dict:
        """逐帧轮询直到属性满足条件（issue #96）。返回 {"matched": bool, ...}，超时不抛。"""
        return self._run(self._client.wait_property(path, prop, value, op=op, timeout=timeout, tolerance=tolerance))

    def wait_signal(self, path: str, signal: str, timeout: float = 5.0) -> dict:
        """等信号发射（issue #96）。返回 {"emitted": bool, "args": [...]}，超时不抛。"""
        return self._run(self._client.wait_signal(path, signal, timeout=timeout))

    def wait_frames(self, frames: int, physics: bool = False) -> dict:
        """等 N 帧（issue #96）。确定性帧推进，替代短 sleep。"""
        return self._run(self._client.wait_frames(frames, physics=physics))

    def scene_reload(self, timeout: float = 10.0) -> dict:
        """重载当前场景并等新场景 ready（issue #98）。返回 {"scene_path": ..., "name": ...}。"""
        return self._run(self._client.scene_reload(timeout=timeout))

    def scene_change(self, path: str, timeout: float = 10.0) -> dict:
        """切换场景并等新场景 ready（issue #98）。path 须为 res:// 或 uid://。"""
        return self._run(self._client.scene_change(path, timeout=timeout))

    def time_scale(self, value: float | None = None) -> dict:
        """读 / 写 Engine.time_scale（issue #102）。value=None 纯读，返回 {"time_scale": x}；越界（<=0 或 >100）→ -32602。"""
        return self._run(self._client.time_scale(value))

    def pause(self) -> dict:
        """暂停 SceneTree（issue #102）。幂等；返回 {"paused": True}。"""
        return self._run(self._client.pause())

    def unpause(self) -> dict:
        """恢复 SceneTree（issue #102）。幂等；返回 {"paused": False}。"""
        return self._run(self._client.unpause())

    def step_frames(self, frames: int, physics: bool = False) -> dict:
        """paused 前置下确定性推进 N 帧再停（issue #102）。未 pause → 1009；返回 {"stepped": N, "paused": True}。"""
        return self._run(self._client.step_frames(frames, physics=physics))

    # ── UI 交互 ──

    def click(
        self,
        path: str | None = None,
        *,
        node_type: str | None = None,
        text: str | None = None,
        text_contains: str | None = None,
        name_pattern: str | None = None,
        from_path: str | None = None,
    ) -> dict:
        """点击 UI 节点：给 ``path`` 直接定位，或给 find 同款过滤器由服务端
        同帧原子 find+click（恰好 1 个匹配才点：0 个 → 1001，≥2 个 → 1017）。"""
        return self._run(
            self._client.click(
                path,
                node_type=node_type,
                text=text,
                text_contains=text_contains,
                name_pattern=name_pattern,
                from_path=from_path,
            )
        )

    def click_at(
        self,
        x: float = 0.0,
        y: float = 0.0,
        *,
        node: str | None = None,
        button: str = "left",
        double: bool = False,
    ) -> dict:
        """坐标级鼠标点击（issue #154）。node 给定时取其中心点，否则用字面坐标。"""
        return self._run(
            self._client.click_at(x, y, node=node, button=button, double=double)
        )

    def mouse_move(
        self, x: float = 0.0, y: float = 0.0, *, node: str | None = None
    ) -> dict:
        """坐标级鼠标移动（issue #154），产生带 relative 的 motion 事件。"""
        return self._run(self._client.mouse_move(x, y, node=node))

    def drag(
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
        """坐标级拖拽（issue #154 P2）：down → 按 duration/steps 插值 motion → up。

        两端 ``from_node`` / ``to_node`` 给定时取节点中心，否则用字面坐标。
        """
        return self._run(
            self._client.drag(
                x1, y1, x2, y2,
                from_node=from_node, to_node=to_node,
                button=button, duration=duration, steps=steps,
            )
        )

    # ── 输入模拟 ──

    def hold(self, action: str, duration: float) -> None:
        """按住一个动作指定时长。"""
        self._run(self._client.hold(action, duration))

    def tap(self, action: str, duration: float = 0.1) -> None:
        """快速点按。"""
        self._run(self._client.action_tap(action, duration))

    def action_tap(self, action: str, duration: float = 0.1) -> None:
        """``tap`` 的别名，与 ``GameClient.action_tap`` 同名对齐（issue #58）。"""
        self.tap(action, duration)

    def action_press(self, action: str) -> None:
        """按下动作（不释放，需手动 release）。"""
        self._run(self._client.action_press(action))

    def action_release(self, action: str) -> None:
        """释放动作。"""
        self._run(self._client.action_release(action))

    def release_all(self) -> None:
        """释放所有按住的动作。"""
        self._run(self._client.release_all())

    def combo(self, steps: list[dict]) -> dict:
        """执行连续动作序列。"""
        return self._run(self._client.combo(steps))

    def combo_cancel(self) -> dict:
        """取消正在运行的 combo（不动 press/hold 状态）。"""
        return self._run(self._client.combo_cancel())

    def get_pressed(self) -> list[str]:
        """当前模拟器持有的输入动作列表（press + held 去重合并）。"""
        return self._run(self._client.get_pressed())

    def list_input_actions(self, include_builtin: bool = False) -> list[str]:
        """列出运行中项目的 InputMap 动作。默认过滤 ui_* 内置。"""
        return self._run(self._client.list_input_actions(include_builtin))

    # ── 截图 ──

    def screenshot(self, path: str | None = None, node: str | None = None) -> bytes:
        """截图，返回 PNG 字节。可选保存到文件。

        ``node``（issue #101）：按节点屏幕 AABB 裁剪成小图（像素级断言用）。
        屏幕外 → 1011，算不出边界 → 1010。

        ``path`` 给定时走 daemon 直写（issue #149）：daemon 与本进程同机
        （localhost-only），PNG 由 daemon 写到 path，base64 不过 WS——大图
        不再受消息大小限制；本方法从文件读回字节，返回值契约不变。旧 addon
        不认 path 参数、照旧回 base64 → 自动回退本地写盘。
        不传 path 仍走 base64 通道（受 daemon outbound buffer 限制，默认 10MB）。
        """
        if path:
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            raw = self._run(
                self._client.screenshot_raw(node=node, path=str(p.resolve()))
            )
            if "image" in raw:  # 旧 addon 回退
                data = base64.b64decode(raw["image"])
                p.write_bytes(data)
                return data
            return p.read_bytes()
        return self._run(self._client.screenshot(node=node))

    def sprite_info(self, path: str) -> dict:
        """渲染态聚合查询（issue #101）：texture/图集区域/翻转/帧号 一次拿齐。"""
        return self._run(self._client.sprite_info(path))

    def errors(self, since: int = 0, limit: int = 100) -> dict:
        """push_error/push_warning 增量查询（issue #103）。since 传上次 marker；
        limit=0 纯拿基线。需 Godot 4.5+，老引擎 → RpcError 1012。"""
        return self._run(self._client.errors(since=since, limit=limit))

    # ── 属性读写 ──

    def get_property(self, path: str, prop: str) -> Any:
        """获取节点属性。"""
        return self._run(self._client.get_property(path, prop))

    def get_properties(self, path: str, props: list[str]) -> dict[str, Any]:
        """同帧原子读多个属性（issue #100），返回 {prop: value} 映射。"""
        return self._run(self._client.get_properties(path, props))

    def set_property(self, path: str, prop: str, value: Any) -> None:
        """设置节点属性。"""
        self._run(self._client.set_property(path, prop, value))

    def call_method(self, path: str, method: str, args: list | None = None) -> Any:
        """调用节点方法。"""
        return self._run(self._client.call_method(path, method, args))

    def emit_signal(self, path: str, signal: str, args: list | None = None) -> dict:
        """发射节点信号（需 daemon 带 --allow-emit-signal 启动；否则服务端 1015）。"""
        return self._run(self._client.emit_signal(path, signal, args))

    def get_children(self, path: str, type_filter: str = "") -> list[dict]:
        """获取子节点列表。"""
        return self._run(self._client.get_children(path, type_filter=type_filter))

    def find_nodes(
        self,
        node_type: str | None = None,
        text: str | None = None,
        text_contains: str | None = None,
        name_pattern: str | None = None,
        from_path: str | None = None,
        limit: int = 20,
    ) -> dict:
        """服务端节点搜索（issue #153）：按 类型/文本/名字通配 一次 RPC 拿齐
        匹配路径，替代逐层 children+get_text 递归。过滤器语义与
        ``GameClient.find_nodes`` 一致；返回 ``{"matches": [...], "truncated"?}``。"""
        return self._run(
            self._client.find_nodes(
                node_type=node_type,
                text=text,
                text_contains=text_contains,
                name_pattern=name_pattern,
                from_path=from_path,
                limit=limit,
            )
        )
