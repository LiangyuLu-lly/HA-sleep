# sync-to-ha.ps1 -- One-shot sync of sleep_classifier/ to an HA Samba share.
#
# Usage:
#   .\sync-to-ha.ps1                  # pull latest from origin/main, then sync
#   .\sync-to-ha.ps1 -SkipPull        # sync current working copy without git pull
#   .\sync-to-ha.ps1 -HAHost 10.0.0.5 # non-default HA IP
#
# Prereqs:
#   1. HA has the official Samba share add-on running
#   2. HA's addons share is reachable at \\$HAHost\addons
#   3. You run this from the repo root (where sleep_classifier/ lives)

param(
    [string]$HAHost = "192.168.31.71",
    [switch]$SkipPull
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

Write-Host "==> Sleep Classifier sync" -ForegroundColor Cyan
Write-Host "    target: \\$HAHost\addons\sleep_classifier"
Write-Host "    source: $repoRoot\sleep_classifier"
Write-Host ""

# 1. git pull
if (-not $SkipPull) {
    Write-Host "==> [1/4] git pull origin main" -ForegroundColor Yellow
    git pull origin main 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "git pull failed, continuing with current working tree"
    }
} else {
    Write-Host "==> [1/4] skipping git pull (--SkipPull)"
}

# 2. strip local __pycache__
Write-Host ""
Write-Host "==> [2/4] strip local __pycache__" -ForegroundColor Yellow
Get-ChildItem -Recurse -Path .\sleep_classifier -Filter "__pycache__" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue
}

# 3. robocopy /MIR
Write-Host ""
Write-Host "==> [3/4] robocopy to HA" -ForegroundColor Yellow
$dst = "\\$HAHost\addons\sleep_classifier"
robocopy .\sleep_classifier $dst /MIR /XD "__pycache__" /NFL /NDL /NJH /NJS /NC /NS /NP
$rc = $LASTEXITCODE
# robocopy 0-7 = success (0 none, 1 copied, 2 extras, 3 both, 4-7 warnings). 8+ = error.
if ($rc -ge 8) {
    Write-Error "robocopy failed with exit code $rc"
    exit 1
}
Write-Host "    robocopy OK (exit $rc)" -ForegroundColor Green

# 4. strip __pycache__ on the remote side (in case something slipped through /XD)
Write-Host ""
Write-Host "==> [4/4] strip remote __pycache__" -ForegroundColor Yellow
Get-ChildItem -Recurse -Path $dst -Filter "__pycache__" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue
}

# Print version confirmation
$configYaml = Join-Path $dst "config.yaml"
if (Test-Path $configYaml) {
    $versionLine = Get-Content $configYaml | Where-Object { $_ -match "^version:" } | Select-Object -First 1
    Write-Host ""
    Write-Host "==> Sync complete" -ForegroundColor Green
    Write-Host "    remote config.yaml: $versionLine"
}

Write-Host ""
Write-Host "Next steps in HA Web UI:" -ForegroundColor Cyan
Write-Host "  1. Settings > Add-ons > Add-on Store > triple-dot menu > Check for updates"
Write-Host "  2. Scroll to the Local add-ons section"
Write-Host "  3. If installed: STOP -> REBUILD -> START"
Write-Host "  4. If not yet installed: click INSTALL"
