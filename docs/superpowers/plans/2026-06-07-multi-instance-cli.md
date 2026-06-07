# 同项目多实例 CLI 控制（命名实例）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同一 Godot 项目可同时跑多个命名 daemon 实例（server / client1 / …），CLI 每条命令可精确选靶。

**Architecture:** 状态文件整体迁入 `.cli_control/instances/<name>/`；实例解析（0/1/N 语义）收敛在 `daemon.py` 单入口，CLI RPC / GameClient / GameBridge 三方共用；注册表文件名变 `<hash>-<instance>.json` 双格式兼容读。GDScript 侧零改动。

**Tech Stack:** Python ≥3.10、argparse、pytest（coverage run -m pytest）、现有 websockets 栈不动。

**Spec:** `docs/superpowers/specs/2026-06-07-multi-instance-cli-design.md`（已两轮 review 通过，实现遇到与 spec 冲突时以 spec 为准并回报）

**全局约束（来自 CLAUDE.md，执行者必读）：**
- 测试一律委托 subagent 跑（`model: "sonnet"`，禁 `run_in_background`），主会话只收精简结论。单条测试命令写在各 Step 里，由 subagent 原样执行。
- 全量套件用 `coverage run -m pytest python/tests -x -q`（**不要** `pytest --cov`，原因见 pyproject.toml 注释）。e2e 需要 `GODOT_BIN`（本机 godot 在 `~/.local/bin`）。
- JSON 信封 / 错误码三段制 / 退出码语义是硬契约（CLAUDE.md 契约 1–3），所有新错误路径必须落信封。
- 不新增错误码：歧义/非法名都是 `-1003`（`CLIENT_CODE_USAGE`）+ exit 64（`EXIT_USAGE`），常量已在 `cli.py` 定义。

---

## File Structure（改动地图）

| 文件 | 职责 / 改动 |
|---|---|
| `python/godot_cli_control/daemon.py` | `Daemon` 加 `instance` 参数 + 新布局；新增 `validate_instance_name` / `list_live_instances` / `InstanceAmbiguityError`；`discover_port` 加 `instance` 参数与解析语义 |
| `python/godot_cli_control/registry.py` | `DaemonRecord.instance` 字段、`<hash>-<instance>.json` 文件名、legacy 兼容读、`_prune` 双格式 |
| `python/godot_cli_control/cli.py` | `_add_instance_name_flag` 挂 5 个 subparser；顶层 `--instance`（与 `--port` 互斥）；`cmd_daemon_*` / `cmd_run` / RPC main 路径选靶 |
| `python/godot_cli_control/client.py` | `GameClient(instance=...)` |
| `python/godot_cli_control/bridge.py` | `GameBridge(instance=...)` |
| `python/godot_cli_control/templates/skill/SKILL.md` | daemon 表、多实例小节、pitfalls |
| `addons/godot_cli_control/README.md` | 命令表对齐 |
| `python/tests/test_daemon.py` / `test_registry.py` / `test_cli.py` / `test_client.py` / `test_bridge.py` | 单测 |
| `python/tests/test_e2e_multi_instance.py` | 新建 e2e |
| `CHANGELOG.md` | `[Unreleased]` |

不动：`pytest_plugin.py`（默认实例语义自动兼容）、GDScript 全部、`init`。

---

### Task 0: 建分支

- [ ] **Step 0.1**: `git checkout -b feat/multi-instance-cli main`

---

### Task 1: Daemon 实例参数 + 状态布局迁移

**Files:**
- Modify: `python/godot_cli_control/daemon.py`（`Daemon.__init__` 约 :33-45；`read_pid` :49；`current_port` :61；`_cleanup_state_files` :267）
- Test: `python/tests/test_daemon.py`

先读 `python/tests/test_daemon.py` 开头 80 行了解既有 fixture 习惯（tmp project、monkeypatch 模式），新测试照抄风格。

- [ ] **Step 1.1: 写失败测试**（追加到 `test_daemon.py`）

```python
class TestInstanceLayout:
    def test_default_instance_uses_instances_dir(self, tmp_path: Path) -> None:
        d = daemon_mod.Daemon(tmp_path)
        assert d.control_dir == tmp_path / ".cli_control" / "instances" / "default"
        assert d.pid_file == d.control_dir / "godot.pid"
        assert d.port_file == d.control_dir / "port"

    def test_named_instance_dir(self, tmp_path: Path) -> None:
        d = daemon_mod.Daemon(tmp_path, instance="server")
        assert d.control_dir == tmp_path / ".cli_control" / "instances" / "server"

    def test_explicit_control_dir_wins(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom"
        d = daemon_mod.Daemon(tmp_path, custom, instance="server")
        assert d.control_dir == custom

    @pytest.mark.parametrize("bad", ["", "a/b", "a b", "x" * 33, "中文", "a.b"])
    def test_invalid_instance_name_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(daemon_mod.DaemonError, match="instance"):
            daemon_mod.Daemon(tmp_path, instance=bad)

    def test_default_read_pid_falls_back_to_legacy(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".cli_control"
        legacy.mkdir()
        (legacy / "godot.pid").write_text(str(os.getpid()))
        (legacy / "port").write_text("12345")
        d = daemon_mod.Daemon(tmp_path)  # default 实例
        assert d.read_pid() == os.getpid()      # legacy fallback
        assert d.current_port() == 12345
        assert d.is_running()                   # 防双开：legacy 存活 = running

    def test_named_instance_no_legacy_fallback(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".cli_control"
        legacy.mkdir()
        (legacy / "godot.pid").write_text(str(os.getpid()))
        d = daemon_mod.Daemon(tmp_path, instance="server")
        assert d.read_pid() is None             # 命名实例不读 legacy
```

