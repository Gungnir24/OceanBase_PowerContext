# Windows: 在 qoder 目录下执行
#   .\scripts\run.ps1
# 如需自定义端口：.\scripts\run.ps1 --port 9000
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$AppArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = $env:PYTHON
if (-not $python) {
  $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
  throw "找不到 python，请先安装 Python 3.11+ 或设置 `$env:PYTHON"
}

& $python "app/server.py" @AppArgs
exit $LASTEXITCODE
