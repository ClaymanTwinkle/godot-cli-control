# `tree [path] [depth]` 子树查询 + 文档对齐 实施计划（issue #150）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `tree` 命令加可选 path 位置参数（启发式：`/` 前缀当 path，否则当 depth），让 agent 能 dump 任意子树（含 autoload 挂 `/root` 的兄弟），并把文档承诺的 `tree <subpath>` 坐实。

**Architecture:** 改动贯穿五层——服务端 GDScript（按 path 选根）、Python client（params 加 path）、Python bridge（透传 path）、CLI（双可选位置参数 + preflight 消歧 + handler）、文档（SKILL.md 模板 + addon README + 重渲染仓内副本）。默认根维持 `current_scene`，纯增量，零破坏；错误码全复用（`1001`/`-1003`/`1005`）不撞码。

**Tech Stack:** Python 3.10+（argparse / asyncio / pytest-asyncio）、GDScript 4（GUT 单测）、`coverage run -m pytest`、`run_gut.py`。

参考 spec：`docs/superpowers/specs/2026-06-08-tree-path-root-design.md`

---

## 文件结构

| 文件 | 职责 | 改动 |
|------|------|------|
| `addons/godot_cli_control/bridge/low_level_api.gd` | `handle_get_scene_tree` 根解析 | 改 427–429 行的根选择逻辑 |
| `addons/godot_cli_control/tests/gut/test_low_level_api.gd` | GUT 单测 | 新增 3 个 scene-tree path 测试 |
| `python/godot_cli_control/client.py` | `GameClient.get_scene_tree` | 加 `path` 形参 + params |
| `python/tests/test_client.py` | client 单测 | 新增 2 个 path 测试 |
| `python/godot_cli_control/bridge.py` | `GameBridge.tree` / `get_scene_tree` | 加 `path` 透传 |
| `python/tests/test_bridge.py` | bridge 单测 + `_StubClient` | stub 加 path 记录 + 新增 1 测试 |
| `python/godot_cli_control/cli.py` | 参数注册 / preflight / handler / RpcSpec | 重写 `_register_tree_args`、新增 `_preflight_tree`、改 `cmd_tree`、改 tree `RpcSpec` |
| `python/tests/test_cli.py` | CLI 单测 | 新增 preflight 消歧矩阵 + 用法错 + cmd_tree 透传 |
| `python/godot_cli_control/templates/skill/SKILL.md` | agent 入口文档 | 4 处措辞对齐 |
| `addons/godot_cli_control/README.md` | addon 文档 | API 表 1 处 |
| `.claude/skills/godot-cli-control/SKILL.md` | 仓内渲染副本 | 重渲染（脚本生成，勿手改） |

---

## Task 1: 服务端 `handle_get_scene_tree` 接受可选 `path`（GDScript）

**Files:**
- Modify: `addons/godot_cli_control/bridge/low_level_api.gd:427-429`
- Test: `addons/godot_cli_control/tests/gut/test_low_level_api.gd`（在约 716 行 scene-tree 测试组后追加）

- [ ] **Step 1: 写失败的 GUT 测试**

在 `test_low_level_api.gd` 的 `test_handle_get_scene_tree_negative_max_nodes_falls_back_to_limit`（约 716 行）之后追加：

