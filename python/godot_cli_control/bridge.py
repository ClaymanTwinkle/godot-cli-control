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
from pathlib import Path
from typing import Any

from .client import GameClient


class GameBridge:
    """同步接口，内部通过事件循环调用异步 GameClient。"""

    def __init__(self, port: int | None = None) -> None:
        # port=None → GameClient 从 .cli_control/port auto-discover（issue #91）。
        # daemon 默认 OS 自动分配端口，所以 ``GameBridge()`` 无参数也能连上正在
        # 跑的 daemon，与 README 的单连接脚本示例一致。
        self._loop = asyncio.new_event_loop()
        self._client = GameClient(port=port)
        self._loop.run_until_complete(
            self._client.connect(retries=15, backoff=1.0, total_timeout=60.0)
        )

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

    def tree(self, depth: int = 3, max_nodes: int | None = None) -> dict:
        """获取场景树。"""
        return self._run(self._client.get_scene_tree(depth=depth, max_nodes=max_nodes))

    def get_scene_tree(self, depth: int = 3, max_nodes: int | None = None) -> dict:
        """``tree`` 的别名，与 ``GameClient.get_scene_tree`` 同名对齐（issue #60）。"""
        return self.tree(depth=depth, max_nodes=max_nodes)

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

    def click(self, path: str) -> dict:
        """点击 UI 节点。"""
        return self._run(self._client.click(path))

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

    def screenshot(self, path: str | None = None) -> bytes:
        """截图，返回 PNG 字节。可选保存到文件。"""
        data = self._run(self._client.screenshot())
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return data

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

    def get_children(self, path: str, type_filter: str = "") -> list[dict]:
        """获取子节点列表。"""
        return self._run(self._client.get_children(path, type_filter=type_filter))
