"""``godot_cli_control.pytest_plugin`` 的结构性单测。

不真启 Godot —— 用 pytester 在子 pytest 里 monkey-patch Daemon /
GameBridge，验证 fixture 顺序、teardown 行为与 CLI 选项注册。
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _pin_asyncio_loop_scope(pytester: pytest.Pytester) -> None:
    """pytester 子实例不继承主 pyproject 的 ``[tool.pytest.ini_options]``，而
    pytest-asyncio 是已安装插件，每次子 ``runpytest`` 都会因
    ``asyncio_default_fixture_loop_scope`` 未设发 deprecation warning（同进程下冒泡到
    主 summary）。这些子测试只验证同步 fixture，给每个临时项目写一份最小 ini 钉死该值，
    消除噪音；与主 pyproject 的同名配置保持一致。"""
    pytester.makeini(
        """
        [pytest]
        asyncio_default_fixture_loop_scope = function
        """
    )


def test_options_registered_in_help(pytester: pytest.Pytester) -> None:
    """--help 输出含我们的 CLI 选项 group。"""
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(
        [
            "*godot-cli-control:*",
            "*--godot-cli-port*",
            "*--godot-cli-no-headless*",
            "*--godot-cli-project-root*",
        ]
    )


def test_fixtures_listed(pytester: pytest.Pytester) -> None:
    """`pytest --fixtures` 列出 godot_daemon + bridge + fresh_scene + no_push_errors。

    逐个 fnmatch（顺序无关——pytest 对 fixture 的列出顺序不保证），
    并锚定「<name> ... -- ...pytest_plugin.py」定义行格式，
    避免 docstring 里提及其它 fixture 名造成假阳性。
    """
    pytester.makepyfile(
        test_smoke="""
        def test_smoke():
            pass
        """
    )
    result = pytester.runpytest("--fixtures")
    for name in (
        "godot_daemon",
        "bridge",
        "fresh_scene",
        "no_push_errors",
        "godot_instances",
    ):
        result.stdout.fnmatch_lines([f"{name}*-- *pytest_plugin.py*"])


def test_fixture_lifecycle(pytester: pytest.Pytester) -> None:
    """跑两个 dummy 测，确认 daemon 启 1 次、bridge 在每个 case 后清理。"""
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin

        # 模块级 monkeypatch：在 pytester 子进程里替掉 Daemon / GameBridge
        # 引用，不真连 Godot。session fixture 会拿到替换后的类。
        CALLS: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): CALLS.append("Daemon.__init__")
            def is_running(self): return False
            def start(self, **kw): CALLS.append(f"start:{kw.get('port')}")
            def stop(self): CALLS.append("stop"); return 0
            def current_port(self): return 9877

        class FakeBridge:
            def __init__(self, port): CALLS.append(f"Bridge.__init__:{port}")
            def release_all(self): CALLS.append("release_all")
            def close(self): CALLS.append("close")

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line(f"CALLS={CALLS}")
        """
    )
    pytester.makepyfile(
        """
        def test_a(bridge):
            pass

        def test_b(bridge):
            pass
        """
    )
    result = pytester.runpytest("-s", "--godot-cli-port", "9890")
    result.assert_outcomes(passed=2)
    output = result.stdout.str()
    # daemon.start 只在 session 起 1 次；每个 test_* 各调一次 release_all + close
    assert output.count("release_all") == 2, output
    assert output.count("'close'") + output.count("close,") + output.count("'close'") >= 2
    assert "start:9890" in output
    assert "stop" in output


# ---------------------------------------------------------------------------
# 以下补强：fixture 失败 / 复用 / 异常清理 场景
# ---------------------------------------------------------------------------


def test_daemon_start_failure_surfaces_to_user(pytester: pytest.Pytester) -> None:
    """daemon.start 抛 DaemonError → fixture 用 pytest.fail 透传错误（不吞）。"""
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin
        from godot_cli_control.daemon import DaemonError

        class FailDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return False
            def start(self, **kw):
                raise DaemonError("Godot binary not found")
            def stop(self): return 0
            def current_port(self): return 9877

        class FakeBridge:
            def __init__(self, port): pass
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = FailDaemon
        plugin.GameBridge = FakeBridge
        """
    )
    pytester.makepyfile(
        """
        def test_uses_bridge(bridge):
            assert False, "fixture should fail before reaching here"
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    # 错误消息必须含 daemon 的原始原因，方便用户定位
    output = result.stdout.str()
    assert "Godot binary not found" in output