```gdscript
func test_handle_get_scene_tree_with_path_returns_subtree() -> void:
	# issue #150：传 path 时以该节点为子树根（从 /root 解析，与 children 同世界观）。
	# _target 是 before_each 挂在测试树下的节点，其 get_path() 是 /root 起的绝对路径，
	# 不在 current_scene 之下——正好验证 autoload-on-/root 那类兄弟子树也能取到。
	var result: Dictionary = _api.handle_get_scene_tree({
		"path": str(_target.get_path()),
		"depth": 3,
	})
	assert_does_not_have(result, "error")
	assert_has(result, "tree")
	assert_eq(str(result.tree.name), "GutTestTarget")


func test_handle_get_scene_tree_bad_path_returns_node_not_found() -> void:
	# issue #150：path 不存在 → 复用 1001 NODE_NOT_FOUND，与 children 一致，不新增码。
	var result: Dictionary = _api.handle_get_scene_tree({
		"path": "/root/DefinitelyDoesNotExist_150",
	})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)  # CliControlErrorCodes.NODE_NOT_FOUND


func test_handle_get_scene_tree_without_path_keeps_current_scene_root() -> void:
	# issue #150 回归：不传 path 时默认根维持 current_scene（fallback root），行为不变。
	var result: Dictionary = _api.handle_get_scene_tree({"depth": 2})
	assert_does_not_have(result, "error")
	assert_has(result, "tree")
```

- [ ] **Step 2: 跑测试确认失败**

委托 subagent（`model: sonnet`，**禁后台**）运行：
Run: `GODOT_BIN=~/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py`
Expected: `test_handle_get_scene_tree_with_path_returns_subtree` 与 `..._bad_path_...` FAIL（当前 handler 忽略 path，返回的是 current_scene 树 / 不报 1001）；`..._without_path_...` 可能已 PASS。

- [ ] **Step 3: 改根解析逻辑**

把 `low_level_api.gd` 的 427–429 行：

```gdscript
	var root: Node = get_tree().current_scene
	if root == null:
		root = get_tree().root
```

替换为：

```gdscript
	# issue #150：传 path 时以该节点为子树根（从 /root 解析，与 children/_get_node_or_error
	# 同世界观）；不传 path 时维持 current_scene 默认根（fallback /root），行为不变。
	var path: String = params.get("path", "") as String
	var root: Node
	if not path.is_empty():
		root = get_tree().root.get_node_or_null(path)
		if root == null:
			return _node_not_found(path)
	else:
		root = get_tree().current_scene
		if root == null:
			root = get_tree().root
```

- [ ] **Step 4: 跑测试确认通过**

Run（subagent，禁后台）: `GODOT_BIN=~/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py`
Expected: 全部 GUT 测试 PASS（含新增 3 个 + 原有 scene-tree 回归）。

- [ ] **Step 5: 提交**

```bash
git add addons/godot_cli_control/bridge/low_level_api.gd addons/godot_cli_control/tests/gut/test_low_level_api.gd
git commit -m "$(cat <<'EOF'
feat(bridge): handle_get_scene_tree 支持可选 path 子树根（#150）

传 path 从 /root 解析子树根（不存在 → 1001，复用 NODE_NOT_FOUND）；
不传维持 current_scene 默认根，行为不变。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 客户端 `get_scene_tree(path=None)`（Python）

**Files:**
- Modify: `python/godot_cli_control/client.py:373-385`
- Test: `python/tests/test_client.py`（在 `test_get_scene_tree_omits_max_nodes_when_none` 后追加，约 528 行）

- [ ] **Step 1: 写失败的测试**

在 `test_client.py` 约 528 行后追加：

```python
@pytest.mark.asyncio
async def test_get_scene_tree_includes_path_when_set() -> None:
    """issue #150：path 非 None 时进 RPC params。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["params"] = params
        return {"tree": {"name": "GameUI", "type": "Control", "path": "/root/GameUI"}}

    client = client_mod.GameClient(port=1)
    monkeypatch_target = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.get_scene_tree(depth=2, path="/root/GameUI")
    finally:
        client_mod.GameClient.request = monkeypatch_target
    assert captured["params"] == {"depth": 2, "path": "/root/GameUI"}


@pytest.mark.asyncio
async def test_get_scene_tree_omits_path_when_none() -> None:
    """issue #150：path=None 时 params 不带 path，保留旧客户端兼容路径。"""
    import godot_cli_control.client as client_mod

    captured: dict = {}

    async def fake_request(self, method, params=None, timeout=30.0):
        captured["params"] = params
        return {"tree": {"name": "root", "type": "Node", "path": "/root"}}

    client = client_mod.GameClient(port=1)
    monkeypatch_target = client_mod.GameClient.request
    client_mod.GameClient.request = fake_request  # type: ignore
    try:
        await client.get_scene_tree(depth=2)
    finally:
        client_mod.GameClient.request = monkeypatch_target
    assert "path" not in captured["params"]