注意 import 习惯对齐文件头（`from godot_cli_control import daemon as daemon_mod` 或既有写法）。

- [ ] **Step 1.2: 跑测试确认失败**

Run（subagent）: `cd /Users/kesar/Projects/godot-cli-control && .venv/bin/python -m pytest python/tests/test_daemon.py::TestInstanceLayout -x -q`
Expected: FAIL（`control_dir` 仍是平铺路径 / 无 instance 参数 TypeError）

- [ ] **Step 1.3: 实现**

`daemon.py` 顶部加：

```python
import re

_INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def validate_instance_name(name: str) -> str:
    """实例名要落进文件路径与注册表文件名，必须文件系统安全。"""
    if not _INSTANCE_NAME_RE.fullmatch(name):
        raise DaemonError(
            f"非法 instance 名 {name!r}：只允许 [A-Za-z0-9_-]，长度 1-32"
        )
    return name
```

`Daemon.__init__` 改为（保留全部既有注释，新增部分带注释说明 #spec 2026-06-07）：

```python
def __init__(
    self,
    project_root: Path,
    control_dir: Path | None = None,
    *,
    instance: str = "default",
) -> None:
    self.project_root = Path(project_root).resolve()
    self.instance = validate_instance_name(instance)
    # 多实例：状态文件按实例隔离到 instances/<name>/（spec 2026-06-07）。
    # 显式 control_dir 仍全权 override（既有测试注入语义不变）。
    self.control_dir = control_dir or (
        self.project_root / ".cli_control" / "instances" / self.instance
    )
    ...  # 五个状态文件字段不变，仍相对 self.control_dir
    # legacy 平铺路径（迁移前布局）：default 实例只读 fallback + 防双开探活
    self._legacy_dir = self.project_root / ".cli_control"
```

`read_pid` / `current_port` 加 legacy fallback（只对 default 实例、只读不写）：

```python
def read_pid(self) -> int | None:
    pid = self._read_int(self.pid_file)
    if pid is None and self.instance == "default":
        pid = self._read_int(self._legacy_dir / "godot.pid")  # 防双开（spec）
    return pid

def current_port(self) -> int | None:
    port = self._read_int(self.port_file)
    if port is None and self.instance == "default":
        port = self._read_int(self._legacy_dir / "port")
    return port

@staticmethod
def _read_int(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None
```

`_cleanup_state_files` 对 default 实例同时 unlink legacy 两文件（kill 掉 legacy daemon 后别留尸体）：

```python
self.pid_file.unlink(missing_ok=True)
self.port_file.unlink(missing_ok=True)
if self.instance == "default":
    (self._legacy_dir / "godot.pid").unlink(missing_ok=True)
    (self._legacy_dir / "port").unlink(missing_ok=True)
_registry.unregister(self.project_root)   # Task 2 再加 instance 参数
```

注意 `read_godot_bin_pref`（:280-283）读 `.cli_control/godot_bin`——这是 init 写的**项目级**偏好，必须改为从 `self._legacy_dir / "godot_bin"` 读（不能跟着 control_dir 搬进 instances/，否则命名实例读不到 init 写的 bin 偏好）。

- [ ] **Step 1.4: 跑测试确认通过 + 既有 test_daemon 不回归**

Run（subagent）: `.venv/bin/python -m pytest python/tests/test_daemon.py -x -q`
Expected: 全 PASS（若既有测试依赖平铺路径布局而挂，修测试以适配新布局——布局变更是 spec 故意为之）

- [ ] **Step 1.5: Commit** `git add -A && git commit -m "feat(daemon): Daemon 实例参数 + 状态迁入 instances/<name>/，default 只读 fallback legacy"`

---

### Task 2: 注册表 instance 字段 + 双格式兼容

**Files:**
- Modify: `python/godot_cli_control/registry.py`（`DaemonRecord` :34-48、`register` :55-77、`unregister` :80-82、`list_all` :85-101、`_prune` :104-108）
- Modify: `python/godot_cli_control/daemon.py`（`start()` :225 的 register 调用、`_cleanup_state_files` 的 unregister 调用）
- Test: `python/tests/test_registry.py`（既有 `reg_dir` / `dead_pid` fixture 直接复用）