def test_fixture_reuses_already_running_daemon(pytester: pytest.Pytester) -> None:
    """开发者手动起的 daemon 已在跑 → fixture 不重启，teardown 也不杀。

    dev workflow（IDE 里 daemon 常驻）vs CI workflow（fixture 全权管理）能自然衔接。
    """
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin

        START_CALLS: list = []
        STOP_CALLS: list = []

        class AlreadyRunningDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return True  # 关键：已在跑
            def start(self, **kw): START_CALLS.append(kw)
            def stop(self): STOP_CALLS.append(1); return 0
            def current_port(self): return 9877

        class FakeBridge:
            def __init__(self, port): pass
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = AlreadyRunningDaemon
        plugin.GameBridge = FakeBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line(f"START_CALLS={START_CALLS}")
            terminalreporter.write_line(f"STOP_CALLS={STOP_CALLS}")
        """
    )
    pytester.makepyfile(
        """
        def test_a(bridge): pass
        def test_b(bridge): pass
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=2)
    output = result.stdout.str()
    assert "START_CALLS=[]" in output, "已运行的 daemon 不应被重启"
    assert "STOP_CALLS=[]" in output, "fixture 没起的 daemon teardown 不该杀它"


def test_bridge_teardown_runs_even_when_test_raises(
    pytester: pytest.Pytester,
) -> None:
    """用户测试体抛异常 → bridge teardown 必须仍调 release_all + close。

    保证清理鲁棒：上一个测试 hold 了输入但代码异常退出，下一个测试不应继承
    那次 hold 的状态。
    """
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin

        TEARDOWN_LOG: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return False
            def start(self, **kw): pass
            def stop(self): pass
            def current_port(self): return 9877

        class FakeBridge:
            def __init__(self, port): pass
            def release_all(self): TEARDOWN_LOG.append("release_all")
            def close(self): TEARDOWN_LOG.append("close")

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line(f"TEARDOWN_LOG={TEARDOWN_LOG}")
        """
    )
    pytester.makepyfile(
        """
        def test_user_raises(bridge):
            raise RuntimeError("user code went bang")
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    # 即使用户测试 raise，release_all + close 都必须在 teardown 跑
    assert "release_all" in output
    assert "close" in output
    # 顺序必须对：release_all 先于 close
    teardown_line = [
        line for line in output.splitlines() if line.startswith("TEARDOWN_LOG=")
    ][0]
    assert teardown_line.index("release_all") < teardown_line.index("close")