```

- [ ] **Step 2: 跑测试确认失败**

Run（subagent，禁后台）: `coverage run -m pytest python/tests/test_client.py::test_get_scene_tree_includes_path_when_set -v`
Expected: FAIL —— `get_scene_tree()` 不接受 `path` 关键字参数（`TypeError`）。

- [ ] **Step 3: 加 path 形参**

把 `client.py:373-385` 的 `get_scene_tree`：

```python
    async def get_scene_tree(
        self, depth: int = 5, max_nodes: int | None = None
    ) -> dict:
        """读取场景树（可选软上限）。

        ``max_nodes``：``None`` 走服务端默认（硬墙 5000，无 ``truncated`` 字段）；
        传正整数 N 时，节点数超 N 时响应附加 ``{"truncated": true, "total_nodes": M}``。
        服务端会把传入值 clamp 到 5000 上限，超过仍走 1005 错误。
        """
        params: dict = {"depth": depth}
        if max_nodes is not None:
            params["max_nodes"] = max_nodes
        return await self.request("get_scene_tree", params)
```

替换为：

```python
    async def get_scene_tree(
        self, depth: int = 5, max_nodes: int | None = None, path: str | None = None
    ) -> dict:
        """读取场景树（可选软上限、可选子树根）。

        ``max_nodes``：``None`` 走服务端默认（硬墙 5000，无 ``truncated`` 字段）；
        传正整数 N 时，节点数超 N 时响应附加 ``{"truncated": true, "total_nodes": M}``。
        服务端会把传入值 clamp 到 5000 上限，超过仍走 1005 错误。

        ``path``（issue #150）：``None`` 时根为当前场景（``current_scene``）；
        传绝对节点路径（如 ``/root/GameUI``）则以该节点为子树根，从 ``/root``
        解析（与 ``get_children`` 同世界观）；路径不存在时服务端返回 1001。
        """
        params: dict = {"depth": depth}
        if max_nodes is not None:
            params["max_nodes"] = max_nodes
        if path is not None:
            params["path"] = path
        return await self.request("get_scene_tree", params)
```

- [ ] **Step 4: 跑测试确认通过**

Run（subagent，禁后台）: `coverage run -m pytest python/tests/test_client.py -k get_scene_tree -v`
Expected: 4 个 get_scene_tree 测试全 PASS（含原有 2 + 新增 2）。

- [ ] **Step 5: 提交**

```bash
git add python/godot_cli_control/client.py python/tests/test_client.py
git commit -m "$(cat <<'EOF'
feat(client): get_scene_tree 加可选 path 子树根（#150）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: bridge `tree(path=None)` 透传（Python）

**Files:**
- Modify: `python/godot_cli_control/bridge.py:66-72`
- Test: `python/tests/test_bridge.py`（改 `_StubClient.get_scene_tree` 约 50 行 + 在约 260 行后追加测试）

- [ ] **Step 1: 写失败的测试 + 更新 stub 签名**

先把 `test_bridge.py:50-54` 的 stub 方法：

```python
    async def get_scene_tree(self, depth: int = 5, max_nodes: int | None = None) -> dict:
        kwargs: dict = {"depth": depth}
        if max_nodes is not None:
            kwargs["max_nodes"] = max_nodes
        return self._record("get_scene_tree", (), kwargs)
```

替换为（加 path 记录，None 时不记，保持现有断言不变）：

```python
    async def get_scene_tree(
        self, depth: int = 5, max_nodes: int | None = None, path: str | None = None
    ) -> dict:
        kwargs: dict = {"depth": depth}
        if max_nodes is not None:
            kwargs["max_nodes"] = max_nodes
        if path is not None:
            kwargs["path"] = path
        return self._record("get_scene_tree", (), kwargs)
```

