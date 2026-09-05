[CmdletBinding()]
param(
    [string]$HostAlias = "HK-Aliyun",
    [switch]$Bootstrap,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$commit = (& git -C $projectRoot rev-parse --short HEAD).Trim()
$dirty = if (& git -C $projectRoot status --porcelain) { "-dirty" } else { "" }
$releaseId = "$timestamp-$commit$dirty"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempRoot = Join-Path $tempBase ("gate-deploy-" + [Guid]::NewGuid().ToString("N"))
$archiveName = "gate-$releaseId.tar.gz"
$archivePath = Join-Path $tempRoot $archiveName

New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
    Push-Location $projectRoot
    try {
        if (-not $SkipTests) {
            & (Join-Path $PSScriptRoot "test.ps1")
            if ($LASTEXITCODE -ne 0) {
                throw "Project checks failed"
            }
        }
        if (Test-Path -LiteralPath "frontend/package.json") {
            & npm --prefix frontend ci
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to install frontend dependencies"
            }
            & npm --prefix frontend run build
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to build frontend"
            }
        }
        & tar `
            --exclude=.git `
            --exclude=.venv `
            --exclude=frontend/node_modules `
            --exclude='*.db' `
            --exclude=.impeccable/review `
            -czf $archivePath .
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to build release archive"
        }
    }
    finally {
        Pop-Location
    }

    if ($Bootstrap) {
        & scp (Join-Path $projectRoot "deploy/bootstrap.sh") "${HostAlias}:/tmp/gate-bootstrap.sh"
        & ssh $HostAlias "chmod 0700 /tmp/gate-bootstrap.sh && /tmp/gate-bootstrap.sh"
    }

    & scp $archivePath "${HostAlias}:/tmp/$archiveName"
    & scp (Join-Path $projectRoot "deploy/install-release.sh") "${HostAlias}:/tmp/gate-install-release.sh"
    $remoteUid = (& ssh $HostAlias "id -u").Trim()
    if ($LASTEXITCODE -ne 0 -or $remoteUid -notmatch '^\d+$') {
        throw "Unable to determine remote SSH user identity"
    }
    $remoteInstall = if ($remoteUid -eq "0") { "/tmp/gate-install-release.sh" } else { "sudo /tmp/gate-install-release.sh" }
    & ssh $HostAlias "chmod 0700 /tmp/gate-install-release.sh && $remoteInstall /tmp/$archiveName $releaseId"
    if ($LASTEXITCODE -ne 0) {
        throw "Remote Gate deployment failed"
    }
    Write-Host "Gate release $releaseId deployed to $HostAlias"
}
finally {
    $resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
    if ($resolvedTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedTemp).StartsWith("gate-deploy-")) {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
