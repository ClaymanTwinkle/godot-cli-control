#!/usr/bin/env bash
# GUT runner: 在临时 Godot 项目里 git clone GUT + 复制本 plugin + 跑 gut_cmdln.gd。
#
# 没有把 GUT vendor 进仓库（它是开发依赖，不该跟 plugin 一起发布到 PyPI/AssetLib）；
# 临时项目从头建，避免污染仓库的 .godot/ import 缓存。
#
# 跨平台：CI（ubuntu / macOS / windows 三格）跑的是同目录的 run_gut.py
# （bash 在 Windows 用不了）。本 .sh 保留给 Linux / macOS 本地开发者方便用；
# 两者逻辑等价，改一个记得对齐另一个。
#
# 用法：
#   GODOT_BIN=/path/to/godot ./run_gut.sh
#   （未设置 GODOT_BIN 时尝试 macOS 默认路径或 PATH 中的 godot）

set -euo pipefail

GUT_REF="v9.4.0"  # bumping：检查 https://github.com/bitwes/Gut/releases

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

PROJ="$(mktemp -d -t godot-cli-control-gut-XXXXXX)"
trap 'rm -rf "$PROJ"' EXIT
echo "==> 临时项目: $PROJ"

# 1) minimal Godot project
cat > "$PROJ/project.godot" <<'EOF'
config_version=5

[application]
config/name="gut-tests"
config/features=PackedStringArray("4.4", "GL Compatibility")

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
EOF

# 2) plugin under test
mkdir -p "$PROJ/addons"
cp -r "$REPO_ROOT/addons/godot_cli_control" "$PROJ/addons/"

# 3) GUT —— GUT 仓库自身就是一个 Godot 项目，真正的 plugin 在它的
#    addons/gut/ 子目录。clone 到临时位置后只搬运 addons/gut/。
echo "==> 下载 GUT $GUT_REF"
GUT_SRC="$(mktemp -d -t gut-src-XXXXXX)"
trap 'rm -rf "$PROJ" "$GUT_SRC"' EXIT
git clone --depth 1 --branch "$GUT_REF" \
    https://github.com/bitwes/Gut.git "$GUT_SRC" >/dev/null 2>&1
if [[ ! -f "$GUT_SRC/addons/gut/gut_cmdln.gd" ]]; then
    echo "FAIL: GUT $GUT_REF 内未找到 addons/gut/gut_cmdln.gd（GUT 目录结构变了？）" >&2
    exit 1
fi
cp -r "$GUT_SRC/addons/gut" "$PROJ/addons/gut"

# 4) headless 预热（把 .gd / .tscn import 到 .godot/ 缓存里，
#    否则 gut_cmdln 启动时可能报 "Could not find script class for ..."）
echo "==> import 资源"
"$GODOT" --headless --path "$PROJ" --editor --quit >/dev/null 2>&1 || true

# 5) 跑 GUT —— 把 stdout 同步到 stderr+原 stdout 双份，便于 grep marker
echo "==> 跑 GUT"
RUN_LOG="$(mktemp -t gut-run-XXXXXX.log)"
trap 'rm -rf "$PROJ" "$GUT_SRC" "$RUN_LOG"' EXIT
"$GODOT" --headless --path "$PROJ" \
    -s res://addons/gut/gut_cmdln.gd \
    -gdir=res://addons/godot_cli_control/tests/gut \
    -gexit 2>&1 | tee "$RUN_LOG"

# 假绿守卫（issue #188）：GUT 遇到解析失败的测试脚本只告警并跳过（不计入统计、
# 不判失败），"All tests passed!" marker 对整文件被跳过是盲区。判 PASS 前加两道
# 独立检测，任一命中即 FAIL：

# 1) 引擎/GUT 在跳过脚本时固定打这三类字样之一。
if grep -qE 'Parse Error|Failed to load script|Ignoring script' "$RUN_LOG"; then
    echo "FAIL: 检测到脚本解析失败被 GUT 静默跳过（Parse Error / Failed to load script / Ignoring script）—— 看上面输出排查" >&2
    exit 1
fi

# 2) 比对 GUT 汇总的 "Scripts" 计数与 tests/gut/ 下实际的 test_*.gd 文件数
#    （add_directory 不递归子目录，这里也用 -maxdepth 1 对齐）。
EXPECTED_SCRIPTS="$(find "$REPO_ROOT/addons/godot_cli_control/tests/gut" -maxdepth 1 -name 'test_*.gd' | wc -l | tr -d ' ')"
ACTUAL_SCRIPTS="$(grep -E '^Scripts[[:space:]]+[0-9]+' "$RUN_LOG" | grep -oE '[0-9]+' | tail -1)"
if [[ -z "$ACTUAL_SCRIPTS" ]]; then
    echo "FAIL: 未能从 GUT 输出中解析出 'Scripts' 计数 —— 看上面输出排查" >&2
    exit 1
fi
if [[ "$ACTUAL_SCRIPTS" -lt "$EXPECTED_SCRIPTS" ]]; then
    echo "FAIL: GUT 实际执行脚本数 ($ACTUAL_SCRIPTS) 少于 tests/gut/test_*.gd 文件数 ($EXPECTED_SCRIPTS) —— 有脚本被静默跳过" >&2
    exit 1
fi

# Godot 在 cmdln 脚本加载失败时仍可能 exit 0；额外断言：GUT 的成功 marker。
if grep -q "All tests passed!" "$RUN_LOG"; then
    echo "==> GUT PASS"
else
    echo "FAIL: 没看到 GUT 的 'All tests passed!' marker —— 看上面输出排查" >&2
    exit 1
fi