def test_bridge_teardown_robust_against_release_all_exception(
    pytester: pytest.Pytester,
) -> None:
    """release_all 自己抛异常 → close 仍必须执行（finally 兜底）。

    场景：daemon 已挂但还没被 fixture 检测到，release_all 走 RPC 时连接已断。
    close 要能正确收尾，否则资源泄漏。
    """
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin

        CLOSE_CALLED: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return False
            def start(self, **kw): pass
            def stop(self): pass
            def current_port(self): return 9877

        class FlakyBridge:
            def __init__(self, port): pass
            def release_all(self):
                raise ConnectionError("pipe broken")
            def close(self): CLOSE_CALLED.append(1)

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FlakyBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line(f"CLOSE_CALLED={CLOSE_CALLED}")
        """
    )
    pytester.makepyfile(
        """
        def test_a(bridge): pass
        """
    )
    result = pytester.runpytest("-s")
    # release_all 异常被 fixture 吞掉，测试本身仍通过
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert "CLOSE_CALLED=[1]" in output, "release_all 抛错后 close 必须仍跑"


def test_default_port_is_zero(pytester: pytest.Pytester) -> None:
    """--godot-cli-port 默认 0 = OS 自动分配，与 daemon start 默认对齐。

    早期版本默认 9877 会让多项目并行测试撞端口，且 9877 不再是 daemon
    start 默认。fixture 必须放手让 daemon 自己挑。
    """
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(
        ["*--godot-cli-port*", "*default: 0*"]
    )


def test_godot_daemon_passes_port_zero_when_default(
    pytester: pytest.Pytester,
) -> None:
    """fixture 拿默认值（不传 --godot-cli-port）时 daemon.start(port=0)。"""
    pytester.makeconftest(
        """
        import godot_cli_control.pytest_plugin as plugin

        OBSERVED: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return False
            def start(self, **kw): OBSERVED.append(kw.get("port"))
            def stop(self): return 0
            def current_port(self): return 5555

        class FakeBridge:
            def __init__(self, port): OBSERVED.append(f"bridge:{port}")
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_sessionfinish(session, exitstatus):
            assert OBSERVED[0] == 0, f"expected port=0 default, got {OBSERVED}"
        """
    )
    pytester.makepyfile(
        test_x="""
        def test_x(godot_daemon, bridge): pass
        """
    )
    result = pytester.runpytest("-v")
    assert result.ret == 0


def test_project_root_option_is_respected(pytester: pytest.Pytester) -> None:
    """--godot-cli-project-root 必须传给 Daemon 构造函数。"""
    pytester.makeconftest(
        """
        from __future__ import annotations
        from pathlib import Path
        import godot_cli_control.pytest_plugin as plugin

        SEEN_ROOTS: list = []

        class CapturingDaemon:
            def __init__(self, project_root, *a, **kw):
                SEEN_ROOTS.append(Path(project_root).resolve())
            def is_running(self): return True
            def start(self, **kw): pass
            def stop(self): return 0
            def current_port(self): return 9877

        class FakeBridge:
            def __init__(self, port): pass
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = CapturingDaemon
        plugin.GameBridge = FakeBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            # 用 str() 逐条打印 —— 列表的 repr 在 Windows 上是
            # repr(WindowsPath(...)) 用正斜杠，str(WindowsPath) 用反斜杠，
            # 测试若直接 in 整个列表 repr 会 mismatch。
            for p in SEEN_ROOTS:
                terminalreporter.write_line(f"SEEN_ROOT={p}")
        """
    )
    pytester.makepyfile("def test_x(bridge): pass")
    custom_root = pytester.path / "my_godot_project"
    custom_root.mkdir()
    result = pytester.runpytest(
        "-s", "--godot-cli-project-root", str(custom_root)
    )
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert str(custom_root.resolve()) in output


# ---------------------------------------------------------------------------
# fresh_scene fixture（issue #98）
# ---------------------------------------------------------------------------


def test_fresh_scene_fixture(pytester: pytest.Pytester) -> None:
    """fresh_scene：setup 时 scene_reload 恰好调 1 次，yield 出 bridge 本身，
    teardown 不再调 scene_reload（每用例各 1 次，无额外调用）。
    """
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin

        RELOAD_COUNT: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return True
            def start(self, **kw): pass
            def stop(self): return 0
            def current_port(self): return 9877

        class FakeBridge:
            def __init__(self, port): pass
            def scene_reload(self): RELOAD_COUNT.append(1)
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line(f"RELOAD_COUNT={RELOAD_COUNT}")
        """
    )
    pytester.makepyfile(
        """
        def test_uses_fresh_scene(fresh_scene, bridge):
            # fresh_scene 必须是 bridge 同一个对象
            assert fresh_scene is bridge

        def test_only_one_reload(fresh_scene):
            pass
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=2)
    output = result.stdout.str()
    # 两个测试各调 1 次 scene_reload，合计 2 次
    assert "RELOAD_COUNT=[1, 1]" in output, f"expected 2 reloads, got: {output}"


# ---------------------------------------------------------------------------
# --godot-cli-time-scale 选项透传（#102）
# ---------------------------------------------------------------------------


def test_time_scale_option_registered_in_help(pytester: pytest.Pytester) -> None:
    """--godot-cli-time-scale 出现在 --help 的 godot-cli-control 组。"""
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(["*--godot-cli-time-scale*"])


def test_godot_daemon_passes_time_scale_to_start(pytester: pytest.Pytester) -> None:
    """--godot-cli-time-scale 3 → daemon.start(time_scale=3.0)。"""
    pytester.makeconftest(
        """
        import godot_cli_control.pytest_plugin as plugin

        OBSERVED: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return False
            def start(self, **kw): OBSERVED.append(kw.get("time_scale"))
            def stop(self): return 0
            def current_port(self): return 5555

        class FakeBridge:
            def __init__(self, port): pass
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_sessionfinish(session, exitstatus):
            assert OBSERVED[0] == 3.0, f"expected time_scale=3.0, got {OBSERVED}"
        """
    )
    pytester.makepyfile(
        test_x="""
        def test_x(godot_daemon, bridge): pass
        """
    )
    result = pytester.runpytest("-v", "--godot-cli-time-scale", "3")
    assert result.ret == 0


def test_godot_daemon_passes_time_scale_none_when_not_given(
    pytester: pytest.Pytester,
) -> None:
    """不传 --godot-cli-time-scale → daemon.start(time_scale=None)。"""
    pytester.makeconftest(
        """
        import godot_cli_control.pytest_plugin as plugin

        OBSERVED: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return False
            def start(self, **kw): OBSERVED.append(kw.get("time_scale"))
            def stop(self): return 0
            def current_port(self): return 5555

        class FakeBridge:
            def __init__(self, port): pass
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_sessionfinish(session, exitstatus):
            assert OBSERVED[0] is None, f"expected time_scale=None, got {OBSERVED}"
        """
    )
    pytester.makepyfile(
        test_x="""
        def test_x(godot_daemon, bridge): pass
        """
    )
    result = pytester.runpytest("-v")
    assert result.ret == 0


def test_time_scale_invalid_float_gives_usage_error(pytester: pytest.Pytester) -> None:
    """--godot-cli-time-scale abc → pytest 以 usage error（exit 4）退出，
    stderr 含 'invalid float value'（由 argparse type=float 触发，不再进 fixture 内部）。
    """
    result = pytester.runpytest("--godot-cli-time-scale", "abc")
    # pytest argparse usage error 退出码为 4
    assert result.ret == 4
    result.stderr.fnmatch_lines(["*invalid float value*"])


# ---------------------------------------------------------------------------
# bridge teardown 兜底还原 pause / time_scale（issue #124）
# ---------------------------------------------------------------------------


def test_bridge_teardown_restores_pause_and_time_scale(
    pytester: pytest.Pytester,
) -> None:
    """pause / time_scale 是引擎全局状态：用例改了没还原，同一 daemon 下的
    下一个用例会在冻住 / 加速的树上跑。teardown 必须 best-effort 兜底：
    unpause + 把 time_scale 还原到 setup 时的快照值（不是盲写 1.0 ——
    否则 --godot-cli-time-scale 5 的整套加速会在第一个用例后被砸掉）。
    """
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin

        LOG: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return True
            def start(self, **kw): pass
            def stop(self): return 0
            def current_port(self): return 9877

        class FakeBridge:
            def __init__(self, port): pass
            def time_scale(self, value=None):
                LOG.append(f"time_scale:{value}")
                return {"time_scale": 5.0}  # 模拟 daemon 以 5x 起的 baseline
            def pause(self):
                LOG.append("pause")
                return {"paused": True}
            def unpause(self):
                LOG.append("unpause")
                return {"paused": False}
            def release_all(self): LOG.append("release_all")
            def close(self): LOG.append("close")

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line(f"LOG={LOG}")
        """
    )
    pytester.makepyfile(
        """
        def test_messes_with_time(bridge):
            bridge.pause()
            bridge.time_scale(2.0)
            raise RuntimeError("crash before restoring")
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    # setup 快照读（None）→ 用例内 pause + 2.0 → teardown 还原到 5.0（非 1.0）
    expected = (
        "LOG=['time_scale:None', 'pause', 'time_scale:2.0', "
        "'unpause', 'time_scale:5.0', 'release_all', 'close']"
    )
    assert expected in output, output


def test_bridge_teardown_restore_robust_when_unpause_raises(
    pytester: pytest.Pytester,
) -> None:
    """unpause 抛异常（连接断）→ time_scale 还原 / release_all / close 仍必须跑。"""
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin

        LOG: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return True
            def start(self, **kw): pass
            def stop(self): return 0
            def current_port(self): return 9877

        class FakeBridge:
            def __init__(self, port): pass
            def time_scale(self, value=None):
                LOG.append(f"time_scale:{value}")
                return {"time_scale": 1.0}
            def unpause(self):
                raise ConnectionError("pipe broken")
            def release_all(self): LOG.append("release_all")
            def close(self): LOG.append("close")

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = FakeBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line(f"LOG={LOG}")
        """
    )
    pytester.makepyfile(
        """
        def test_a(bridge): pass
        """
    )
    result = pytester.runpytest("-s")
    # unpause 异常被吞，测试本身仍通过
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert (
        "LOG=['time_scale:None', 'time_scale:1.0', 'release_all', 'close']" in output
    ), output