- [ ] **Step 2.1: 写失败测试**

```python
def test_register_with_instance_filename(reg_dir: Path, tmp_path: Path) -> None:
    proj = tmp_path / "p1"
    proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x",
                      log_path="x", instance="server")
    files = list(reg_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == f"{registry.project_hash(proj)}-server.json"
    r = registry.list_all()[0]
    assert r.instance == "server"


def test_same_project_two_instances_coexist(reg_dir: Path, tmp_path: Path) -> None:
    proj = tmp_path / "p1"
    proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x",
                      log_path="x", instance="server")
    registry.register(proj, pid=os.getpid(), port=2, godot_bin="x",
                      log_path="x", instance="client1")
    assert {r.instance for r in registry.list_all()} == {"server", "client1"}


def test_legacy_record_read_as_default(reg_dir: Path, tmp_path: Path) -> None:
    proj = tmp_path / "p1"
    proj.mkdir()
    reg_dir.mkdir(parents=True)
    legacy = reg_dir / f"{registry.project_hash(proj)}.json"
    legacy.write_text(json.dumps({
        "project_root": str(proj.resolve()), "pid": os.getpid(), "port": 7,
        "started_at": "2026-01-01T00:00:00+00:00", "godot_bin": "x", "log_path": "x",
    }))  # 旧格式：无 instance 字段
    r = registry.list_all()[0]
    assert r.instance == "default"


def test_unregister_targets_instance(reg_dir: Path, tmp_path: Path) -> None:
    proj = tmp_path / "p1"
    proj.mkdir()
    registry.register(proj, pid=os.getpid(), port=1, godot_bin="x",
                      log_path="x", instance="server")
    registry.unregister(proj, instance="client1")  # 不存在的实例 → no-op
    assert len(registry.list_all()) == 1
    registry.unregister(proj, instance="server")
    assert registry.list_all() == []


def test_prune_dead_new_format_cleans_instance_dir(
    reg_dir: Path, tmp_path: Path, dead_pid: int
) -> None:
    proj = tmp_path / "p1"
    proj.mkdir()
    inst_dir = proj / ".cli_control" / "instances" / "server"
    inst_dir.mkdir(parents=True)
    (inst_dir / "godot.pid").write_text(str(dead_pid))
    (inst_dir / "port").write_text("1")
    registry.register(proj, pid=dead_pid, port=1, godot_bin="x",
                      log_path="x", instance="server")
    assert registry.list_all() == []
    assert not (inst_dir / "godot.pid").exists()
    assert not (inst_dir / "port").exists()
```

既有 `test_list_all_also_cleans_project_state_for_dead`（legacy 平铺路径清理）保持通过——legacy 记录的 `_prune` 分支不能删。

- [ ] **Step 2.2: 确认失败** Run（subagent）: `.venv/bin/python -m pytest python/tests/test_registry.py -x -q` → 新增项 FAIL

- [ ] **Step 2.3: 实现**

```python
@dataclass(frozen=True)
class DaemonRecord:
    project_root: str
    pid: int
    port: int
    started_at: str
    godot_bin: str
    log_path: str
    instance: str = "default"   # 旧记录无此字段 → default（spec 2026-06-07）

    @property
    def record_file(self) -> Path:
        return _REGISTRY_DIR / f"{project_hash(Path(self.project_root))}-{self.instance}.json"
```

- `register(..., instance: str = "default")`：payload 加 `"instance"`，目标文件名 `f"{project_hash(project_root)}-{instance}.json"`。
- `unregister(project_root, instance: str = "default")`：unlink 新文件名；`instance == "default"` 时**连同** legacy `f"{hash}.json"` 一并 unlink（停掉 legacy daemon 后记录别残留）。
- `list_all()` 构造改为容缺：`r = DaemonRecord(**{k: data[k] for k in data if k in DaemonRecord.__dataclass_fields__})`——注意改成「按 data 现有键过滤」而非按 fields 取（旧记录缺 instance 键时 KeyError 会误删活记录）。必填六键缺失仍走 except 清理。
- `_prune()`：新格式按 `record.instance` 清 `instances/<inst>/{godot.pid,port}`；`instance == "default"` 同时清 legacy 平铺两文件（覆盖旧格式记录）。
- `daemon.py start()` 的 `_registry.register(...)` 加 `instance=self.instance`；`_cleanup_state_files` 的 unregister 加 `instance=self.instance`。

- [ ] **Step 2.4: 确认通过** Run（subagent）: `.venv/bin/python -m pytest python/tests/test_registry.py python/tests/test_daemon.py -x -q` → 全 PASS

