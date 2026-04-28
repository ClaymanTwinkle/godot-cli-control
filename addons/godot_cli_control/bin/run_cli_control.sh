#!/usr/bin/env bash
set -euo pipefail

# WebSocket 连接不应走 HTTP 代理
export no_proxy="${no_proxy:+${no_proxy},}localhost,127.0.0.1"

# 统一 CLI Control 入口
#
# 用法：
#   ./run_cli_control.sh start [--record] [--movie-path PATH] [--headless] [--fps N] [--port N]
#   ./run_cli_control.sh stop
#   ./run_cli_control.sh click <node_path>
#   ./run_cli_control.sh screenshot [output_path]
#   ./run_cli_control.sh tree [depth]
#   ./run_cli_control.sh press <action>
#   ./run_cli_control.sh hold <action> <duration>
#   ./run_cli_control.sh combo <json_file>
#   ./run_cli_control.sh release-all
#   ./run_cli_control.sh run [--record] [--movie-path PATH] [--fps N] [--port N] <python_script> [args...]

DEFAULT_GODOT_BIN="/Applications/Godot.app/Contents/MacOS/Godot"
GODOT_BIN="${GODOT_BIN:-$DEFAULT_GODOT_BIN}"
export CLI_CONTROL_DIR="${CLI_CONTROL_DIR:-.cli_control}"
PID_FILE="$CLI_CONTROL_DIR/godot.pid"
DEFAULT_PORT=9877

# 跳到项目根（脚本在 addons/godot_cli_control/bin/，往上 3 级）
cd "$(dirname "$0")/../../.."

# ── 工具函数 ──

_ensure_imported() {
    if [[ -d ".godot" ]] && [[ -f ".godot/global_script_class_cache.cfg" ]]; then
        return 0  # 已 import 过
    fi
    echo "首次导入项目资源（--editor --quit）..."
    "$GODOT_BIN" --headless --editor --quit --path . >/dev/null 2>&1 || true
}

_read_pid() {
    if [[ -f "$PID_FILE" ]]; then
        cat "$PID_FILE"
    fi
}

_is_our_godot_running() {
    local pid
    pid="$(_read_pid)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

_require_godot_bin() {
    if [[ ! -x "$GODOT_BIN" ]]; then
        echo "错误：找不到 Godot: $GODOT_BIN"
        echo "可通过 GODOT_BIN 环境变量指定路径"
        exit 1
    fi
}

_require_running() {
    if ! _is_our_godot_running; then
        echo "错误：没有运行中的 Godot 实例（先执行 start）"
        exit 1
    fi
}

# 等 TCP 端口可连接（GameBridge listen），最多 max 秒
_wait_port_ready() {
    local port="$1" max="${2:-30}"
    for i in $(seq 1 "$max"); do
        if python3 -c "import socket,sys
s=socket.socket(); s.settimeout(0.5)
try:
    s.connect(('127.0.0.1', $port)); s.close()
except Exception:
    sys.exit(1)" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ── start ──

cmd_start() {
    _require_godot_bin

    if _is_our_godot_running; then
        echo "Godot 已在运行 (PID $(_read_pid))，先执行 stop"
        exit 1
    fi

    mkdir -p "$CLI_CONTROL_DIR"
    umask 077  # PID/port 文件仅本用户可读

    local RECORD=false HEADLESS=false FPS=30 PORT=$DEFAULT_PORT MOVIE_PATH=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --record)      RECORD=true; shift ;;
            --headless)    HEADLESS=true; shift ;;
            --fps)         FPS="$2"; shift 2 ;;
            --port)        PORT="$2"; shift 2 ;;
            --movie-path)  MOVIE_PATH="$2"; shift 2 ;;
            *)             echo "未知选项: $1"; exit 1 ;;
        esac
    done

    # 先确保 Godot 资源缓存就位（不复用 import_project.sh，避免插件依赖业务脚本）
    _ensure_imported

    # 保存端口到文件供 proxy 命令使用
    echo "$PORT" > "$CLI_CONTROL_DIR/port"

    local GODOT_ARGS=(--path . --cli-control "--game-bridge-port=$PORT")

    if $HEADLESS; then
        GODOT_ARGS+=(--headless)
    fi

    if $RECORD; then
        if [[ -z "$MOVIE_PATH" ]]; then
            echo "错误：--record 需要指定 --movie-path <路径>"
            exit 1
        fi
        GODOT_ARGS+=("--write-movie" "$MOVIE_PATH" "--fixed-fps" "$FPS")
        export GODOT_MOVIE_MAKER=1
        echo "$MOVIE_PATH" > "$CLI_CONTROL_DIR/movie_path"
        echo "录制模式：$MOVIE_PATH (${FPS}fps)"
    fi

    "$GODOT_BIN" "${GODOT_ARGS[@]}" &
    local GODOT_PID=$!
    echo "$GODOT_PID" > "$PID_FILE"

    # 短暂等待后验证进程还在运行
    sleep 1
    if ! kill -0 "$GODOT_PID" 2>/dev/null; then
        echo "错误：Godot 启动后立即退出"
        rm -f "$PID_FILE"
        exit 1
    fi

    # 等 GameBridge 端口监听（最多 30s）
    echo "等待 GameBridge 就绪 (port $PORT)..."
    if ! _wait_port_ready "$PORT" 30; then
        echo "错误：GameBridge 30s 未就绪，清理"
        kill -TERM "$GODOT_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$GODOT_PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        exit 1
    fi

    echo "Godot 已启动 (PID $GODOT_PID)"
}

# ── stop ──