def test_bridge_time_scale_snapshot_failure_skips_restore(
    pytester: pytest.Pytester,
) -> None:
    """setup 快照读 time_scale 失败（如旧版 addon 无此 RPC）→ 不还原 time_scale
    （baseline 未知，盲写反而破坏），但 unpause / release_all / close 照常跑。
    """
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin

        LOG: list = []

        class FakeDaemon:
            def __init__(self, *a, **kw): pass
            def is_running(self): return True
            def start(self, **kw): pass
            def stop(self): return 0
            def current_port(self): return 9877

        class OldAddonBridge:
            def __init__(self, port): pass
            def time_scale(self, value=None):
                raise RuntimeError("RPC method not found: time_scale")
            def unpause(self):
                LOG.append("unpause")
                return {"paused": False}
            def release_all(self): LOG.append("release_all")
            def close(self): LOG.append("close")

        plugin.Daemon = FakeDaemon
        plugin.GameBridge = OldAddonBridge

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line(f"LOG={LOG}")
        """
    )
    pytester.makepyfile(
        """
        def test_a(bridge): pass
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert "LOG=['unpause', 'release_all', 'close']" in output, output


# ---------------------------------------------------------------------------
# no_push_errors fixture + 失败自动截图（issue #103）
# ---------------------------------------------------------------------------