- [ ] **Step 2.5: Commit** `git commit -am "feat(registry): DaemonRecord.instance + <hash>-<instance>.json，legacy 兼容读与双格式 prune"`

---

### Task 3: 实例解析单入口（discover_port / list_live_instances）

**Files:**
- Modify: `python/godot_cli_control/daemon.py`（`discover_port` :399-412 附近）
- Test: `python/tests/test_daemon.py`

- [ ] **Step 3.1: 写失败测试**

```python
class TestInstanceResolution:
    def _mk_live(self, proj: Path, name: str, port: int) -> None:
        d = proj / ".cli_control" / "instances" / name
        d.mkdir(parents=True)
        (d / "godot.pid").write_text(str(os.getpid()))  # 本进程 = 必活
        (d / "port").write_text(str(port))

    def test_zero_running_falls_back_legacy_then_none(self, tmp_path: Path) -> None:
        assert daemon_mod.discover_port(tmp_path) is None
        legacy = tmp_path / ".cli_control"
        legacy.mkdir()
        (legacy / "port").write_text("9999")
        assert daemon_mod.discover_port(tmp_path) == 9999  # legacy 只读 fallback

    def test_single_running_auto_selected(self, tmp_path: Path) -> None:
        self._mk_live(tmp_path, "server", 7001)
        assert daemon_mod.discover_port(tmp_path) == 7001  # 不管名字，唯一即选中

    def test_multiple_running_raises_ambiguity(self, tmp_path: Path) -> None:
        self._mk_live(tmp_path, "server", 7001)
        self._mk_live(tmp_path, "client1", 7002)
        with pytest.raises(daemon_mod.InstanceAmbiguityError) as ei:
            daemon_mod.discover_port(tmp_path)
        # message 必须列出全部实例名（agent 靠它知道下一步传什么）
        assert "client1" in str(ei.value) and "server" in str(ei.value)

    def test_explicit_instance_reads_only_that_dir(self, tmp_path: Path) -> None:
        self._mk_live(tmp_path, "server", 7001)
        self._mk_live(tmp_path, "client1", 7002)
        assert daemon_mod.discover_port(tmp_path, instance="client1") == 7002

    def test_explicit_instance_not_running_raises(self, tmp_path: Path) -> None:
        self._mk_live(tmp_path, "server", 7001)
        with pytest.raises(daemon_mod.InstanceAmbiguityError, match="server"):
            daemon_mod.discover_port(tmp_path, instance="nope")

    def test_dead_pid_instance_ignored(self, tmp_path: Path, dead_pid: int) -> None:
        self._mk_live(tmp_path, "server", 7001)
        d = tmp_path / ".cli_control" / "instances" / "ghost"
        d.mkdir(parents=True)
        (d / "godot.pid").write_text(str(dead_pid))
        (d / "port").write_text("7099")
        assert daemon_mod.discover_port(tmp_path) == 7001  # 死实例不算「在跑」
```

（`dead_pid` fixture 从 test_registry.py 抄到 test_daemon.py 或挪进 conftest 共享——选挪 conftest，DRY。）

- [ ] **Step 3.2: 确认失败**（subagent，同前命令）

- [ ] **Step 3.3: 实现**（`daemon.py`，替换现 `discover_port`，保留原 docstring 精神并扩写多实例语义）

```python
class InstanceAmbiguityError(RuntimeError):
    """实例无法唯一确定（≥2 在跑且未显式指定，或显式指定的不在跑）。

    CLI 把它映射成 -1003 + exit 64 的 preflight 用法错；message 自带在跑
    实例清单，agent 看单行 JSON 即知下一步传什么（spec 2026-06-07）。
    """

    def __init__(self, message: str, names: list[str]) -> None:
        super().__init__(message)
        self.names = names


def list_live_instances(project_root: Path | None = None) -> list[str]:
    """扫 <project_root>/.cli_control/instances/*/，按 pid 探活返回在跑实例名（有序）。"""
    root = Path(project_root) if project_root is not None else Path.cwd()
    base = root / ".cli_control" / "instances"
    if not base.is_dir():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and Daemon(root, instance=p.name).is_running()
    )


def discover_port(
    project_root: Path | None = None, instance: str | None = None
) -> int | None:
    root = Path(project_root) if project_root is not None else Path.cwd()
    if instance is not None:
        d = Daemon(root, instance=instance)
        if not d.is_running():
            live = list_live_instances(root)
            raise InstanceAmbiguityError(
                f"instance {instance!r} not running"
                + (f"; running: {', '.join(live)}" if live else "; none running"),
                live,
            )
        return d.current_port()
    live = list_live_instances(root)
    if len(live) > 1:
        raise InstanceAmbiguityError(
            f"multiple instances running: {', '.join(live)} — pass --instance <name>",
            live,
        )
    if len(live) == 1:
        return Daemon(root, instance=live[0]).current_port()
    # 0 个在跑：default 实例的 current_port 自带 legacy 只读 fallback（Task 1）
    return Daemon(root).current_port()
```

