"""pytest 插件：``godot_daemon`` (session) + ``bridge`` (function) + ``fresh_scene`` (function) fixtures。

加载路径：
  - ``pip install godot-cli-control[pytest]`` 装上 pytest 后，pytest 启动时
    通过 ``pytest11`` entry-point 自动注册本模块；
  - 或者用户在 ``conftest.py`` 写 ``pytest_plugins = ["godot_cli_control.pytest_plugin"]``。

把端到端测试样板从 ~20 行降到 0 行：

    def test_jump(godot_daemon, bridge):
        bridge.click("/root/Game/Start")
        bridge.tap("jump")
        assert bridge.get_property("/root/Player", "on_floor") is False

CLI 选项：
  --godot-cli-port            GameBridge 端口（默认 0 = OS 自动分配）
  --godot-cli-no-headless     带窗口跑（默认 headless）
  --godot-cli-project-root    指定 Godot 项目根（默认 pytest rootdir）
  --godot-cli-time-scale      Engine.time_scale（整套 suite 加速用，如 5）
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from .bridge import GameBridge
from .client import DEFAULT_PORT
from .daemon import Daemon, DaemonError


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("godot-cli-control")
    group.addoption(
        "--godot-cli-port",
        action="store",
        default="0",  # 0 = OS-assigned，与 daemon start 默认对齐
        help="GameBridge WebSocket port for the godot_daemon fixture (default: 0 = OS-assigned).",
    )
    group.addoption(
        "--godot-cli-no-headless",
        action="store_true",
        default=False,
        help="Disable headless mode (open a real Godot window).",
    )
    group.addoption(
        "--godot-cli-project-root",
        action="store",
        default=None,
        help="Godot project root (default: pytest rootdir).",
    )
    group.addoption(
        "--godot-cli-time-scale",
        action="store",
        default=None,
        help="Engine.time_scale applied at daemon startup (e.g. 5 to speed up the whole suite).",
    )


@pytest.fixture(scope="session")
def godot_daemon(request: pytest.FixtureRequest) -> Iterator[Daemon]:
    """Session-scoped: 启动 Godot daemon，所有测试跑完后停。

    若启动前已经有 daemon 在跑（开发者手动起的），fixture 不重启也不在
    teardown 杀它 —— 让 dev workflow（IDE 里 daemon 常驻跑测）和 CI
    workflow（fixture 全权管理）都自然衔接。
    """
    config = request.config
    port = int(config.getoption("--godot-cli-port"))
    headless = not config.getoption("--godot-cli-no-headless")

    project_root_opt = config.getoption("--godot-cli-project-root")
    project_root = (
        Path(project_root_opt).resolve()
        if project_root_opt
        else Path(str(config.rootpath)).resolve()
    )

    raw_scale = config.getoption("--godot-cli-time-scale")
    time_scale = float(raw_scale) if raw_scale is not None else None

    daemon = Daemon(project_root)
    started_by_us = False
    if not daemon.is_running():
        try:
            daemon.start(headless=headless, port=port, time_scale=time_scale)
        except DaemonError as e:
            pytest.fail(f"无法启动 Godot daemon：{e}", pytrace=False)
        started_by_us = True

    try:
        yield daemon
    finally:
        if started_by_us:
            try:
                daemon.stop()
            except DaemonError:
                pass


@pytest.fixture
def bridge(godot_daemon: Daemon) -> Iterator[GameBridge]:
    """Function-scoped: 每个测试单独连一个 bridge，结束 release_all + close。

    release_all 是关键：上一个测试 hold 但没 release 的输入会污染下一个；
    pytest 默认每 case 独立，fixture 替用户保证清理。
    """
    port = godot_daemon.current_port() or DEFAULT_PORT
    b = GameBridge(port=port)
    try:
        yield b
    finally:
        try:
            b.release_all()
        except Exception:  # noqa: BLE001 — 关闭路径，吞掉残留以保证 close 一定跑
            pass
        b.close()


@pytest.fixture
def fresh_scene(bridge: GameBridge) -> Iterator[GameBridge]:
    """Function-scoped：setup 时调 bridge.scene_reload() 等新场景 ready（issue #98）。

    语义是「本用例开始时场景是干净的」：teardown 不做事，下一个需要干净
    场景的用例自己声明 fresh_scene。reload 后此前缓存的节点路径全部失效。

        def test_jump(godot_daemon, fresh_scene):
            fresh_scene.click("/root/Game/Start")   # fresh_scene 即 bridge
    """
    bridge.scene_reload()
    yield bridge