cmd_stop() {
    local pid
    local conv_failed=0
    pid="$(_read_pid)"

    if [[ -z "$pid" ]]; then
        echo "没有 PID 文件，无需停止"
        exit 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Godot (PID $pid) 已不在运行，清理 PID 文件"
        rm -f "$PID_FILE"
        exit 0
    fi

    # 校验 PID 真的是 Godot 进程（避免 PID 复用误杀）
    local pid_comm
    pid_comm=$(ps -p "$pid" -o comm= 2>/dev/null | tr -d ' ' || true)
    if [[ -z "$pid_comm" ]]; then
        echo "PID $pid 已不存在，清理 PID 文件"
        rm -f "$PID_FILE"
        exit 0
    fi
    # macOS comm 截 16 char（"Godot.app/Conten..." 等），Linux 输出 "Godot"；
    # 软校验：含 godot（不区分大小写）即认为是
    if ! echo "$pid_comm" | grep -qi "godot"; then
        echo "警告：PID $pid 进程名 '$pid_comm' 不像 Godot；拒绝 SIGTERM 以免误杀"
        echo "若确认需要清理，手动 kill 该 PID 并删 $PID_FILE"
        exit 1
    fi

    echo "关闭 Godot (PID $pid)..."
    kill -TERM "$pid"
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 10 ]]; do
        sleep 1
        waited=$((waited + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "SIGTERM 超时，强制终止"
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    echo "Godot 已停止"

    # 录制结束后自动转码为 mp4
    if [[ -f "$CLI_CONTROL_DIR/movie_path" ]]; then
        local movie_path
        movie_path="$(cat "$CLI_CONTROL_DIR/movie_path")"
        rm -f "$CLI_CONTROL_DIR/movie_path"
        if [[ -f "$movie_path" ]] && command -v ffmpeg &>/dev/null; then
            local mp4_path="${movie_path%.*}.mp4"
            echo "正在转码：$movie_path → $mp4_path ..."
            if ffmpeg -i "$movie_path" -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -y "$mp4_path" 2>"$CLI_CONTROL_DIR/ffmpeg.log"; then
                rm -f "$movie_path"
                echo "转码完成：$mp4_path ($(du -h "$mp4_path" | cut -f1))"
            else
                echo "转码失败，保留原始文件：$movie_path（日志：$CLI_CONTROL_DIR/ffmpeg.log）"
                conv_failed=1
            fi
        fi
    fi

    return $conv_failed
}

# ── proxy（转发到 Python CLI）──

cmd_proxy() {
    _require_running
    local port_args=()
    if [[ -f "$CLI_CONTROL_DIR/port" ]]; then
        port_args=(--port "$(cat "$CLI_CONTROL_DIR/port")")
    fi
    python3 -m godot_cli_control "${port_args[@]}" "$@"
}

# ── run ──

cmd_run() {
    local godot_opts=()
    local PY_SCRIPT=""
    local py_args=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --record|--headless)
                godot_opts+=("$1"); shift ;;
            --movie-path|--fps|--port)
                godot_opts+=("$1" "$2"); shift 2 ;;
            *)
                if [[ -z "$PY_SCRIPT" ]]; then
                    PY_SCRIPT="$1"
                else
                    py_args+=("$1")
                fi
                shift ;;
        esac
    done

    if [[ -z "$PY_SCRIPT" ]] || [[ ! -f "$PY_SCRIPT" ]]; then
        echo "错误：找不到脚本: $PY_SCRIPT"
        exit 1
    fi

    local auto_started=false
    if ! _is_our_godot_running; then
        cmd_start ${godot_opts[@]+"${godot_opts[@]}"} || exit 1
        auto_started=true
    fi

    # cmd_start 已等过端口就绪，这里直接读 port 进 runner
    local port
    if [[ ! -f "$CLI_CONTROL_DIR/port" ]]; then
        echo "错误：找不到 $CLI_CONTROL_DIR/port（daemon 状态文件丢失）"
        exit 1
    fi
    port=$(cat "$CLI_CONTROL_DIR/port")

    echo "运行 $PY_SCRIPT..."
    local exit_code=0
    python3 -m godot_cli_control.runner "$PY_SCRIPT" --port "$port" ${py_args[@]+"${py_args[@]}"} || exit_code=$?

    if [[ "$auto_started" == true ]]; then
        cmd_stop
    fi

    return $exit_code
}

# ── 主入口 ──

if [[ $# -lt 1 ]]; then
    cat <<'USAGE'
用法:
  ./run_cli_control.sh start [--record] [--movie-path PATH] [--headless] [--fps N] [--port N]
  ./run_cli_control.sh stop
  ./run_cli_control.sh click <node_path>
  ./run_cli_control.sh screenshot [output_path]
  ./run_cli_control.sh tree [depth]
  ./run_cli_control.sh press <action>
  ./run_cli_control.sh release <action>
  ./run_cli_control.sh tap <action> [duration]
  ./run_cli_control.sh hold <action> <duration>
  ./run_cli_control.sh combo <json_file>
  ./run_cli_control.sh release-all
  ./run_cli_control.sh run [options] <python_script> [args...]
USAGE
    exit 1
fi

SUBCMD="$1"
shift

case "$SUBCMD" in
    start)      cmd_start "$@" ;;
    stop)       cmd_stop ;;
    click|screenshot|tree|press|release|tap|hold|combo|release-all)
                cmd_proxy "$SUBCMD" "$@" ;;
    run)        cmd_run "$@" ;;
    *)          echo "未知命令: $SUBCMD"; exit 1 ;;
esac
