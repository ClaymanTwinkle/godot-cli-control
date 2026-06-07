"""端到端回归：同项目双实例隔离 + 歧义解析 + --instance 选靶。

验证点（全部 headless）：
  1. 同项目启动 server / client1 两实例，端口互不相同（OS 自动分配隔离）。
  2. 用 GameBridge(instance=...) 各自连，tree() 均可成功——证明连接存活且互不串台。
  3. 各自 set/get 不同值，交叉断言不污染——证明 RPC 打到独立 daemon。
  4. discover_port(project) 无 instance 且双实例在跑 → 抛 InstanceAmbiguityError。
  5. discover_port(project, instance="server") 返回 server 实例的端口。
  6. registry.list_all() 包含两条本项目记录，instance 字段互不重叠。
  7. 分别 stop 两实例，stop 后注册表中本项目记录清空。

需要真实 Godot 4：PATH 里有 godot（或设 GODOT_BIN），否则整文件 skip。
截图走 GUI 路径（headless 拿不到 viewport texture），不在此覆盖。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from godot_cli_control import registry
from godot_cli_control.bridge import GameBridge
from godot_cli_control.daemon import (
    Daemon,
    InstanceAmbiguityError,
    discover_port,
    find_godot_binary,
)

_GODOT_BIN = find_godot_binary()
_ADDON_SRC = Path(__file__).resolve().parents[2] / "addons" / "godot_cli_control"

# GODOT_BIN 缺失时整文件 skip（与其它 e2e 约定一致）。
pytestmark = pytest.mark.skipif(
    not _GODOT_BIN,
    reason="需要真实 Godot 4：把 godot 装进 PATH 或设 GODOT_BIN，否则整文件 skip",
)

# ── 最小 Godot 项目模板 ──
# 暴露可读写属性 value（int），供跨实例交叉断言不串台。
# GameBridgeNode autoload + plugin 段与其它 e2e 一致（等价于 init 的 patch）。

_PROJECT_GODOT = """\
config_version=5

[application]
config/name="gcc-multi-instance-e2e"
run/main_scene="res://main.tscn"
config/features=PackedStringArray("4.4")

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"

[autoload]

GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"

[editor_plugins]

enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")
"""

# 主场景：挂带 value 属性的脚本，headless 下直接可读写，无需任何 UI 节点。
_MAIN_TSCN = """\
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://main.gd" id="1_main"]

[node name="Main" type="Node"]
script = ExtResource("1_main")
"""

# 脚本：暴露 value 属性用于交叉断言（不在 property blacklist）。
_MAIN_GD = """\
extends Node

