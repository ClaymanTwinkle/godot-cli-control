# Compatibility shim — historical entry point (PowerShell, Windows / pwsh).
# 实际逻辑在 Python CLI（python/godot_cli_control/cli.py）。
# 与同目录 run_cli_control.sh 等价，覆盖 start / stop / run / click /
# screenshot / tree / press / release / tap / hold / combo / release-all。
#
# 推荐新用户直接：
#   pipx install godot-cli-control
#   godot-cli-control init           # 在 godot 项目根
#   godot-cli-control daemon start

$ErrorActionPreference = 'Stop'

# WebSocket 连接不应走 HTTP/SOCKS 代理（GameBridge 永远是 127.0.0.1）
if ($env:no_proxy) {
    $env:no_proxy = "$($env:no_proxy),localhost,127.0.0.1"
} else {
    $env:no_proxy = 'localhost,127.0.0.1'
}

# 跳到 Godot 项目根：脚本在 addons/godot_cli_control/bin/，往上 3 级。
# 这样无论用户从哪里调，相对路径 .cli_control/ 始终落在项目根。
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..\..')
Set-Location $projectRoot

# venv 通常只暴露 `python`（不一定 link 到 `python3`）。优先 `python`，
# 让脚本沿用调用者激活的解释器；fallback `python3` 兼容系统级安装。
$py = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    $py = 'python'
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $py = 'python3'
} else {
    Write-Error '错误：找不到 python / python3 解释器'
    exit 1
}

# 第一个参数是子命令；剩下的转发给 godot-cli-control。
$sub = if ($args.Count -ge 1) { $args[0] } else { '' }
$rest = if ($args.Count -ge 2) { $args[1..($args.Count - 1)] } else { @() }

switch ($sub) {
    'start'       { & $py -m godot_cli_control daemon start @rest }
    'stop'        { & $py -m godot_cli_control daemon stop }
    'run'         { & $py -m godot_cli_control run @rest }
    { $_ -in 'click','screenshot','tree','press','release','tap','hold','combo','release-all' } {
        & $py -m godot_cli_control @args
    }
    ''            { & $py -m godot_cli_control --help }
    default       { & $py -m godot_cli_control @args }
}
exit $LASTEXITCODE