注意坑：`list_live_instances` 里 default 实例的 `is_running()` 带 legacy fallback（Task 1），意味着「legacy daemon 在跑 + instances/default/ 目录存在但空」时 default 会被算成 live——这是**符合预期**的（legacy daemon 应算 default 在跑），给它写个测试钉住：legacy pid 活 + `instances/default/` 不存在时 `list_live_instances` 返回 `[]` 但 `discover_port` 仍经 0-命中分支拿到 legacy port。

- [ ] **Step 3.4: 确认通过**（subagent）：`.venv/bin/python -m pytest python/tests/test_daemon.py -x -q`

- [ ] **Step 3.5: Commit** `git commit -am "feat(daemon): 实例解析单入口 discover_port(instance=)/list_live_instances + 歧义异常"`

---

### Task 4: GameClient / GameBridge 加 instance 参数

**Files:**
- Modify: `python/godot_cli_control/client.py:79-90`、`python/godot_cli_control/bridge.py:24-32`
- Test: `python/tests/test_client.py`、`python/tests/test_bridge.py`

- [ ] **Step 4.1: 写失败测试**（test_client.py；先读该文件开头看 mock 习惯）

```python
def test_client_instance_param_resolves_port(tmp_path, monkeypatch):
    inst = tmp_path / ".cli_control" / "instances" / "server"
    inst.mkdir(parents=True)
    (inst / "godot.pid").write_text(str(os.getpid()))
    (inst / "port").write_text("7042")
    monkeypatch.chdir(tmp_path)
    c = GameClient(instance="server")
    assert c._port == 7042


def test_client_port_takes_precedence_over_instance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = GameClient(port=1234, instance="server")  # 显式 port 全权
    assert c._port == 1234
```

test_bridge.py 加一条同形测试验证 `GameBridge.__init__` 透传 `instance`（mock 掉 connect，看既有测试怎么 mock）。

- [ ] **Step 4.2: 确认失败**（subagent）

- [ ] **Step 4.3: 实现**

`client.py`：

```python
def __init__(self, port: int | None = None, instance: str | None = None) -> None:
    # port=None → auto-discover；instance 透传 discover_port 选定命名实例
    # （spec 2026-06-07）。显式 port 优先，instance 仅在 port=None 时生效。
    if port is None:
        from .daemon import discover_port

        port = discover_port(instance=instance) or DEFAULT_PORT
```

`bridge.py`：`def __init__(self, port: int | None = None, instance: str | None = None)`，`GameClient(port=port, instance=instance)`。

注意：`instance` 指定但不在跑时 `InstanceAmbiguityError` 直接抛给库用户（run 脚本场景），不吞。

- [ ] **Step 4.4: 确认通过**（subagent）：`.venv/bin/python -m pytest python/tests/test_client.py python/tests/test_bridge.py -x -q`

- [ ] **Step 4.5: Commit** `git commit -am "feat(client,bridge): instance 参数接入实例解析单入口"`

---

### Task 5: CLI —— `--name` helper、daemon 子命令选靶、stop 矩阵、ls 列

**Files:**
- Modify: `python/godot_cli_control/cli.py`：
  - 新增 `_add_instance_name_flag()`（放 `_add_daemon_flags` :1928 旁）
  - `build_parser()`：挂到 `start_p`(:2104) / `stop_p`(:2118) / `status_p`(:2137) / `logs_p`(:2149) / `run_p`(:2180)；**拆掉** `stop_p` 的 `--all`/`--project` 互斥组(:2126 附近)
  - `cmd_daemon_start` :1264 / `cmd_daemon_stop` :1301 / `cmd_daemon_status` :1364 / `cmd_daemon_logs` :1416 / `cmd_daemon_ls` :1457
- Test: `python/tests/test_cli.py`（先读开头看 CliRunner/capsys 习惯，照抄既有 daemon 命令测试的 mock 方式）

- [ ] **Step 5.1: 写失败测试**（关键断言，具体 harness 照 test_cli.py 既有 daemon 测试改）

