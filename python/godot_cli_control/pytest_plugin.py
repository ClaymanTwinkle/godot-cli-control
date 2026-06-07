"""pytest 插件：``godot_daemon`` (session) + ``bridge`` (function) + ``fresh_scene`` (function)
+ ``godot_instances``（多实例工厂，scope 可配）fixtures。

加载路径：
  - ``pip install godot-cli-control[pytest]`` 装上 pytest 后，pytest 启动时
    通过 ``pytest11`` entry-point 自动注册本模块；
  - 或者用户在 ``conftest.py`` 写 ``pytest_plugins = ["godot_cli_control.pytest_plugin"]``。

把端到端测试样板从 ~20 行降到 0 行：

    def test_jump(godot_daemon, bridge):
        bridge.click("/root/Game/Start")
        bridge.tap("jump")
        assert bridge.get_property("/root/Player", "on_floor") is False

联机玩法 e2e 在单测里同时拿 server / client bridge（issue #143）：

    def test_join(godot_instances):
        server = godot_instances.start("server")
        client = godot_instances.start("client1")
        # 均为 GameBridge；teardown 自动 stop 本 fixture 起的全部实例

CLI 选项：
  --godot-cli-port            GameBridge 端口（默认 0 = OS 自动分配）
  --godot-cli-no-headless     带窗口跑（默认 headless）
  --godot-cli-project-root    指定 Godot 项目根（默认 pytest rootdir）
  --godot-cli-time-scale      Engine.time_scale（整套 suite 加速用，如 5）
  --godot-cli-instances-scope godot_instances 的 scope：function（默认，每用例
                              各起各停）或 session（整套共享一组实例，省启动
                              时间，换取用例间游戏状态不隔离）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

from .bridge import GameBridge
from .client import DEFAULT_PORT
from .daemon import Daemon, DaemonError


def _project_root(config: pytest.Config) -> Path:
    """--godot-cli-project-root（缺省 pytest rootdir）→ resolve 后的绝对路径。"""
    opt = config.getoption("--godot-cli-project-root")
    return Path(opt).resolve() if opt else Path(str(config.rootpath)).resolve()


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
    group.addoption(
        "--godot-cli-instances-scope",
        action="store",
        choices=("function", "session"),
        default="function",
        help=(
            "Scope of the godot_instances fixture: 'function' (default; each test"
            " starts/stops its own instances) or 'session' (one shared set for the"
            " whole suite — faster, but game state is not isolated between tests)."
        ),
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
    time_scale: float | None = config.getoption("--godot-cli-time-scale")

    daemon = Daemon(_project_root(config))
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


@dataclass
class _InstanceEntry:
    daemon: Daemon
    bridge: GameBridge
    started_by_us: bool


class GodotInstances:
    """``godot_instances`` fixture 的工厂句柄：按名字起 / 连 / 停同项目多个 Godot 实例。

    - ``start(name)`` 幂等 get-or-start：已起过的名字直接返回同一 GameBridge；
    - 实例已在跑（开发者手动起的）只连不重启，teardown 也不杀——与
      ``godot_daemon`` 的 dev / CI 衔接语义一致；
    - ``stop(name)`` 显式中途停（模拟 server 掉线类场景），之后可重新 ``start``；
    - teardown 自动断开全部连接并 stop 本 fixture 起的实例。
    """

    def __init__(
        self,
        project_root: Path,
        *,
        headless: bool,
        time_scale: float | None,
    ) -> None:
        self._project_root = project_root
        self._default_headless = headless
        self._default_time_scale = time_scale
        self._entries: dict[str, _InstanceEntry] = {}

    def start(
        self,
        name: str,
        *,
        headless: bool | None = None,
        port: int = 0,
        time_scale: float | None = None,
    ) -> GameBridge:
        """启动（或连上）命名实例，返回已连接的 GameBridge。

        ``port`` 默认恒 0（OS 自动分配）——多实例不能共享 ``--godot-cli-port``
        的固定端口。``headless`` / ``time_scale`` 缺省跟全局 CLI 选项，可逐实例
        关键字覆盖（如给 server 实例单独开窗观察）。
        """
        entry = self._entries.get(name)
        if entry is not None:
            return entry.bridge
        daemon = Daemon(self._project_root, instance=name)
        started_by_us = False
        if not daemon.is_running():
            try:
                daemon.start(
                    headless=self._default_headless if headless is None else headless,
                    port=port,
                    time_scale=(
                        self._default_time_scale if time_scale is None else time_scale
                    ),
                )
            except DaemonError as e:
                pytest.fail(f"无法启动 Godot 实例 {name!r}：{e}", pytrace=False)
            started_by_us = True
        bridge_port = daemon.current_port()
        if bridge_port is None:
            # 不退 DEFAULT_PORT —— 多实例都退到同一默认端口必然串台，fail-loud。
            if started_by_us:
                try:
                    daemon.stop()
                except DaemonError:
                    pass
            pytest.fail(f"实例 {name!r} 的端口文件不可读，无法建立连接", pytrace=False)
        bridge = GameBridge(port=bridge_port)
        self._entries[name] = _InstanceEntry(daemon, bridge, started_by_us)
        return bridge

    def daemon(self, name: str) -> Daemon:
        """访问底层 Daemon（current_port / read_pid 等）。未 start 的名字 → KeyError。"""
        return self._require(name).daemon

    def stop(self, name: str) -> None:
        """显式停掉一个实例：断连 + （若是本 fixture 起的）stop 进程。

        之后同名可重新 ``start``。借用的实例（fixture 没起的）只断连不杀进程。
        ``daemon.stop()`` 的失败向上抛——中途显式 stop 是测试逻辑的一部分，
        失败应让用例红，而非静默放过。
        """
        entry = self._require(name)
        del self._entries[name]
        try:
            entry.bridge.release_all()
        except Exception:  # noqa: BLE001 — 关闭路径，残留输入清理 best-effort
            pass
        try:
            entry.bridge.close()
        except Exception:  # noqa: BLE001 — 连接可能已断，不阻塞 stop 流程
            pass
        if entry.started_by_us:
            entry.daemon.stop()

    def stop_all(self) -> None:
        """teardown 兜底：逐实例 stop，单个失败不拦住其余实例的清理。"""
        for name in list(self._entries):
            try:
                self.stop(name)
            except Exception:  # noqa: BLE001 — teardown 路径，保证全部实例都被尝试
                pass

    def _require(self, name: str) -> _InstanceEntry:
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(
                f"instance {name!r} was not started via this fixture"
                f" (known: {sorted(self._entries)})"
            )
        return entry


def _godot_instances_scope(fixture_name: str, config: pytest.Config) -> str:
    """``godot_instances`` 的 dynamic scope：--godot-cli-instances-scope（默认 function）。"""
    return config.getoption("--godot-cli-instances-scope")


@pytest.fixture(scope=_godot_instances_scope)
def godot_instances(request: pytest.FixtureRequest) -> Iterator[GodotInstances]:
    """多实例工厂（issue #143）：联机 e2e 在单测里同时拿 server / client bridge。

        def test_join(godot_instances):
            server = godot_instances.start("server")
            client = godot_instances.start("client1")
            # server / client 均为 GameBridge，teardown 自动 stop 全部

    scope 由 ``--godot-cli-instances-scope`` 控制：function（默认）每用例各起
    各停，隔离最好；session 整套共享一组实例，省掉每用例数秒的启动开销，代价
    是游戏状态跨用例不隔离——套件自己负责状态复位。

    与 ``godot_daemon`` / ``bridge`` 共存：那对管 default 实例，本 fixture 管
    命名实例，互不相干。
    """
    config = request.config
    inst = GodotInstances(
        _project_root(config),
        headless=not config.getoption("--godot-cli-no-headless"),
        time_scale=config.getoption("--godot-cli-time-scale"),
    )
    try:
        yield inst
    finally:
        inst.stop_all()


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
    root = _project_root(config)
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