然后在 `test_tree_forwards_max_nodes_to_client`（约 260 行）后追加：

```python
def test_tree_forwards_path_to_client(stub_client: dict) -> None:
    """issue #150：bridge.tree(path=...) 必须把 path 透传到 GameClient。"""
    b, c = _make_bridge(stub_client)
    c.returns["get_scene_tree"] = {}
    b.tree(path="/root/GameUI")
    assert c.calls[-1] == ("get_scene_tree", (), {"depth": 3, "path": "/root/GameUI"})
    b.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run（subagent，禁后台）: `coverage run -m pytest python/tests/test_bridge.py::test_tree_forwards_path_to_client -v`
Expected: FAIL —— `GameBridge.tree()` 不接受 `path` 关键字参数（`TypeError`）。

- [ ] **Step 3: bridge 加 path 透传**

把 `bridge.py:66-72`：

```python
    def tree(self, depth: int = 3, max_nodes: int | None = None) -> dict:
        """获取场景树。"""
        return self._run(self._client.get_scene_tree(depth=depth, max_nodes=max_nodes))

    def get_scene_tree(self, depth: int = 3, max_nodes: int | None = None) -> dict:
        """``tree`` 的别名，与 ``GameClient.get_scene_tree`` 同名对齐（issue #60）。"""
        return self.tree(depth=depth, max_nodes=max_nodes)
```

替换为：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run（subagent，禁后台）: `coverage run -m pytest python/tests/test_bridge.py -k tree -v`
Expected: 所有 tree 相关 bridge 测试 PASS（含原有 default/explicit/max_nodes/alias + 新增 path）。

- [ ] **Step 5: 提交**

```bash
git add python/godot_cli_control/bridge.py python/tests/test_bridge.py
git commit -m "$(cat <<'EOF'
feat(bridge): GameBridge.tree 透传可选 path（#150）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: CLI 双位置参数 + `_preflight_tree` 消歧 + handler（Python）

**Files:**
- Modify: `python/godot_cli_control/cli.py` —— `_register_tree_args`（约 754）、新增 `_preflight_tree`（放在 `_preflight_step_frames` 后约 337 行）、`cmd_tree`（约 421）、tree `RpcSpec`（约 909）
- Test: `python/tests/test_cli.py`（在 step-frames preflight 测试组附近追加）

- [ ] **Step 1: 写失败的测试**

在 `test_cli.py` 末尾（或 step-frames preflight 测试组后）追加：

```python
@pytest.mark.parametrize(
    "argv,expect_path,expect_depth",
    [
        (["tree"], None, 3),
        (["tree", "2"], None, 2),
        (["tree", "/root/GameUI"], "/root/GameUI", 3),
        (["tree", "/root/GameUI", "2"], "/root/GameUI", 2),
    ],
)
def test_tree_preflight_disambiguates(
    argv: list[str], expect_path: str | None, expect_depth: int
) -> None:
    """issue #150：/ 前缀当 path，否则当 depth；结果 stash 到 ns。"""
    from godot_cli_control.cli import _preflight_tree, build_parser

    ns = build_parser().parse_args(argv)
    _preflight_tree(ns)
    assert ns._tree_path == expect_path
    assert ns._tree_depth == expect_depth


@pytest.mark.parametrize(
    "bad_argv",
    [
        ["tree", "GameUI"],   # 漏斜杠 → 当 depth → int 失败（fail-loud）
        ["tree", "2", "3"],   # depth-only 形式带多余尾随 token
        ["tree", "abc"],      # 非法 depth
    ],
)
def test_tree_preflight_rejects_usage_errors(
    bad_argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #150：tree 用法错必须在连 daemon 之前报 EXIT_USAGE（-1003）。"""
    import json as _json

    import godot_cli_control.cli as cli_mod
    from godot_cli_control.cli import EXIT_USAGE, main

    class _ShouldNotConnect:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("preflight 失效：tree 用法错时不应连 daemon")

    monkeypatch.setattr(cli_mod, "GameClient", _ShouldNotConnect)
    monkeypatch.setattr(sys, "argv", ["godot-cli-control", *bad_argv])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == EXIT_USAGE
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003


@pytest.mark.asyncio
async def test_cmd_tree_forwards_path_and_depth() -> None:
    """issue #150：cmd_tree 把 preflight 解析出的 path/depth 透传给 client。"""
    from godot_cli_control.cli import _preflight_tree, build_parser, cmd_tree

    captured: dict = {}

    class _FakeClient:
        async def get_scene_tree(
            self, depth: int, max_nodes: int | None = None, path: str | None = None
        ) -> dict:
            captured.update(depth=depth, max_nodes=max_nodes, path=path)
            return {"tree": {}}

    ns = build_parser().parse_args(["tree", "/root/GameUI", "2"])
    _preflight_tree(ns)
    await cmd_tree(_FakeClient(), ns)
    assert captured == {"depth": 2, "max_nodes": 200, "path": "/root/GameUI"}
```

