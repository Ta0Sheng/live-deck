# live-deck 一键安装（Windows）
#
#   .\scripts\setup.ps1              # CPU 字幕 + 桌面窗口
#   .\scripts\setup.ps1 -Gpu         # CUDA 字幕
#   .\scripts\setup.ps1 -SkipModels  # 不下模型（离线/只要界面）
#   .\scripts\setup.ps1 -Dev         # 开发依赖（pytest / ruff）

param(
    [switch]$Gpu,
    [switch]$SkipModels,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "==> 创建 venv：$venv"
    $launcher = Get-Command python -ErrorAction SilentlyContinue
    if ($launcher) {
        & python -m venv $venv
    } else {
        & py -3 -m venv $venv
    }
}

Write-Host "==> 安装依赖"
& $python -m pip install -U pip
if ($Dev) {
    & $python -m pip install -e ".[dev]"
} elseif ($Gpu) {
    & $python -m pip install -e ".[asr-gpu]"
} else {
    & $python -m pip install -e ".[asr]"
}

# 桌面窗口是可选件：pywebview 在新 Python 上可能装不上，装不上就用浏览器
if (-not $Dev) {
    & $python -m pip install -e ".[window]"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pywebview 装不上，live-deck app 会自动用浏览器打开"
    }
}

if (-not (Test-Path (Join-Path $root "config.toml"))) {
    Write-Host "==> 生成 config.toml"
    & $python -m live_deck.cli init
}

if (-not $SkipModels) {
    Write-Host "==> 下载字幕模型（约 300 MB）"
    & $python -m live_deck.cli models install
}

Write-Host "==> 体检"
& $python -m live_deck.cli doctor

Write-Host ""
Write-Host "完成。启动："
Write-Host "    .\scripts\run.ps1          # 桌面窗口"
Write-Host "    .\scripts\run.ps1 demo     # 演示模式"