var value: int = 0
"""


@pytest.fixture(scope="module")
def multi_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """构建最小 Godot 项目并做一次编辑器导入（注册 GameBridge class）。

    scope=module：整个文件共用一份 tmp 项目目录，只导入一次——导入耗时最长，
    不必每条用例重复。两个测试函数各自独立管理 daemon 生命周期，互不影响。
    """
    proj = tmp_path_factory.mktemp("gcc_multi_e2e")
    (proj / "project.godot").write_text(_PROJECT_GODOT, encoding="utf-8")
    (proj / "main.tscn").write_text(_MAIN_TSCN, encoding="utf-8")
    (proj / "main.gd").write_text(_MAIN_GD, encoding="utf-8")
    (proj / "addons").mkdir()
    shutil.copytree(_ADDON_SRC, proj / "addons" / "godot_cli_control")

    # 编辑器导入：注册 global class GameBridge，否则 autoload 找不到 class_name。
    imp = subprocess.run(
        [_GODOT_BIN, "--headless", "--editor", "--quit", "--path", str(proj)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert imp.returncode == 0, (
        f"Godot 导入失败：\nstdout={imp.stdout}\nstderr={imp.stderr}"
    )
    return proj


# ────────────────────────────────────────────────────────────────────────────
# 辅助：安全停止（stop 失败也打印 stderr 但不再抛，确保 finally 能完整清理）
# ────────────────────────────────────────────────────────────────────────────

def _safe_stop(d: Daemon, label: str) -> None:
    """安全停止指定 daemon；失败时 print 到 stderr 但不抛异常，确保清理兜底。

    try/finally 里的 stop 失败不应掩盖更上层的断言失败——所以这里吞掉错误，
    只在 stderr 留痕，方便事后 debug。
    """
    try:
        d.stop()
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] stop 失败（已忽略）：{exc}", file=sys.stderr)


# ────────────────────────────────────────────────────────────────────────────
# Test 1：双实例端口隔离 + 歧义解析 + registry 双记录 + stop 后清空
# ────────────────────────────────────────────────────────────────────────────

def test_two_instances_isolated(multi_project: Path) -> None:
    """同项目双实例（server / client1）隔离全链路验证。

    范围：端口互不同 → 各连各的 tree() 成功 → discover_port 无 instance 抛歧义 →
    显式 instance 选靶正确 → registry 双记录 → stop 后记录清空。
    所有 bridge 操作在 headless 模式下进行（无 UI，只验证连接存活 + 属性隔离）。
    """
    project = multi_project

    # 1. 启动双实例（OS 自动分配端口，保证互不碰撞）
    a = Daemon(project, instance="server")
    b = Daemon(project, instance="client1")
    bridge_a: GameBridge | None = None
    bridge_b: GameBridge | None = None

    try:
        a.start(headless=True)
        b.start(headless=True)

        # 端口必须不同——两个 OS 自动分配端口永远不会相同
        port_a = a.current_port()
        port_b = b.current_port()
        assert port_a is not None, "server 实例端口文件未写入"
        assert port_b is not None, "client1 实例端口文件未写入"
        assert port_a != port_b, (
            f"两实例端口相同（{port_a}）——OS 自动分配应保证互不碰撞"
        )

        # 2. 各自连接，用 tree() 验证连接存活且互不串台
        # GameBridge(instance=...) cwd 默认 Path.cwd()，需传 port 以免
        # discover_port(cwd) 可能解析到别的项目；直接用端口最稳。
        bridge_a = GameBridge(port=port_a)
        bridge_b = GameBridge(port=port_b)

        tree_a = bridge_a.get_scene_tree(depth=2)
        tree_b = bridge_b.get_scene_tree(depth=2)
        assert isinstance(tree_a, dict) and tree_a, "server bridge 的 get_scene_tree 应返回非空 dict"
        assert isinstance(tree_b, dict) and tree_b, "client1 bridge 的 get_scene_tree 应返回非空 dict"

        # 3. 交叉断言：各自 set 不同值，读回验证不串台
        bridge_a.set_property("/root/Main", "value", 111)
        bridge_b.set_property("/root/Main", "value", 222)
        # 关闭再重开连接，模拟独立 bridge 读（防止 cache 等）
        bridge_a.close()
        bridge_a = None
        bridge_b.close()
        bridge_b = None

        fresh_a = GameBridge(port=port_a)
        fresh_b = GameBridge(port=port_b)
        try:
            val_a = fresh_a.get_property("/root/Main", "value")
            val_b = fresh_b.get_property("/root/Main", "value")
        finally:
            fresh_a.close()
            fresh_b.close()

        assert val_a == 111, (
            f"server 实例 value 应为 111（set 的值），实际 {val_a!r}——两实例可能串台"
        )
        assert val_b == 222, (
            f"client1 实例 value 应为 222（set 的值），实际 {val_b!r}——两实例可能串台"
        )

        # 4. 歧义：discover_port(project) 无 instance → InstanceAmbiguityError
        # 两实例都在跑，不指定 instance 时必须报歧义让调用方明确选靶。
        with pytest.raises(InstanceAmbiguityError) as exc_info:
            discover_port(project)
        # 错误信息应包含两个实例名，让 agent 看单行 JSON 就知道怎么选
        err_msg = str(exc_info.value)
        assert "server" in err_msg, f"歧义错误信息应含 'server'，实际：{err_msg!r}"
        assert "client1" in err_msg, f"歧义错误信息应含 'client1'，实际：{err_msg!r}"

        # 5. 显式选靶：discover_port(project, instance="server") == server 端口
        found_port = discover_port(project, instance="server")
        assert found_port == port_a, (
            f"discover_port(..., instance='server') 应返回 {port_a}，实际 {found_port}"
        )

        # 6. registry 双记录：两条记录 instance 字段互不重叠
        recs = [
            r for r in registry.list_all()
            if Path(r.project_root) == project.resolve()
        ]
        assert len(recs) == 2, (
            f"注册表中本项目应有 2 条记录，实际 {len(recs)} 条：{recs}"
        )
        instances_in_registry = {r.instance for r in recs}
        assert instances_in_registry == {"server", "client1"}, (
            f"注册表 instance 字段应为 {{'server', 'client1'}}，实际 {instances_in_registry}"
        )

    finally:
        # 兜底清理：哪怕断言失败也必须 stop 两实例，不留泄漏进程
        if bridge_a is not None:
            try:
                bridge_a.close()
            except Exception:  # noqa: BLE001
                pass
        if bridge_b is not None:
            try:
                bridge_b.close()
            except Exception:  # noqa: BLE001
                pass
        _safe_stop(a, "server")
        _safe_stop(b, "client1")

    # 7. stop 后注册表中本项目记录应清空（在 finally 外断言，确保 stop 已完成）
    remaining = [
        r for r in registry.list_all()
        if Path(r.project_root) == project.resolve()
    ]
    assert not remaining, (
        f"stop 后注册表应清空本项目记录，仍剩 {remaining}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Test 2：库面双连接互不污染（--instance 选靶 + 值隔离）
# ────────────────────────────────────────────────────────────────────────────

def test_instance_targeting_no_crosstalk(multi_project: Path) -> None:
    """库面双 bridge 交叉写/读断言：server 写 100、client1 写 200，交叉读不混淆。

    补充 Test 1 的"关闭重开再读"路径，本条在同一组 bridge 对象上连续操作，
    验证相同 bridge 持续连接也不会把 value 写串到另一个实例。

    选靶方式：``GameBridge(port=...)`` 显式端口（从 Daemon.current_port() 读取）。
    ``GameBridge(instance=...)`` 路径由单测覆盖，不在此重复。
    """
    project = multi_project

    a = Daemon(project, instance="server")
    b = Daemon(project, instance="client1")
    bridge_a: GameBridge | None = None
    bridge_b: GameBridge | None = None

    try:
        a.start(headless=True)
        b.start(headless=True)

        port_a = a.current_port()
        port_b = b.current_port()
        assert port_a is not None and port_b is not None

        bridge_a = GameBridge(port=port_a)
        bridge_b = GameBridge(port=port_b)

        # 初始化：两边都重置为 0，避免前一条测试遗留值干扰
        bridge_a.set_property("/root/Main", "value", 0)
        bridge_b.set_property("/root/Main", "value", 0)

        # server 写 100，client1 写 200
        bridge_a.set_property("/root/Main", "value", 100)
        bridge_b.set_property("/root/Main", "value", 200)

        # 各自读回自己写的值，互不污染
        # bridge_a 连 server 实例，应读到 100；bridge_b 连 client1 实例，应读到 200
        v_a = bridge_a.get_property("/root/Main", "value")
        v_b = bridge_b.get_property("/root/Main", "value")
        assert v_a == 100, f"server value 应为 100，实际 {v_a!r}（串台了？）"
        assert v_b == 200, f"client1 value 应为 200，实际 {v_b!r}（串台了？）"

    finally:
        if bridge_a is not None:
            try:
                bridge_a.close()
            except Exception:  # noqa: BLE001
                pass
        if bridge_b is not None:
            try:
                bridge_b.close()
            except Exception:  # noqa: BLE001
                pass
        _safe_stop(a, "server")
        _safe_stop(b, "client1")

    # 7. stop 后注册表中本项目记录应清空（在 finally 外断言，确保 stop 已完成）
    remaining = [
        r for r in registry.list_all()
        if Path(r.project_root) == project.resolve()
    ]
    assert not remaining, (
        f"stop 后注册表应清空本项目记录，仍剩 {remaining}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Test 3：--instance all 广播（#145）——CLI 子进程全链路
# ────────────────────────────────────────────────────────────────────────────

def _cli(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """以 project 为 cwd 跑 CLI 子进程（广播按 cwd 枚举实例）。"""
    return subprocess.run(
        [sys.executable, "-m", "godot_cli_control.cli", *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_broadcast_exists_and_per_instance_values(multi_project: Path) -> None:
    """--instance all 全链路：双实例广播 exists rc=0；先各自 set 不同值再广播
    get，entry 值互不串台——证明 fan-out 真打到两个独立 daemon。"""
    project = multi_project
    a = Daemon(project, instance="server")
    b = Daemon(project, instance="client1")
    try:
        a.start(headless=True)
        b.start(headless=True)

        # 1. 广播 exists：双实例命中，聚合 rc=0、退出码 0，数组按名排序
        r = _cli(project, "--instance", "all", "exists", "/root/Main")
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        payload = json.loads(r.stdout)
        assert payload["ok"] is True
        insts = payload["result"]["instances"]
        assert [e["instance"] for e in insts] == ["client1", "server"]
        assert all(e["ok"] is True and e["result"] is True for e in insts)
        assert payload["result"]["rc"] == 0

        # 2. 各自 set 不同值（单靶路径），广播 get 读回互不串台
        assert _cli(project, "--instance", "server", "set",
                    "/root/Main", "value", "111").returncode == 0
        assert _cli(project, "--instance", "client1", "set",
                    "/root/Main", "value", "222").returncode == 0
        r = _cli(project, "--instance", "all", "get", "/root/Main", "value")
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        by_name = {
            e["instance"]: e for e in json.loads(r.stdout)["result"]["instances"]
        }
        # get 单属性的 result 形状是 {"value": ..., "type"?}（cli.py cmd_get docstring）
        assert by_name["server"]["result"]["value"] == 111
        assert by_name["client1"]["result"]["value"] == 222

        # 3. 广播缺失节点：两实例 result=false → 各 entry rc=1，聚合退出码 3
        r = _cli(project, "--instance", "all", "exists", "/root/Nope")
        assert r.returncode == 3, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        payload = json.loads(r.stdout)
        assert payload["result"]["rc"] == 3
        assert all(e["rc"] == 1 for e in payload["result"]["instances"])
    finally:
        _safe_stop(a, "server")
        _safe_stop(b, "client1")