> 注：`sys` / `Any` / `pytest` 在 `test_cli.py` 顶部已 import（与 step-frames 测试同款），无需重复。

- [ ] **Step 2: 跑测试确认失败**

Run（subagent，禁后台）: `coverage run -m pytest python/tests/test_cli.py -k tree_preflight -v`
Expected: FAIL —— `_preflight_tree` 不存在（`ImportError`）；解析也因 `ns` 无 `tree_arg1`/`_tree_path` 失败。

- [ ] **Step 3a: 重写 `_register_tree_args`**

把 `cli.py:754-769` 的 `_register_tree_args`：

```python
def _register_tree_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "depth",
        nargs="?",
        default=None,
        help="遍历深度，默认 3",
    )
    p.add_argument(
        "--max-nodes",
        type=int,
        default=200,
        help=(
            "节点数软上限（默认 200）。超出时服务端截断子节点并返回 "
            "{truncated: true, total_nodes: N}，agent 据此决定是否拆分子树。"
        ),
    )
```

替换为（两个可选位置参数 + 不动 `--max-nodes`）：

```python
def _register_tree_args(p: argparse.ArgumentParser) -> None:
    # issue #150：第一个位置参数以 / 开头当 node path（子树根），否则当 depth。
    # 消歧 + 校验在 _preflight_tree 里做（argparse 无法靠内容区分两个可选位置参数）。
    p.add_argument(
        "tree_arg1",
        nargs="?",
        default=None,
        metavar="path-or-depth",
        help=(
            "可选：节点绝对路径（以 / 开头，如 /root/GameUI）查该子树根；"
            "或遍历深度（整数，默认 3）。省略则 dump 当前场景。"
        ),
    )
    p.add_argument(
        "tree_arg2",
        nargs="?",
        default=None,
        metavar="depth",
        help="可选：遍历深度（仅当第一个参数是路径时有意义，默认 3）",
    )
    p.add_argument(
        "--max-nodes",
        type=int,
        default=200,
        help=(
            "节点数软上限（默认 200）。超出时服务端截断子节点并返回 "
            "{truncated: true, total_nodes: N}，agent 据此决定是否拆分子树。"
        ),
    )
```

- [ ] **Step 3b: 新增 `_preflight_tree`**

在 `cli.py` 的 `_preflight_step_frames`（约 335-336 行）之后插入：