_DIAG_CONFTEST_HEADER = """
from __future__ import annotations
import godot_cli_control.pytest_plugin as plugin

CALLS: list = []

class FakeDaemon:
    def __init__(self, *a, **kw): pass
    def is_running(self): return True
    def start(self, **kw): pass
    def stop(self): return 0
    def current_port(self): return 9877
"""

_DIAG_CONFTEST_FOOTER = """
plugin.Daemon = FakeDaemon
plugin.GameBridge = FakeBridge

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    terminalreporter.write_line(f"CALLS={CALLS}")
"""


def test_no_push_errors_passes_when_clean(pytester: pytest.Pytester) -> None:
    """setup 记 marker（limit=0），teardown 查增量；无错误 → 用例过。"""
    pytester.makeconftest(
        _DIAG_CONFTEST_HEADER
        + """
class FakeBridge:
    def __init__(self, port): pass
    def errors(self, since=0, limit=100):
        CALLS.append(f"errors:since={since},limit={limit}")
        if limit == 0:
            return {"marker": 7, "errors": [], "dropped": 0, "truncated": False}
        return {"marker": 7, "errors": [], "dropped": 0, "truncated": False}
    def release_all(self): pass
    def close(self): pass
"""
        + _DIAG_CONFTEST_FOOTER
    )
    pytester.makepyfile(
        """
        def test_clean(no_push_errors):
            pass
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert "errors:since=0,limit=0" in output, "setup 应做 limit=0 基线查询"
    assert "errors:since=7,limit=100" in output, "teardown 应从 marker 起查增量"


def test_no_push_errors_fails_on_new_error(pytester: pytest.Pytester) -> None:
    """teardown 查到 error 级新增 → 用例红（teardown 失败 = pytest ERROR）。"""
    pytester.makeconftest(
        _DIAG_CONFTEST_HEADER
        + """
class FakeBridge:
    def __init__(self, port): pass
    def errors(self, since=0, limit=100):
        if limit == 0:
            return {"marker": 7, "errors": [], "dropped": 0, "truncated": False}
        return {
            "marker": 8,
            "errors": [{
                "type": "error",
                "message": "npc sheet missing",
                "source": "res://npc.gd:12 @ load_sheet",
            }],
            "dropped": 0,
            "truncated": False,
        }
    def release_all(self): pass
    def close(self): pass
"""
        + _DIAG_CONFTEST_FOOTER
    )
    pytester.makepyfile(
        """
        def test_silently_swallows(no_push_errors):
            pass  # 业务断言全绿——但游戏内部 push_error 了
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, errors=1)
    output = result.stdout.str()
    assert "npc sheet missing" in output
    assert "res://npc.gd:12" in output


