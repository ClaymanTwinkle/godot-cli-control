#!/usr/bin/env bash
# Compatibility shim — historical entry point.
# 实际逻辑在 Python CLI（python/godot_cli_control/cli.py）。
# 保留此脚本是为已经把它写进 Makefile / docs / muscle memory 的用户。
#
# 推荐新用户直接：
#   pipx install godot-cli-control
#   godot-cli-control init           # 在 godot 项目根
#   godot-cli-control daemon start
#
# 子命令完全同步：start / stop / run / click / tree / screenshot / press /
# release / tap / hold / combo / release-all。

set -euo pipefail

# WebSocket 连接不应走 HTTP/SOCKS 代理（GameBridge 永远是 127.0.0.1）
export no_proxy="${no_proxy:+${no_proxy},}localhost,127.0.0.1"

# 跳到 Godot 项目根：脚本在 addons/godot_cli_control/bin/，往上 3 级。
# 这样无论用户从哪里调，相对路径 .cli_control/ 始终落在项目根。
cd "$(dirname "$0")/../../.."

# 把旧的非 daemon 子命令映射到新 CLI 形态
case "${1:-}" in
    start)         shift; exec python3 -m godot_cli_control daemon start "$@" ;;
    stop)          shift; exec python3 -m godot_cli_control daemon stop ;;
    run)           shift; exec python3 -m godot_cli_control run "$@" ;;
    click|screenshot|tree|press|release|tap|hold|combo|release-all)
                   exec python3 -m godot_cli_control "$@" ;;
    "")            exec python3 -m godot_cli_control --help ;;
    *)             exec python3 -m godot_cli_control "$@" ;;
esac
