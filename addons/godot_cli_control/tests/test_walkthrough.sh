#!/usr/bin/env bash
# Fresh-clone walkthrough：验证 README quick start 在干净 Godot 项目上能跑通。
#
# 步骤：mktemp 项目 → 复制 plugin → pip install → start (--headless) → tree → stop
# 不测 screenshot：headless 视觉路径见 issue #16 known limitation。
#
# 用法：
#   GODOT_BIN=/path/to/godot ./test_walkthrough.sh
#   （未设置 GODOT_BIN 时尝试 macOS 默认路径或 PATH 中的 godot）

set -euo pipefail

# 仓库根（脚本在 addons/godot_cli_control/tests/，往上 3 级）
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

# 找 Godot 二进制
if [[ -n "${GODOT_BIN:-}" ]]; then
    GODOT="$GODOT_BIN"
elif [[ -x "/Applications/Godot.app/Contents/MacOS/Godot" ]]; then
    GODOT="/Applications/Godot.app/Contents/MacOS/Godot"
elif command -v godot >/dev/null 2>&1; then
    GODOT="$(command -v godot)"
else
    echo "FAIL: 找不到 Godot 二进制（设置 GODOT_BIN 或加入 PATH）" >&2
    exit 1
fi

if [[ ! -x "$GODOT" ]]; then
    echo "FAIL: Godot binary 不可执行: $GODOT" >&2
    exit 1
fi

echo "==> 使用 Godot: $GODOT"
"$GODOT" --version

# 临时项目目录
TMPDIR_ROOT="$(mktemp -d -t godot-cli-control-walkthrough-XXXXXX)"
trap 'cleanup' EXIT

cleanup() {
    local exit_code=$?
    if [[ -n "${TMPDIR_ROOT:-}" && -d "$TMPDIR_ROOT" ]]; then
        # 保险：尝试停掉 daemon（PID 文件还在的话）
        if [[ -f "$TMPDIR_ROOT/.cli_control/godot.pid" ]]; then
            local pid
            pid="$(cat "$TMPDIR_ROOT/.cli_control/godot.pid" 2>/dev/null || true)"
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                kill -TERM "$pid" 2>/dev/null || true
                sleep 1
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -rf "$TMPDIR_ROOT"
    fi
    if [[ $exit_code -eq 0 ]]; then
        echo "==> walkthrough PASS"
    else
        echo "==> walkthrough FAIL (exit $exit_code)" >&2
    fi
    exit $exit_code
}

echo "==> 临时项目: $TMPDIR_ROOT"
cd "$TMPDIR_ROOT"

# 1) 创建 minimal Godot 项目
mkdir -p addons
cat > project.godot <<'EOF'
config_version=5

[application]
config/name="walkthrough"
config/features=PackedStringArray("4.4", "GL Compatibility")
run/main_scene="res://main.tscn"

[autoload]
; 直接注入 autoload（模拟 plugin 已在 editor 中启用），避免依赖交互式启用
GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"

[editor_plugins]
enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
EOF

cat > main.tscn <<'EOF'
[gd_scene format=3 uid="uid://walkthrough_main"]

[node name="Main" type="Node2D"]
EOF

# 2) 复制 plugin
cp -r "$REPO_ROOT/addons/godot_cli_control" addons/

# wrapper 脚本依赖相对路径 cd 到项目根（addons/<plugin>/bin/ 往上 3 级），
# 用插件内位置直接调用即可，不复制到项目根。
WRAPPER="./addons/godot_cli_control/bin/run_cli_control.sh"

# 3) pip install python client（用户独立 venv 模拟 fresh install）
echo "==> 创建 venv + 安装 python client"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet "$REPO_ROOT/python"

# 4) start daemon（headless）
echo "==> start daemon"
export GODOT_BIN="$GODOT"
"$WRAPPER" start --headless --port 9899

# 等待 GameBridge 端口监听（最多 60s）
# 用轻量 TCP 探测（python3 socket connect），避免 client.connect() 内部 10× retry
# 把单次循环拉到几十秒、整体超时到几分钟。
echo "==> 等待 GameBridge 就绪"
_port_ready() {
    python3 -c "import socket,sys
s=socket.socket(); s.settimeout(0.5)
try:
    s.connect(('127.0.0.1', 9899)); s.close()
except Exception:
    sys.exit(1)
" >/dev/null 2>&1
}
for i in $(seq 1 60); do
    if _port_ready; then
        echo "==> GameBridge 就绪 (${i}s)"
        break
    fi
    if [[ $i -eq 60 ]]; then
        echo "FAIL: GameBridge 60s 未就绪" >&2
        "$WRAPPER" stop || true
        exit 1
    fi
    sleep 1
done

# 5) tree 验证 RPC 能通
echo "==> tree 1"
TREE_JSON="$(python3 -m godot_cli_control --port 9899 tree 1)"
echo "$TREE_JSON"
if ! echo "$TREE_JSON" | grep -q '"name"'; then
    echo "FAIL: tree 输出不含 name 字段" >&2
    "$WRAPPER" stop || true
    exit 1
fi

# 6) stop
echo "==> stop daemon"
"$WRAPPER" stop