def test_no_push_errors_ignores_warnings(pytester: pytest.Pytester) -> None:
    """warning 级不触发失败（要更严格自己查 bridge.errors()）。"""
    pytester.makeconftest(
        _DIAG_CONFTEST_HEADER
        + """
class FakeBridge:
    def __init__(self, port): pass
    def errors(self, since=0, limit=100):
        if limit == 0:
            return {"marker": 0, "errors": [], "dropped": 0, "truncated": False}
        return {
            "marker": 1,
            "errors": [{"type": "warning", "message": "deprecated thing"}],
            "dropped": 0,
            "truncated": False,
        }
    def release_all(self): pass
    def close(self): pass
"""
        + _DIAG_CONFTEST_FOOTER
    )
    pytester.makepyfile(
        """
        def test_warn_only(no_push_errors):
            pass
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_failure_screenshot_taken_when_not_headless(
    pytester: pytest.Pytester,
) -> None:
    """非 headless + call 阶段失败 + 用了 bridge → 自动截图到 .cli_control/failures/。"""
    pytester.makeconftest(
        _DIAG_CONFTEST_HEADER
        + """
class FakeBridge:
    def __init__(self, port): pass
    def screenshot(self, path):
        CALLS.append(f"screenshot:{path}")
        return b"png"
    def release_all(self): pass
    def close(self): pass
"""
        + _DIAG_CONFTEST_FOOTER
    )
    pytester.makepyfile(
        """
        def test_boom(bridge):
            assert False, "intentional"
        """
    )
    result = pytester.runpytest("-s", "--godot-cli-no-headless")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "screenshot:" in output, "失败后应自动截图"
    assert ".cli_control" in output and "failures" in output
    assert "test_boom" in output.split("screenshot:")[1].splitlines()[0]


# ---------------------------------------------------------------------------
# godot_instances 多实例工厂 fixture（issue #143）
# ---------------------------------------------------------------------------

# 共享假桩：Daemon 记 instance 维度的调用流水，Bridge 记端口维度的；
# PORTS 给每个实例固定不同端口，断言「各连各的」不串台。

_INSTANCES_CONFTEST = """
from __future__ import annotations
import godot_cli_control.pytest_plugin as plugin

CALLS: list = []
PORTS = {"server": 9001, "client1": 9002}

class FakeDaemon:
    def __init__(self, project_root, instance="default", **kw):
        CALLS.append(f"Daemon:{instance}")
        self.instance = instance
    def is_running(self): return False
    def start(self, **kw):
        CALLS.append(
            f"start:{self.instance}:headless={kw.get('headless')},"
            f"port={kw.get('port')},time_scale={kw.get('time_scale')}"
        )
    def stop(self):
        CALLS.append(f"stop:{self.instance}")
        return 0
    def current_port(self): return PORTS.get(self.instance, 9999)

class FakeBridge:
    def __init__(self, port):
        CALLS.append(f"Bridge:{port}")
        self.port = port
    def release_all(self): CALLS.append(f"release_all:{self.port}")
    def close(self): CALLS.append(f"close:{self.port}")

plugin.Daemon = FakeDaemon
plugin.GameBridge = FakeBridge

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    terminalreporter.write_line(f"CALLS={CALLS}")
"""


def test_instances_scope_option_registered_in_help(
    pytester: pytest.Pytester,
) -> None:
    """--godot-cli-instances-scope 出现在 --help，默认 function。"""
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(["*--godot-cli-instances-scope*"])


def test_instances_scope_invalid_value_gives_usage_error(
    pytester: pytest.Pytester,
) -> None:
    """--godot-cli-instances-scope bogus → argparse choices 用法错（exit 4）。"""
    result = pytester.runpytest("--godot-cli-instances-scope", "bogus")
    assert result.ret == 4
    result.stderr.fnmatch_lines(["*invalid choice*"])


def test_instances_two_instances_lifecycle(pytester: pytest.Pytester) -> None:
    """start 两实例：Daemon 各带 instance 名构造、各 start 一次、bridge 各连各端口；
    teardown 对每个实例 release_all → close → stop（close 先于 stop）。
    """
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        def test_two(godot_instances):
            server = godot_instances.start("server")
            client = godot_instances.start("client1")
            assert server is not client
            assert server.port == 9001
            assert client.port == 9002
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    for needle in (
        "Daemon:server",
        "Daemon:client1",
        "'Bridge:9001'",
        "'Bridge:9002'",
        "stop:server",
        "stop:client1",
        "close:9001",
        "close:9002",
    ):
        assert needle in output, f"missing {needle}: {output}"
    assert output.count("start:server") == 1
    assert output.count("start:client1") == 1
    # 每实例：close 先于 stop（先断连接再杀进程）
    calls_line = [ln for ln in output.splitlines() if ln.startswith("CALLS=")][0]
    assert calls_line.index("close:9001") < calls_line.index("stop:server")
    assert calls_line.index("close:9002") < calls_line.index("stop:client1")


def test_instances_start_idempotent(pytester: pytest.Pytester) -> None:
    """同名重复 start 返回同一 bridge 对象，Daemon.start 只调一次（get-or-start）。"""
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        def test_idem(godot_instances):
            a = godot_instances.start("server")
            b = godot_instances.start("server")
            assert a is b
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert output.count("start:server") == 1
    assert output.count("'Bridge:9001'") == 1


def test_instances_daemon_accessor(pytester: pytest.Pytester) -> None:
    """daemon(name) 返回底层 Daemon；未 start 的名字 → KeyError。"""
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        import pytest

        def test_accessor(godot_instances):
            godot_instances.start("server")
            assert godot_instances.daemon("server").current_port() == 9001
            with pytest.raises(KeyError):
                godot_instances.daemon("nope")
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)


def test_instances_global_options_and_per_call_override(
    pytester: pytest.Pytester,
) -> None:
    """headless / time_scale 默认跟全局选项（--godot-cli-time-scale 3 + 默认 headless），
    start() 的关键字参数可逐实例覆盖。port 默认恒 0（OS 自动分配，多实例不能共享
    --godot-cli-port 的固定端口）。
    """
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        def test_opts(godot_instances):
            godot_instances.start("server")
            godot_instances.start("client1", headless=False, time_scale=1.5)
        """
    )
    result = pytester.runpytest("-s", "--godot-cli-time-scale", "3")
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert "start:server:headless=True,port=0,time_scale=3.0" in output, output
    assert "start:client1:headless=False,port=0,time_scale=1.5" in output, output


