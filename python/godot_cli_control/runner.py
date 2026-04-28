"""脚本运行器 —— 加载用户脚本并注入 GameBridge。

用法: python3 -m godot_cli_control.runner <script.py> [args...]

用户脚本只需定义 run(bridge) 函数：

    def run(bridge):
        bridge.click("/root/MainMenu/.../NewGameButton")
        bridge.wait(2)
        bridge.hold("run", 1.5)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .bridge import GameBridge


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python3 -m godot_cli_control.runner <script.py> [args...]", file=sys.stderr)
        sys.exit(1)

    script_path = Path(sys.argv[1])
    if not script_path.exists():
        print(f"错误：找不到脚本: {script_path}", file=sys.stderr)
        sys.exit(1)

    # 解析 --port 参数
    port = 9877
    args = sys.argv[2:]
    if "--port" in args:
        idx = args.index("--port")
        port = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    # 动态加载用户脚本
    spec = importlib.util.spec_from_file_location("user_script", script_path)
    if spec is None or spec.loader is None:
        print(f"错误：无法加载脚本: {script_path}", file=sys.stderr)
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "run"):
        print(f"错误：脚本 {script_path} 中缺少 run(bridge) 函数", file=sys.stderr)
        sys.exit(1)

    # 创建 bridge 并执行
    bridge = GameBridge(port=port)
    try:
        module.run(bridge)
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
