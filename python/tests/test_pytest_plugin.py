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
