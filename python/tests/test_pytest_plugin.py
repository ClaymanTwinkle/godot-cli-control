"""``godot_cli_control.pytest_plugin`` 的结构性单测。

不真启 Godot —— 用 pytester 在子 pytest 里 monkey-patch Daemon /
GameBridge，验证 fixture 顺序、teardown 行为与 CLI 选项注册。
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


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
    """`pytest --fixtures` 列出 godot_daemon + bridge。"""
    pytester.makepyfile(
        test_smoke="""
        def test_smoke():
            pass
        """
    )
    result = pytester.runpytest("--fixtures")
    result.stdout.fnmatch_lines(["*godot_daemon*", "*bridge*"])


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
            terminalreporter.write_line(f"SEEN_ROOTS={SEEN_ROOTS}")
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
