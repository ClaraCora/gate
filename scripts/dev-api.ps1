[CmdletBinding()]
param(
    [string]$Config = "config/gate.example.yaml"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv. Install the Python development dependencies first."
}

Push-Location $projectRoot
try {
    $env:GATE_CONFIG = $Config
    $env:GATE_AUTH_ENABLED = "false"
    & $python -m uvicorn gate.api:create_dev_app --factory --host 127.0.0.1 --port 18080 --reload
}
finally {
    Remove-Item Env:GATE_CONFIG -ErrorAction SilentlyContinue
    Remove-Item Env:GATE_AUTH_ENABLED -ErrorAction SilentlyContinue
    Pop-Location
}
