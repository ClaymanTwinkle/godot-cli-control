"""Global cross-project daemon registry under ~/.local/state/godot-cli-control/daemons/.

每个守护进程一份 ``<project_hash>.json``，记录 PID / 端口 / 项目路径 / 启动时刻。
``list_all()`` 顺手探活并清理死记录。无文件锁 —— 每条记录文件名以 project_hash
区分，写入幂等。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

_REGISTRY_DIR = Path.home() / ".local" / "state" / "godot-cli-control" / "daemons"


@dataclass(frozen=True)
class DaemonRecord:
    project_root: str
    pid: int
    port: int
    started_at: str
    godot_bin: str
    log_path: str

    @property
    def record_file(self) -> Path:
        return _REGISTRY_DIR / f"{project_hash(Path(self.project_root))}.json"


def project_hash(project_root: Path) -> str:
    return hashlib.sha1(str(Path(project_root).resolve()).encode()).hexdigest()[:12]


def register(
    project_root: Path,
    *,
    pid: int,
    port: int,
    godot_bin: str,
    log_path: str,
) -> None:
    _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_root": str(Path(project_root).resolve()),
        "pid": pid,
        "port": port,
        "started_at": _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "godot_bin": godot_bin,
        "log_path": log_path,
    }
    target = _REGISTRY_DIR / f"{project_hash(project_root)}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def unregister(project_root: Path) -> None:
    target = _REGISTRY_DIR / f"{project_hash(project_root)}.json"
    target.unlink(missing_ok=True)


def list_all() -> list[DaemonRecord]:
    """List live records. Dead PIDs are pruned (registry + project state files)."""
    if not _REGISTRY_DIR.exists():
        return []
    live: list[DaemonRecord] = []
    for f in sorted(_REGISTRY_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            r = DaemonRecord(**{k: data[k] for k in DaemonRecord.__dataclass_fields__})
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        if _process_alive(r.pid):
            live.append(r)
        else:
            _prune(r, f)
    return live


def _prune(record: DaemonRecord, registry_file: Path) -> None:
    registry_file.unlink(missing_ok=True)
    ctrl = Path(record.project_root) / ".cli_control"
    (ctrl / "godot.pid").unlink(missing_ok=True)
    (ctrl / "port").unlink(missing_ok=True)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user; treat as alive
    except OSError:
        return False
    return True