```python
# 1) --name 注册面：5 个子命令接受 --name，且 status/logs/stop 不长出 start 专属 flag
def test_daemon_subcommands_accept_name():
    parser = cli.build_parser()
    for argv in (["daemon", "status", "--name", "server"],
                 ["daemon", "logs", "--name", "server"],
                 ["daemon", "stop", "--name", "server"]):
        ns = parser.parse_args(argv)
        assert ns.name == "server"
    with pytest.raises(SystemExit):  # status 不该有 --record
        parser.parse_args(["daemon", "status", "--record"])

# 2) 非法实例名：argparse type 校验 → exit 64 + -1003 信封（用既有 envelope 断言 helper）
def test_invalid_instance_name_usage_error(...): ...
    # main(["daemon", "start", "--name", "a/b"]) → exit 64, envelope code -1003

# 3) stop 选靶矩阵
def test_stop_all_with_name_is_usage_error(...): ...      # --all --name → 64/-1003
def test_stop_ambiguous_without_name(...): ...            # cwd 两实例活 → 64/-1003，message 含两名字
def test_stop_all_threads_instance(...):
    # 注册表两条不同 instance 记录 + monkeypatch Daemon.stop 收集调用，
    # 断言 Daemon(...) 以 instance=r.instance 构造（防 spec 指出的「--all 永远打 default」回归）

# 4) ls 含 instance
def test_ls_json_includes_instance(...): ...   # result.daemons[i].instance == "server"
def test_ls_text_has_instance_column(...): ... # 行格式 pid\tport\tinstance\tproject_root\tstarted_at

# 5) daemon start envelope 带 instance
def test_daemon_start_json_includes_instance(...): ...  # {"started": true, "instance": "server", ...}
```

- [ ] **Step 5.2: 确认失败**（subagent）：`.venv/bin/python -m pytest python/tests/test_cli.py -x -q -k "instance or stop_all or daemon_start_json"`（注意 -k 表达式整体引号）

- [ ] **Step 5.3: 实现**

(a) helper + argparse type 校验（type 校验失败 argparse 自动走 -1003/64 信封，与 #82/#111 一致）：

```python
def _instance_name_arg(value: str) -> str:
    from .daemon import DaemonError, validate_instance_name

    try:
        return validate_instance_name(value)
    except DaemonError as e:
        raise argparse.ArgumentTypeError(str(e))


def _add_instance_name_flag(p: argparse.ArgumentParser) -> None:
    """daemon 子命令的实例选择 flag。独立于 _add_daemon_flags：后者全是
    start 专属 flag（--record/--headless/...），挂到 status/logs/stop 会污染。"""
    p.add_argument(
        "--name",
        type=_instance_name_arg,
        default=None,
        help="实例名（默认 default；多实例并行时用于选靶，等价顶层 --instance）",
    )
```

挂到 `start_p` / `run_p` / `status_p` / `logs_p` / `stop_p` 五处。

(b) 共享选靶 helper（cmd_daemon_status / logs / stop / run 复用，DRY）：

```python
def _resolve_daemon_instance(ns: argparse.Namespace, project_root: Path) -> str | None:
    """返回实例名；歧义时 emit -1003 信封并返回 None（调用方 return EXIT_USAGE）。"""
    inst = getattr(ns, "name", None)
    if inst is not None:
        return inst
    from .daemon import list_live_instances

    live = list_live_instances(project_root)
    if len(live) > 1:
        _emit_top_error(
            ns, code=CLIENT_CODE_USAGE,
            message=f"multiple instances running: {', '.join(live)} — pass --name <instance>",
        )
        return None
    return live[0] if live else "default"   # 0 个在跑 → default（保 legacy fallback 与 stopped 语义）
```

(c) `cmd_daemon_start`：`daemon = Daemon(Path.cwd(), instance=ns.name or "default")`；成功 envelope `result` 加 `"instance": daemon.instance`。
(d) `cmd_daemon_status` / `cmd_daemon_logs`：开头 `inst = _resolve_daemon_instance(ns, Path.cwd())`，None → `return EXIT_USAGE`；`Daemon(Path.cwd(), instance=inst)`。status 的 running/stopped envelope 加 `"instance"` 字段。
(e) `cmd_daemon_stop` 矩阵（拆掉 argparse 互斥组后顶部显式校验）：

```python
if getattr(ns, "all", False) and getattr(ns, "name", None):
    _emit_top_error(ns, code=CLIENT_CODE_USAGE, message="--all 与 --name 互斥")
    return EXIT_USAGE
if getattr(ns, "all", False):
    if getattr(ns, "project", None):
        # 本项目全部实例：以 project_root 为根扫 live，不查注册表（spec §3）
        target = ns.project.resolve()
        from .daemon import list_live_instances
        names = list_live_instances(target) or ["default"]
        # 逐实例 stop，沿用全局 --all 的 partial-failure 汇总语义（EXIT_PARTIAL）
        ...
    else:
        ...  # 既有注册表循环，唯一改动：Daemon(Path(r.project_root), instance=r.instance).stop()
    ...
# 单停：target = project or cwd；inst = _resolve_daemon_instance(ns, target)
```

`--all --project` 分支的逐实例循环直接仿照既有 `--all` 循环体（cli.py:1317-1348）：entry dict 加 `"instance"`、catch `DaemonError` 聚合 partial failure、**JSON 与 text 两条路径都要**（含 text 模式 `summary: N/M stopped` 行），汇总 rc 语义一致：全成功 0、有 DaemonError EXIT_PARTIAL(3)。单停 envelope `{"stopped": true, "rc": rc, "project_root": ..., "instance": inst}`。
(f) `cmd_daemon_ls`：payload 每条加 `"instance": r.instance`；text 行变 `f"{r.pid}\t{r.port}\t{r.instance}\t{r.project_root}\t{r.started_at}"`。