```python
def _preflight_tree(ns: argparse.Namespace) -> None:
    """tree 位置参数消歧 + depth 校验，结果 stash 到 ns（连 daemon 前跑，issue #150）。

    第一个位置参数以 / 开头 → 当 node path（子树根，从 /root 解析）；否则当 depth。
    depth-only 形式不接受第二个位置参数；漏斜杠的路径（如 ``tree GameUI``）会被当
    depth 解析失败而 fail-loud，不静默吞掉。
    """
    arg1 = ns.tree_arg1
    arg2 = ns.tree_arg2
    if arg1 is not None and arg1.startswith("/"):
        path: str | None = arg1
        depth_token = arg2
    else:
        path = None
        depth_token = arg1
        if arg2 is not None:
            raise ValueError(
                f"tree: 多余的参数 {arg2!r}；节点路径须以 / 开头"
                f"（如 tree /root/GameUI 2），否则只接受单个 depth"
            )
    depth = 3
    if depth_token is not None:
        try:
            depth = int(depth_token)
        except (TypeError, ValueError):
            raise ValueError(
                f"tree: depth 必须是整数，收到 {depth_token!r}"
                f"（要查子树请用绝对路径，如 tree /root/GameUI）"
            )
        if depth < 0:
            raise ValueError(f"tree: depth 必须 >= 0，收到 {depth}")
    ns._tree_path = path
    ns._tree_depth = depth
```

- [ ] **Step 3c: 改 `cmd_tree`**

把 `cli.py:421-423`：

```python
async def cmd_tree(client: GameClient, ns: argparse.Namespace) -> dict:
    depth = int(ns.depth) if ns.depth else 3
    return await client.get_scene_tree(depth=depth, max_nodes=ns.max_nodes)
```

替换为（读 preflight stash 的值）：

```python
async def cmd_tree(client: GameClient, ns: argparse.Namespace) -> dict:
    # ns._tree_path / ns._tree_depth 由 _preflight_tree 解析填入（连 daemon 前）。
    return await client.get_scene_tree(
        depth=ns._tree_depth, max_nodes=ns.max_nodes, path=ns._tree_path
    )
```

- [ ] **Step 3d: 改 tree `RpcSpec`**

把 `cli.py:909-917` 的 tree `RpcSpec`：

```python
    RpcSpec(
        name="tree",
        handler=cmd_tree,
        description="dump 当前场景树为 JSON。",
        positionals=(),  # 由 extra_args 注册（depth + --max-nodes）
        example="tree 3",
        extra_args=_register_tree_args,
        text_formatter=_fmt_tree_text,
    ),
```

替换为：

```python
    RpcSpec(
        name="tree",
        handler=cmd_tree,
        description="dump 场景树为 JSON（省略 path 取当前场景，传 /root 起的路径取子树）。",
        positionals=(),  # 由 extra_args 注册（path-or-depth + depth + --max-nodes）
        example="tree /root/GameUI 2",
        extra_args=_register_tree_args,
        preflight=_preflight_tree,
        text_formatter=_fmt_tree_text,
    ),
```

- [ ] **Step 4: 跑测试确认通过**

Run（subagent，禁后台）: `coverage run -m pytest python/tests/test_cli.py -k "tree" -v`
Expected: 新增的 disambiguate 矩阵（4）、usage-error（3）、cmd_tree 透传（1）全 PASS；原有 tree 相关测试不回归。

- [ ] **Step 5: 提交**

```bash
git add python/godot_cli_control/cli.py python/tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): tree [path] [depth] 启发式位置参数 + preflight 消歧（#150）

/ 前缀当 path（子树根）否则当 depth；漏斜杠/多余 token/非法 depth
在连 daemon 前报 -1003/64。文档承诺的 tree <subpath> 坐实。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 文档同步 + 重渲染仓内 SKILL.md

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`（4 处）
- Modify: `addons/godot_cli_control/README.md:94`
- Regenerate: `.claude/skills/godot-cli-control/SKILL.md`（脚本生成）

- [ ] **Step 1: 改 SKILL.md 模板命令表（约 L262）**

```
- `tree [depth] [--max-nodes N]` — full scene tree (default `--max-nodes 200`; on overflow, response includes `truncated: true` and `total_nodes: N`)
```

改为：

