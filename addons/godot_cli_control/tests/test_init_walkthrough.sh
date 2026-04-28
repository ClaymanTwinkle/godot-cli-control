#!/usr/bin/env bash
# Init walkthrough：验证 `godot-cli-control init` 一键接入 + daemon 启停的纯 Python 路径
# （与 test_walkthrough.sh 测的 bash shim 路径互补）。
#
# 步骤：mktemp 项目（不带 plugin、不写 [autoload]/[editor_plugins]）→
#       pip install → init → daemon start → tree → daemon stop
#
# 用法：
#   GODOT_BIN=/path/to/godot ./test_init_walkthrough.sh
#   （未设置时尝试 macOS 默认路径或 PATH 中的 godot）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

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

TMPDIR_ROOT="$(mktemp -d -t godot-cli-control-init-XXXXXX)"
trap 'cleanup' EXIT

cleanup() {
    local exit_code=$?
    if [[ -n "${TMPDIR_ROOT:-}" && -d "$TMPDIR_ROOT" ]]; then
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
        echo "==> init walkthrough PASS"
    else
        echo "==> init walkthrough FAIL (exit $exit_code)" >&2
    fi
    exit $exit_code
}

echo "==> 临时项目: $TMPDIR_ROOT"
cd "$TMPDIR_ROOT"

# 1) 创建 minimal Godot 项目（**不预置 autoload / editor_plugins / addons**，
#    模拟全新项目，依赖 init 命令注入所有配置）
cat > project.godot <<'EOF'
config_version=5

[application]
config/name="init-walkthrough"
config/features=PackedStringArray("4.4", "GL Compatibility")
run/main_scene="res://main.tscn"

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
EOF

cat > main.tscn <<'EOF'
[gd_scene format=3 uid="uid://initwalk_main"]

[node name="Main" type="Node2D"]
EOF

# 2) pip install python client
echo "==> 创建 venv + 安装 python client"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet "$REPO_ROOT/python"

# 3) init —— 一键接入：复制 plugin + patch project.godot + 写 godot_bin
echo "==> godot-cli-control init"
export GODOT_BIN="$GODOT"  # init 会自动检测，env 是兜底
godot-cli-control init

# 验证 init 产物
test -f "addons/godot_cli_control/plugin.cfg" || { echo "FAIL: 插件未复制" >&2; exit 1; }
grep -q '^\[autoload\]$' project.godot || { echo "FAIL: [autoload] 段缺失" >&2; exit 1; }
grep -q '^GameBridgeNode=' project.godot || { echo "FAIL: GameBridgeNode 未注入" >&2; exit 1; }
grep -q '^enabled=PackedStringArray' project.godot || { echo "FAIL: editor_plugins 未注入" >&2; exit 1; }
test -f ".cli_control/godot_bin" || { echo "FAIL: godot_bin 未写入" >&2; exit 1; }

# 4) daemon start
echo "==> godot-cli-control daemon start --headless --port 9897"
godot-cli-control daemon start --headless --port 9897

# 5) tree —— 验证 RPC
echo "==> tree 1"
TREE_JSON="$(godot-cli-control --port 9897 tree 1)"
echo "$TREE_JSON"
if ! echo "$TREE_JSON" | grep -q '"name"'; then
    echo "FAIL: tree 输出不含 name 字段" >&2
    godot-cli-control daemon stop || true
    exit 1
fi

# 6) daemon stop
echo "==> godot-cli-control daemon stop"
godot-cli-control daemon stop
