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

import re
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
        type=float,
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

    time_scale: float | None = config.getoption("--godot-cli-time-scale")

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
    """Function-scoped: 每个测试单独连一个 bridge，teardown 还原全局态 + release_all + close。

    release_all 是关键：上一个测试 hold 但没 release 的输入会污染下一个；
    同理 pause / time_scale 是引擎全局状态（issue #124）：上一个用例 pause 后
    崩溃没 unpause，下一个用例就在冻住的树上跑。teardown 统一 best-effort
    兜底：unpause + 把 time_scale 还原到 setup 时的快照值——不是盲写 1.0，
    否则 --godot-cli-time-scale 整套加速（或手动起的 5x daemon）会在第一个
    用例后被砸回 1x。pytest 默认每 case 独立，fixture 替用户保证清理。
    """
    port = godot_daemon.current_port() or DEFAULT_PORT
    b = GameBridge(port=port)
    baseline_time_scale: float | None = None
    try:
        baseline_time_scale = float(b.time_scale()["time_scale"])
    except Exception:  # noqa: BLE001 — 旧版 addon 无 time_scale RPC；拿不到 baseline 就不还原
        pass
    try:
        yield b
    finally:
        try:
            b.unpause()  # 幂等，未 pause 时也安全
        except Exception:  # noqa: BLE001 — 关闭路径，吞掉异常保证后续清理一定跑
            pass
        try:
            if baseline_time_scale is not None:
                b.time_scale(baseline_time_scale)
        except Exception:  # noqa: BLE001
            pass
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


@pytest.fixture
def no_push_errors(bridge: GameBridge) -> Iterator[GameBridge]:
    """Opt-in（issue #103）：用例期间出现新 push_error → 用例失败。

    「游戏静默吞错」类 bug 的唯一 e2e 级防线 —— 业务断言全绿、但代码里
    push_error 了，这个 fixture 让它红出来。setup 记 errors marker，
    teardown 查增量；warning 级不算失败（要更严格自己查 bridge.errors()）。

    注意：
      - teardown 阶段的失败在 pytest 里表现为 ERROR（非 FAIL）——这是
        pytest fixture 的固有语义，结果一样是红的；
      - 需 Godot 4.5+（Logger API）。老引擎上 setup 即抛 RpcError 1012 ——
        fail-loud：宁可红，也不让「断言了零错误」变成假绿。

        def test_load_npc(godot_daemon, no_push_errors):
            no_push_errors.click("/root/Game/SpawnNpc")   # 即 bridge
    """
    marker = int(bridge.errors(limit=0)["marker"])
    yield bridge
    result = bridge.errors(since=marker, limit=100)
    bad = [e for e in result.get("errors", []) if e.get("type") != "warning"]
    if bad:
        lines = [
            f"  - [{e.get('type')}] {e.get('message')}"
            f" ({e.get('source') or e.get('file')})"
            for e in bad
        ]
        extra = "（已截断，还有更多）" if result.get("truncated") else ""
        pytest.fail(
            f"用例期间捕获到 {len(bad)} 条 push_error{extra}:\n" + "\n".join(lines),
            pytrace=False,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """失败自动截图（issue #103）：用例 call 阶段失败且用了 bridge、daemon
    非 headless（headless 截图必 1006，白费）时，best-effort 截到
    ``<project_root>/.cli_control/failures/<nodeid>.png``，路径写进 report
    sections（``-rA`` 或失败摘要里可见）。截图自身的任何异常都吞掉 ——
    诊断辅助不允许掩盖原始失败。
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    bridge_obj = getattr(item, "funcargs", {}).get("bridge")
    if bridge_obj is None:
        return
    config = item.config
    headless = not config.getoption("--godot-cli-no-headless")
    if headless:
        return
    project_root_opt = config.getoption("--godot-cli-project-root")
    root = (
        Path(project_root_opt).resolve()
        if project_root_opt
        else Path(str(config.rootpath)).resolve()
    )
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", item.nodeid)[:150]
    path = root / ".cli_control" / "failures" / f"{safe}.png"
    try:
        bridge_obj.screenshot(str(path))
        report.sections.append(
            ("godot-cli-control", f"failure screenshot: {path}")
        )
    except Exception as exc:  # noqa: BLE001 — 诊断辅助，绝不让截图失败掩盖原失败
        report.sections.append(
            ("godot-cli-control", f"failure screenshot failed: {exc}")
        )