```
- `tree [path] [depth] [--max-nodes N]` — scene tree as JSON. Omit `path` → current scene root; pass an absolute node path (starts with `/`, e.g. `tree /root` or `tree /root/GameUI 2`) to dump that subtree — this is how you reach autoload singletons mounted under `/root` (siblings of the current scene). First arg starting with `/` is the path, otherwise it's the depth (so `tree 2` still means depth 2). Default `--max-nodes 200`; on overflow, response includes `truncated: true` and `total_nodes: N`.
```

- [ ] **Step 2: 改 SKILL.md 模板 1005 指引（约 L222）**

把：

```
| `1005` | Scene tree too large to serialize (default safety limit). Pass `--max-nodes` or query a subtree with `children` / `tree <subpath>`. Don't retry as-is. |
```

改为（措辞与新参数一致）：

```
| `1005` | Scene tree too large to serialize (default safety limit). Pass `--max-nodes` or query a subtree with `tree <path>` / `children <path>`. Don't retry as-is. |
```

- [ ] **Step 3: 改 SKILL.md 模板 truncation 段（约 L394-396）**

把：

```
- `tree --max-nodes 50` — quick overview
- `children /root/Game/Spawner` — drill into one branch
- `tree 1` — depth-1 only
```

改为：

```
- `tree --max-nodes 50` — quick overview
- `tree /root/Game/Spawner` — dump just that subtree
- `children /root/Game/Spawner` — drill into one branch
- `tree 1` — depth-1 only
```

- [ ] **Step 4: 改 SKILL.md 模板 Python API 表（约 L472）**

把：

```
| `await client.get_scene_tree(depth, max_nodes=None)` | `tree [depth] [--max-nodes N]` |
```

改为：

```
| `await client.get_scene_tree(depth, max_nodes=None, path=None)` | `tree [path] [depth] [--max-nodes N]` |
```

- [ ] **Step 5: 改 SKILL.md 模板 pitfall（约 L588）**

把：

```
- **`tree` returns `1005 "scene tree too large"`** — your scene has more than 5000 visible nodes (a Grid / spawned-bullets situation). Pass `--max-nodes 200` to cap, or `children <path>` for one specific subtree.
```

改为：

```
- **`tree` returns `1005 "scene tree too large"`** — your scene has more than 5000 visible nodes (a Grid / spawned-bullets situation). Pass `--max-nodes 200` to cap, or `tree <path>` / `children <path>` for one specific subtree.
```

- [ ] **Step 6: 改 addon README API 表（约 L94）**

把：

```
| `get_scene_tree(depth)` | `await client.get_scene_tree(depth=3)` |
```

改为：

```
| `get_scene_tree(depth, path=None)` | `await client.get_scene_tree(depth=3, path="/root/GameUI")` — omit `path` → current scene; CLI: `tree [path] [depth]` |
```

- [ ] **Step 7: 重渲染仓内副本 + 验证无漂移**

在仓库根用 **Python 3.12**（必须，argparse 折行随版本变；CI 以 3.12 为准）跑：

Run:
```bash
COLUMNS=80 python3.12 -c "from godot_cli_control import cli; from godot_cli_control.skills_install import render_skill; from godot_cli_control._version import version; open('.claude/skills/godot-cli-control/SKILL.md','w').write(render_skill(version, cli.format_full_help()))"
```
Expected: `.claude/skills/godot-cli-control/SKILL.md` 被覆盖；`git diff --stat` 显示该文件变更（命令表 / API 表 / `{{cli_help}}` 注入的 tree usage 同步更新）。

再确认 init 注入渲染不崩：
Run（subagent，禁后台）: `coverage run -m pytest python/tests/test_skills_install.py -v`
Expected: 全 PASS。

> 若本机无 `python3.12`：先 `git add` 模板/README/代码改动并提交，重渲染留到验证阶段在有 3.12 的环境补；**但** PR 前必须完成，否则 CI `skill-render-drift` 会红。

- [ ] **Step 8: 提交**