def test_instances_reuses_running_daemon(pytester: pytest.Pytester) -> None:
    """实例已在跑（开发者手动起的）→ 不重启、teardown 不杀，但连接照常建照常收。"""
    pytester.makeconftest(
        _INSTANCES_CONFTEST.replace(
            "    def is_running(self): return False",
            "    def is_running(self): return True",
        )
    )
    pytester.makepyfile(
        """
        def test_borrow(godot_instances):
            godot_instances.start("server")
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert "start:server" not in output, "已运行的实例不应被重启"
    assert "stop:server" not in output, "fixture 没起的实例 teardown 不该杀它"
    assert "close:9001" in output, "借用的实例 teardown 仍要断开连接"


def test_instances_explicit_stop_allows_restart(
    pytester: pytest.Pytester,
) -> None:
    """stop(name) 中途显式停（掉线类场景）→ 立即 close + stop；之后可重新 start。"""
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        def test_restart(godot_instances):
            godot_instances.start("server")
            godot_instances.stop("server")
            godot_instances.start("server")
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert output.count("start:server") == 2
    assert output.count("stop:server") == 2  # 显式 1 次 + teardown 1 次
    assert output.count("close:9001") == 2


def test_instances_stop_unknown_name_raises(pytester: pytest.Pytester) -> None:
    """stop 一个没 start 过的名字 → KeyError（抓 typo，不静默吞）。"""
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        import pytest

        def test_unknown(godot_instances):
            with pytest.raises(KeyError) as exc_info:
                godot_instances.stop("nope")
            assert "nope" in str(exc_info.value)
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)


def test_instances_start_failure_fails_loud(pytester: pytest.Pytester) -> None:
    """Daemon.start 抛 DaemonError → pytest.fail 透传原始原因（不吞）。"""
    pytester.makeconftest(
        """
        from __future__ import annotations
        import godot_cli_control.pytest_plugin as plugin
        from godot_cli_control.daemon import DaemonError

        class FailDaemon:
            def __init__(self, project_root, instance="default", **kw): pass
            def is_running(self): return False
            def start(self, **kw):
                raise DaemonError("Godot binary not found")
            def stop(self): return 0
            def current_port(self): return None

        class FakeBridge:
            def __init__(self, port): pass
            def release_all(self): pass
            def close(self): pass

        plugin.Daemon = FailDaemon
        plugin.GameBridge = FakeBridge
        """
    )
    pytester.makepyfile(
        """
        def test_boom(godot_instances):
            godot_instances.start("server")
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "Godot binary not found" in output
    assert "server" in output, "失败消息应指明是哪个实例起不来"


def test_instances_teardown_runs_even_when_test_raises(
    pytester: pytest.Pytester,
) -> None:
    """用户测试体抛异常 → 全部已起实例仍被 close + stop（不留泄漏进程）。"""
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        def test_crash(godot_instances):
            godot_instances.start("server")
            godot_instances.start("client1")
            raise RuntimeError("user code went bang")
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    for needle in ("close:9001", "stop:server", "close:9002", "stop:client1"):
        assert needle in output, f"missing {needle}: {output}"


def test_instances_function_scope_isolates_tests_by_default(
    pytester: pytest.Pytester,
) -> None:
    """默认 function scope：两个用例各起各停（互不共享实例）。"""
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        def test_a(godot_instances):
            godot_instances.start("server")

        def test_b(godot_instances):
            godot_instances.start("server")
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=2)
    output = result.stdout.str()
    assert output.count("start:server") == 2
    assert output.count("stop:server") == 2


