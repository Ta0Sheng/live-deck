# 启动 live-deck（Windows）
#   .\scripts\run.ps1            # 桌面窗口 / 浏览器降级
#   .\scripts\run.ps1 demo       # 演示模式
#   .\scripts\run.ps1 serve      # 只起服务

param([string]$Mode = "app")

$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root ".venv\Scripts\live-deck.exe"

if (-not (Test-Path $exe)) {
    Write-Error "还没安装。先跑 .\scripts\setup.ps1"
    exit 1
}

switch ($Mode) {
    "demo"  { & $exe demo }
    "serve" { & $exe serve }
    default { & $exe app }
}