```bash
git add python/godot_cli_control/templates/skill/SKILL.md addons/godot_cli_control/README.md .claude/skills/godot-cli-control/SKILL.md
git commit -m "$(cat <<'EOF'
docs: tree [path] [depth] 同步 SKILL.md/README + 重渲染仓内副本（#150）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 全量验证 + CHANGELOG + 开 PR

**Files:**
- Modify: `CHANGELOG.md`（`[Unreleased]` 段）

- [ ] **Step 1: CHANGELOG 记用户可见变更**

在 `CHANGELOG.md` 的 `[Unreleased]` 段加（归到 Fixed 或 Added，按现有分节）：

```markdown
- `tree` 新增可选 path 位置参数：`tree /root/GameUI [depth]` dump 任意子树，
  含 autoload 挂 `/root` 的兄弟（此前只能看 current_scene）；第一个参数以 `/`
  开头当路径否则当 depth，`tree <depth>` 旧用法不变；路径不存在报 1001、用法错
  报 64（#150）。
```

- [ ] **Step 2: 全量测试 + lint（subagent，禁后台）**

委托 subagent（`model: sonnet`）依次跑，主会话只收结论：
- `coverage run -m pytest && coverage report`（覆盖率 ≥ 80% 门槛）
- `GODOT_BIN=~/.local/bin/godot python addons/godot_cli_control/tests/run_gut.py`
- `ruff check python/`（本机 .venv 可能没装 ruff，按需 `pip install ruff` 临时校验）
- skill 漂移 parity：
  `COLUMNS=80 python3.12 -c "from godot_cli_control import cli; from godot_cli_control.skills_install import render_skill; from godot_cli_control._version import version; print(render_skill(version, cli.format_full_help()))" | diff - .claude/skills/godot-cli-control/SKILL.md`（应无差异）

Expected: pytest 全绿 + 覆盖率达标；GUT 全绿；ruff 干净；skill diff 为空。

- [ ] **Step 3: 提交 CHANGELOG + 推分支 + 开 PR**

```bash
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs(changelog): tree [path] 子树查询（#150）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
git push -u origin fix/150-tree-path-subtree
gh pr create --base main --fill --title "feat: tree [path] [depth] 子树查询 + autoload 兄弟可见（fix #150）"
```

> gh 需绕本地代理（`unset *_proxy` + 禁沙箱）；push 失败即切 HTTPS + gh 凭据。开 PR 后用 `gh pr merge --auto`（main required check = `ci-ok` 聚合 job，真等绿），挂完用 `autoMergeRequest` 非 null 自检。

---

## Self-Review（spec 覆盖核对）

- **决策 A（启发式位置参数）** → Task 4（`_register_tree_args` 双位置 + `_preflight_tree` 消歧 + 矩阵测试）。✅
- **决策 B（默认根维持 current_scene）** → Task 1 Step 3 的 `else` 分支 + `test_handle_get_scene_tree_without_path_keeps_current_scene_root` 回归。✅
- **CLI 参数** → Task 4；**client** → Task 2；**bridge** → Task 3；**服务端根解析** → Task 1。✅
- **错误码/退出码（复用 1001/-1003/1005，不撞码）** → Task 1（1001）、Task 4（-1003/64）；1005 不动。✅
- **文档同步（SKILL.md 模板 4 处 + addon README + 重渲染）** → Task 5 全部覆盖。✅
- **测试矩阵（Python preflight + client + GUT subtree/bad-path/regression + skills_install）** → Task 1/2/3/4 各自的 test step + Task 5 Step 7 + Task 6 Step 2。✅
- 类型/签名一致性：`get_scene_tree(depth, max_nodes=None, path=None)` 在 client/bridge/stub/FakeClient 四处签名一致；`ns._tree_path`/`ns._tree_depth` 在 `_preflight_tree`（写）与 `cmd_tree`（读）一致；`tree_arg1`/`tree_arg2` dest 在注册与 preflight 一致。✅
- 无占位符 / TODO。✅