def test_instances_session_scope_shares_across_tests(
    pytester: pytest.Pytester,
) -> None:
    """--godot-cli-instances-scope session：跨用例共享实例，start 1 次、
    session 末 stop 1 次（联机 e2e 套件起一组 server/client 全程复用）。
    """
    pytester.makeconftest(_INSTANCES_CONFTEST)
    pytester.makepyfile(
        """
        def test_a(godot_instances):
            godot_instances.start("server")

        def test_b(godot_instances):
            godot_instances.start("server")
        """
    )
    result = pytester.runpytest("-s", "--godot-cli-instances-scope", "session")
    result.assert_outcomes(passed=2)
    output = result.stdout.str()
    assert output.count("start:server") == 1
    assert output.count("stop:server") == 1


def test_failure_screenshot_skipped_when_headless(
    pytester: pytest.Pytester,
) -> None:
    """headless（默认）失败不截图——dummy renderer 必 1006，纯浪费。"""
    pytester.makeconftest(
        _DIAG_CONFTEST_HEADER
        + """
class FakeBridge:
    def __init__(self, port): pass
    def screenshot(self, path):
        CALLS.append(f"screenshot:{path}")
        return b"png"
    def release_all(self): pass
    def close(self): pass
"""
        + _DIAG_CONFTEST_FOOTER
    )
    pytester.makepyfile(
        """
        def test_boom(bridge):
            assert False, "intentional"
        """
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(failed=1)
    assert "screenshot:" not in result.stdout.str()
