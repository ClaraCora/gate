[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e '.[dev]'"
}

Push-Location $projectRoot
try {
    & $python -m ruff check backend
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -m ruff format --check backend
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -m mypy backend/src backend/tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -m pytest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if (Test-Path -LiteralPath "frontend/package.json") {
        & npm --prefix frontend run test
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & npm --prefix frontend run build
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
finally {
    Pop-Location
}
