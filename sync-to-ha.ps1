# sync-to-ha.ps1 -- One-shot sync of sleep_classifier/ to an HA Samba share,
# then automatically REBUILD + RESTART the add-on via Supervisor API.
#
# Usage:
#   .\sync-to-ha.ps1                  # pull latest from origin/main, then sync + rebuild
#   .\sync-to-ha.ps1 -SkipPull        # sync current working copy without git pull
#   .\sync-to-ha.ps1 -HAHost 10.0.0.5 # non-default HA IP
#   .\sync-to-ha.ps1 -SkipRebuild     # only sync files, don't trigger rebuild
#   .\sync-to-ha.ps1 -HAToken "xxx"   # provide HA Long-Lived Access Token inline
#
# Prereqs:
#   1. HA has the official Samba share add-on running
#   2. HA's addons share is reachable at \\$HAHost\addons
#   3. You run this from the repo root (where sleep_classifier/ lives)
#   4. For auto-rebuild: set env var HA_TOKEN or pass -HAToken with a
#      Long-Lived Access Token (Settings > People > Security > Create Token)

param(
    [string]$HAHost = "192.168.31.71",
    [switch]$SkipPull,
    [switch]$SkipRebuild,
    [string]$HAToken = "",
    [switch]$DumpLogs
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

Write-Host "==> Sleep Classifier sync" -ForegroundColor Cyan
Write-Host "    target: \\$HAHost\addons\sleep_classifier"
Write-Host "    source: $repoRoot\sleep_classifier"
Write-Host "    auto-rebuild: $(-not $SkipRebuild)"
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

# ── Auto-rebuild via Supervisor API ─────────────────────────────────────────
# The Supervisor API lets us stop/rebuild/start the add-on without touching
# the HA Web UI.  We need a Long-Lived Access Token for authentication.
# The add-on slug is "local_sleep_classifier" (local add-ons get "local_" prefix).

$addonSlug = "local_sleep_classifier"
$supervisorBase = "http://${HAHost}:8123/api/hassio"

# Resolve token: param > env var > skip
if (-not $HAToken) {
    $HAToken = $env:HA_TOKEN
}

if ($SkipRebuild) {
    Write-Host ""
    Write-Host "==> Skipping rebuild (--SkipRebuild)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Manual steps in HA Web UI:" -ForegroundColor Cyan
    Write-Host "  1. Settings > Add-ons > Sleep Classifier"
    Write-Host "  2. STOP -> REBUILD -> START"
    exit 0
}

if (-not $HAToken) {
    Write-Host ""
    Write-Host "==> No HA_TOKEN found — skipping auto-rebuild" -ForegroundColor Yellow
    Write-Host "    To enable auto-rebuild, set env var HA_TOKEN or pass -HAToken" -ForegroundColor Yellow
    Write-Host "    (Create one at: Settings > People > Security > Long-Lived Access Tokens)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Manual steps in HA Web UI:" -ForegroundColor Cyan
    Write-Host "  1. Settings > Add-ons > Sleep Classifier"
    Write-Host "  2. STOP -> REBUILD -> START"
    exit 0
}

$headers = @{
    "Authorization" = "Bearer $HAToken"
    "Content-Type"  = "application/json"
}

function Invoke-HAApi {
    param([string]$Method, [string]$Endpoint, [int]$TimeoutSec = 300)
    $url = "${supervisorBase}${Endpoint}"
    try {
        $resp = Invoke-RestMethod -Uri $url -Method $Method -Headers $headers -TimeoutSec $TimeoutSec
        return $resp
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        Write-Warning "API call failed: $Method $url -> HTTP $status"
        Write-Warning $_.Exception.Message
        return $null
    }
}

# Diagnostic helper: dump Supervisor logs so we can see WHY install fails.
# Usage: .\sync-to-ha.ps1 -DumpLogs
if ($DumpLogs) {
    if (-not $HAToken) {
        Write-Host "==> -DumpLogs requires -HAToken or HA_TOKEN env var" -ForegroundColor Red
        exit 1
    }
    Write-Host ""
    Write-Host "==> Pulling Supervisor logs..." -ForegroundColor Yellow
    try {
        $supLogs = Invoke-RestMethod -Uri "${supervisorBase}/supervisor/logs" -Headers $headers -TimeoutSec 30
        $supLogFile = Join-Path $repoRoot "supervisor.log"
        $supLogs | Out-File -FilePath $supLogFile -Encoding utf8
        Write-Host "    wrote $supLogFile" -ForegroundColor Green
    } catch {
        Write-Warning "supervisor log fetch failed: $($_.Exception.Message)"
    }

    Write-Host ""
    Write-Host "==> Pulling add-on build logs (if exists)..." -ForegroundColor Yellow
    try {
        $addonLogs = Invoke-RestMethod -Uri "${supervisorBase}/addons/$addonSlug/logs" -Headers $headers -TimeoutSec 30
        $addonLogFile = Join-Path $repoRoot "addon.log"
        $addonLogs | Out-File -FilePath $addonLogFile -Encoding utf8
        Write-Host "    wrote $addonLogFile" -ForegroundColor Green
    } catch {
        Write-Host "    no add-on logs (probably not yet installed)" -ForegroundColor DarkYellow
    }

    Write-Host ""
    Write-Host "==> Add-on info (state / version / image):" -ForegroundColor Yellow
    try {
        $info = Invoke-RestMethod -Uri "${supervisorBase}/addons/$addonSlug/info" -Headers $headers -TimeoutSec 30
        $info | ConvertTo-Json -Depth 6 | Out-Host
    } catch {
        Write-Host "    add-on not registered with Supervisor" -ForegroundColor DarkYellow
    }
    exit 0
}

# Step 5: Stop the add-on (ignore error if already stopped)
Write-Host ""
Write-Host "==> [5/7] Stopping add-on..." -ForegroundColor Yellow
$stopResult = Invoke-HAApi -Method "POST" -Endpoint "/addons/$addonSlug/stop"
if ($stopResult) {
    Write-Host "    Stopped" -ForegroundColor Green
} else {
    Write-Host "    (may already be stopped, continuing)" -ForegroundColor DarkYellow
}
Start-Sleep -Seconds 2

# Step 6: Rebuild the add-on (this triggers a docker build, can take 2-5 min)
Write-Host ""
Write-Host "==> [6/7] Rebuilding add-on (this may take 2-5 minutes)..." -ForegroundColor Yellow
$rebuildResult = Invoke-HAApi -Method "POST" -Endpoint "/addons/$addonSlug/rebuild" -TimeoutSec 600
if ($rebuildResult) {
    Write-Host "    Rebuild complete" -ForegroundColor Green
} else {
    Write-Error "Rebuild failed! Check HA logs for details."
    exit 1
}

# Step 7: Start the add-on
Write-Host ""
Write-Host "==> [7/7] Starting add-on..." -ForegroundColor Yellow
$startResult = Invoke-HAApi -Method "POST" -Endpoint "/addons/$addonSlug/start"
if ($startResult) {
    Write-Host "    Started" -ForegroundColor Green
} else {
    Write-Error "Start failed! Check HA logs for details."
    exit 1
}

Write-Host ""
$remoteVersion = "unknown"
if (Test-Path $configYaml) {
    $vline = Get-Content $configYaml | Where-Object { $_ -match "^version:" } | Select-Object -First 1
    if ($vline -match 'version:\s*["'']?([^"''\s]+)') { $remoteVersion = $Matches[1] }
}
Write-Host "==> All done! Add-on v$remoteVersion is running on $HAHost" -ForegroundColor Green
Write-Host "    Open Web UI: http://${HAHost}:8123/hassio/ingress/local_sleep_classifier" -ForegroundColor Cyan