- [ ] **Step 5.4: 确认通过 + cli 套件无回归**（subagent）：`.venv/bin/python -m pytest python/tests/test_cli.py python/tests/test_cli_helpers.py -x -q`

- [ ] **Step 5.5: Commit** `git commit -am "feat(cli): daemon 子命令 --name 选靶 + stop 矩阵 + ls instance 列"`

---

### Task 6: CLI —— 顶层 `--instance`（RPC 路径）+ run 选靶

**Files:**
- Modify: `python/godot_cli_control/cli.py`：`build_parser()` 顶层 `--port`(:2077-2086) 改互斥组；RPC main 路径(:2455-2480)；`cmd_run`(:1494-1562)
- Test: `python/tests/test_cli.py`

- [ ] **Step 6.1: 写失败测试**

```python
def test_top_level_instance_and_port_mutually_exclusive(...):
    # main(["--instance", "x", "--port", "1", "tree"]) → exit 64（argparse 互斥）

def test_rpc_ambiguity_is_preflight(...):
    # cwd 两实例活、无 --instance → exit 64 + -1003 信封，message 列两名字
    # 且不发生任何网络连接（monkeypatch GameClient.connect 断言未被调用）

def test_rpc_explicit_instance_not_running_preflight(...):
    # --instance nope 而只有 server 活 → 64/-1003，message 含 "server"

def test_rpc_single_instance_auto_selected(...):
    # 唯一实例活 → _run_rpc 收到该实例端口（monkeypatch _run_rpc 捕获 port 参数）
```

- [ ] **Step 6.2: 确认失败**（subagent）

- [ ] **Step 6.3: 实现**

(a) `build_parser()`：

```python
conn_grp = parser.add_mutually_exclusive_group()
conn_grp.add_argument("--port", ...)        # 原 help 不动，整段挪进组
conn_grp.add_argument(
    "--instance",
    type=_instance_name_arg,
    default=None,
    help="RPC 连接的目标实例名（多实例并行时必传；与 --port 互斥）",
)
```

(b) RPC main 路径（:2472-2477 替换）：

```python
port = ns.port
if port is None:
    from .daemon import InstanceAmbiguityError, discover_port

    try:
        port = discover_port(instance=ns.instance) or DEFAULT_PORT
    except InstanceAmbiguityError as e:
        # preflight：本地 FS 即可判定，绝不让 agent 等 30s 连接重试（契约 #5）
        if fmt == OUTPUT_JSON:
            _emit_error_payload(CLIENT_CODE_USAGE, str(e))
        else:
            print(f"错误：{e}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
```

(c) `cmd_run`：`daemon = Daemon(Path.cwd())`(:1534) 前插选靶（复用 `_resolve_daemon_instance`，None → `return EXIT_USAGE`）；`Daemon(Path.cwd(), instance=inst)`。0 个在跑 → helper 返回 "default" → 走既有 auto-start 分支，语义自然成立（spec：0 → 启 default、跑完停）。

- [ ] **Step 6.4: 确认通过**（subagent）：`.venv/bin/python -m pytest python/tests/test_cli.py -x -q`

- [ ] **Step 6.5: Commit** `git commit -am "feat(cli): 顶层 --instance 选靶（与 --port 互斥）+ run 实例解析"`

---

### Task 7: 文档同步（SKILL.md 模板 / addon README / 渲染 / CHANGELOG）

**Files:**
- Modify: `python/godot_cli_control/templates/skill/SKILL.md`
- Modify: `addons/godot_cli_control/README.md`
- Modify: `CHANGELOG.md`
- Regenerate: `.claude/skills/godot-cli-control/SKILL.md`（仓内渲染版）

- [ ] **Step 7.1: 改 SKILL.md 模板**
  - daemon 命令表：start/stop/status/logs 加 `--name`，`stop --all --project <path>` 组合，ls 列说明加 instance
  - 新增「## Multi-instance（同项目多实例）」小节：场景示例（server + client1 启动、`--instance` 选靶 RPC、歧义报错样例 JSON）
  - common pitfalls 加一条：「多实例在跑时不传 `--instance`/`--name` → exit 64 / code -1003，message 列出在跑实例名；照着传即可」
  - 顶层 flag 说明：`--instance` 与 `--port` 互斥
