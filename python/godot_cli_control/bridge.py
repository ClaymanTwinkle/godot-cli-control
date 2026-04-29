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

from .client import DEFAULT_PORT, GameClient


class GameBridge:
    """同步接口，内部通过事件循环调用异步 GameClient。"""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
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

    # ── 场景树 ──

    def tree(self, depth: int = 3) -> dict:
        """获取场景树。"""
        return self._run(self._client.get_scene_tree(depth=depth))

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

    def set_property(self, path: str, prop: str, value: Any) -> None:
        """设置节点属性。"""
        self._run(self._client.set_property(path, prop, value))

    def call_method(self, path: str, method: str, args: list | None = None) -> Any:
        """调用节点方法。"""
        return self._run(self._client.call_method(path, method, args))

    def get_children(self, path: str, type_filter: str = "") -> list[dict]:
        """获取子节点列表。"""
        return self._run(self._client.get_children(path, type_filter=type_filter))
