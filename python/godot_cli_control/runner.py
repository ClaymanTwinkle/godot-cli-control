"""脚本运行器 —— 加载用户脚本并注入 GameBridge。

用法: python3 -m godot_cli_control.runner <script.py> [--port N]

用户脚本只需定义 run(bridge) 函数：

    def run(bridge):
        bridge.click("/root/MyScene/StartButton")
        bridge.wait(2)
        bridge.hold("run", 1.5)

实际执行委托给 ``cli._exec_user_script``，保持单一加载路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .cli import _exec_user_script
from .client import DEFAULT_PORT


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python3 -m godot_cli_control.runner <script.py> [--port N]", file=sys.stderr)
        sys.exit(1)

    script_path = Path(sys.argv[1])
    if not script_path.exists():
        print(f"错误：找不到脚本: {script_path}", file=sys.stderr)
        sys.exit(1)

    port = DEFAULT_PORT
    args = sys.argv[2:]
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 >= len(args):
            print("错误：--port 需要一个值", file=sys.stderr)
            sys.exit(1)
        try:
            port = int(args[idx + 1])
        except ValueError:
            print(
                f"错误：--port 值必须是整数，收到 {args[idx + 1]!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    sys.exit(_exec_user_script(script_path, port))


if __name__ == "__main__":
    main()