- [ ] **Step 7.2: addon README 命令表 / 错误码表对齐**（错误码无新增，只补充 -1003 的多实例触发场景一句）
- [ ] **Step 7.3: CHANGELOG `[Unreleased]` 记**：同项目多实例（命名实例）、状态布局迁移 `.cli_control/instances/<name>/`（legacy 只读 fallback 平滑升级）、`daemon ls` 新列、顶层 `--instance`
- [ ] **Step 7.4: 验证渲染**（subagent）：
  - `.venv/bin/python -c "from godot_cli_control import cli; print(cli.format_full_help())" > /dev/null`（渲染没崩）
  - 重渲仓内副本：`COLUMNS=80` + **Python 3.12** 跑 `skills_install.render_skill`（CI skill-render-drift 以 3.12 为准；若 .venv 不是 3.12，先确认 `python3.12` 可用，用它建一次性 venv 或直接 `python3.12 -m`）
  - `.venv/bin/python -m pytest python/tests/test_skills_install.py -x -q` → PASS
- [ ] **Step 7.5: Commit** `git commit -am "docs(skill): 多实例小节 + daemon 表对齐 + CHANGELOG"`

---

### Task 8: e2e —— 真 Godot 双实例

**Files:**
- Create: `python/tests/test_e2e_multi_instance.py`

先读 `python/tests/test_e2e_example.py` 全文，照抄其 GODOT_BIN gating / 项目 fixture / 清理习惯。**不要**用 session 级 `godot_daemon` fixture（它是单实例语义），本文件自管生命周期。

- [ ] **Step 8.1: 写 e2e 测试**（骨架——`example_project` / `closing_bridge` 等 fixture/helper 名是**示意**，以 test_e2e_example.py 实际存在的为准改写）

```python
def test_two_instances_isolated(example_project, ...):
    a = Daemon(example_project, instance="server")
    b = Daemon(example_project, instance="client1")
    try:
        a.start(headless=True)
        b.start(headless=True)
        assert a.current_port() != b.current_port()
        # 各连各的，RPC 不串台
        with closing_bridge(GameBridge(instance="server")) as ba: ...
        # 歧义：discover_port 无 instance → InstanceAmbiguityError
        with pytest.raises(InstanceAmbiguityError):
            discover_port(example_project)
        # registry 两条记录
        assert {r.instance for r in registry.list_all()
                if r.project_root == str(example_project)} == {"server", "client1"}
    finally:
        a.stop(); b.stop()
```

加 CLI 面 e2e（subprocess 跑 `godot-cli-control --instance server tree` 等，按既有 e2e 是否有 subprocess 先例决定，没有就只测库面，CLI 面已被 Task 5/6 单测覆盖）。

- [ ] **Step 8.2: 本机真跑**（subagent，**禁 run_in_background**，带 `GODOT_BIN=~/.local/bin/godot` 或既有发现机制）：`.venv/bin/python -m pytest python/tests/test_e2e_multi_instance.py -x -q`
Expected: PASS。挂了按 superpowers:systematic-debugging 排查，不许 skip。

- [ ] **Step 8.3: Commit** `git commit -am "test(e2e): 同项目双实例隔离 + 歧义解析真机回归"`

---

### Task 9: 收尾 —— 全量回归、覆盖率、PR、遗留 issue

- [ ] **Step 9.1: 全量套件 + 覆盖率门槛**（subagent，model sonnet，禁后台）：
  `cd /Users/kesar/Projects/godot-cli-control && .venv/bin/coverage run -m pytest python/tests -x -q && .venv/bin/coverage report`
  Expected: 全 PASS，覆盖率 ≥80%（fail_under 门禁）。
- [ ] **Step 9.2: 推分支开 PR**（base main；CI 锚定 ci-ok，`gh pr merge --auto` 后用 `autoMergeRequest` 非 null 自检；gh 需 unset *_proxy + 禁沙箱）
- [ ] **Step 9.3: 开遗留 issue**（CLAUDE.md「实施收尾必开 Issue」）：
  1. pytest plugin 多实例 fixture（`godot_instances` 工厂；现状：`godot_daemon` session 级单实例，位置 `python/godot_cli_control/pytest_plugin.py`；影响：联机 e2e 无法在单测里抽双 bridge；方案：参考本 spec 解析单入口；优先级 P2）
  2. 一条命令广播多实例（编排层；P3）
  3. 实施中新发现的越界问题（如有）
- [ ] **Step 9.4: 把 issue 链接贴给用户**

---

## 验证总览

| 层 | 怎么验 |
|---|---|
| 单测 | Task 1–6 各自的 pytest 文件，TDD 红→绿 |
| e2e | Task 8 真 Godot 双实例（本机必真跑，不许拿缺 GODOT_BIN 当借口） |
| 文档 | `format_full_help()` 渲染不崩 + test_skills_install + CI skill-render-drift |
| 契约 | 歧义/非法名信封 `{"ok": false, "error": {"code": -1003, ...}}` + exit 64；`daemon ls`/`stop --all` 退出码语义不变 |
| 兼容 | legacy port/pid fallback 测试（Task 1/3）+ 双格式注册表（Task 2）+ 防双开（Task 1） |
