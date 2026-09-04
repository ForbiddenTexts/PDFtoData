<#
    update_helper.ps1 - post-exit file swap + relaunch + watchdog rollback.

    A running PDFtoData.exe cannot be overwritten, so the app hands off to this
    script and exits. Everything here is idempotent and fails safe: if anything
    goes wrong the backup made by updater.py is restored and the old app relaunched.

    Invoked as:
        powershell -NoProfile -ExecutionPolicy Bypass -File update_helper.ps1 -PlanFile <plan.json>

    Plan JSON (written by updater.apply_app_update):
        install_dir, stage_dir, backup_dir, new_version, old_version,
        exe_name, app_pid, files[], watchdog_sec, log
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PlanFile
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------- logging
$script:LogFile = Join-Path $env:LOCALAPPDATA 'PDFtoData\updater.log'

# PowerShell 5.1's Set-Content/Add-Content -Encoding utf8 emits a BOM. Everything
# here is read back by Python, so write BOM-less UTF-8 via .NET instead.
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Utf8 {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, $script:Utf8NoBom)
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "[{0}] [helper] {1} {2}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $Level, $Message
    try {
        $dir = Split-Path -Parent $script:LogFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        [System.IO.File]::AppendAllText($script:LogFile, $line + [Environment]::NewLine, $script:Utf8NoBom)
    } catch { }
    Write-Host $line
}

function Fail-And-Rollback {
    param([string]$Reason)
    Write-Log "FAILURE: $Reason" 'ERROR'
    try {
        Restore-Backup
        $marker = @{
            reason      = $Reason
            rolled_back = $true
            at          = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
            new_version = $plan.new_version
            old_version = $plan.old_version
        } | ConvertTo-Json
        Write-Utf8 -Path (Join-Path $plan.install_dir '.update-failed.json') -Content $marker
        Write-Log 'rollback complete; failure marker written'
    } catch {
        Write-Log "rollback itself failed: $($_.Exception.Message)" 'ERROR'
    }
    Start-App
    exit 1
}

function Restore-Backup {
    $manifestPath = Join-Path $plan.backup_dir '.backup-manifest.json'
    if (-not (Test-Path $manifestPath)) {
        Write-Log 'no backup manifest found; nothing to restore' 'WARN'
        return
    }
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    foreach ($entry in $manifest.files) {
        $src = Join-Path $plan.backup_dir $entry.path
        $dst = Join-Path $plan.install_dir $entry.path
        if (-not (Test-Path $src)) { continue }
        try {
            $parent = Split-Path -Parent $dst
            if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            if ((Test-Path $dst) -and $dst.ToLower().EndsWith('.exe')) {
                try { Move-Item -LiteralPath $dst -Destination "$dst.old" -Force } catch { }
            }
            Copy-Item -LiteralPath $src -Destination $dst -Force
            Write-Log "restored $($entry.path)"
        } catch {
            Write-Log "could not restore $($entry.path): $($_.Exception.Message)" 'ERROR'
        }
    }
    if ($manifest.version_json) {
        try {
            $json = $manifest.version_json | ConvertTo-Json -Depth 10
            Write-Utf8 -Path (Join-Path $plan.install_dir 'version.json') -Content $json
            Write-Log 'version.json reverted'
        } catch {
            Write-Log "could not revert version.json: $($_.Exception.Message)" 'ERROR'
        }
    }
}

function Start-App {
    $exe = Join-Path $plan.install_dir $plan.exe_name
    if (Test-Path $exe) {
        try {
            Start-Process -FilePath $exe -WorkingDirectory $plan.install_dir
            Write-Log "launched $exe"
        } catch {
            Write-Log "could not launch $($exe): $($_.Exception.Message)" 'ERROR'
        }
    } else {
        Write-Log "cannot launch - $exe is missing" 'ERROR'
    }
}

# --------------------------------------------------------------------------- start
if (-not (Test-Path $PlanFile)) {
    Write-Log "plan file not found: $PlanFile" 'ERROR'
    exit 2
}
$plan = Get-Content $PlanFile -Raw | ConvertFrom-Json
if ($plan.log) { $script:LogFile = $plan.log }

Write-Log "=== update start: $($plan.old_version) -> $($plan.new_version) ==="
Write-Log "install=$($plan.install_dir)"
Write-Log "stage=$($plan.stage_dir)"
Write-Log "backup=$($plan.backup_dir)"
Write-Log "files to swap: $($plan.files.Count)"

# --------------------------------------------------------- (a) wait for the app to exit
$deadline = (Get-Date).AddSeconds(60)
$exited = $false
while ((Get-Date) -lt $deadline) {
    $proc = Get-Process -Id $plan.app_pid -ErrorAction SilentlyContinue
    if ($null -eq $proc) { $exited = $true; break }
    Start-Sleep -Milliseconds 400
}
if (-not $exited) {
    Write-Log "app (pid $($plan.app_pid)) did not exit within 60s - aborting, nothing was changed" 'ERROR'
    exit 3
}
Write-Log "app pid $($plan.app_pid) has exited"
Start-Sleep -Milliseconds 600   # let Windows release the file handles

# ------------------------------------------------- (c) delete stale .old from past runs
try {
    Get-ChildItem -Path $plan.install_dir -Recurse -Filter '*.old' -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            try { Remove-Item -LiteralPath $_.FullName -Force; Write-Log "removed stale $($_.Name)" }
            catch { Write-Log "could not remove stale $($_.Name)" 'WARN' }
        }
} catch { Write-Log 'stale .old sweep skipped' 'WARN' }

# ------------------------------------------------------------- (b) staged file swap
$copied = 0
foreach ($rel in $plan.files) {
    $src = Join-Path $plan.stage_dir $rel
    $dst = Join-Path $plan.install_dir $rel
    if (-not (Test-Path $src)) {
        Fail-And-Rollback "staged file missing: $rel"
    }
    try {
        $parent = Split-Path -Parent $dst
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        # A just-exited exe can still hold a lock; rename it out of the way first.
        if ((Test-Path $dst) -and $dst.ToLower().EndsWith('.exe')) {
            Move-Item -LiteralPath $dst -Destination "$dst.old" -Force
        }
        Copy-Item -LiteralPath $src -Destination $dst -Force
        $copied++
    } catch {
        Fail-And-Rollback "could not write $($rel): $($_.Exception.Message)"
    }
}
Write-Log "swapped $copied file(s)"

# ------------------------------------------------------------------ (d) relaunch
$startedOk = Join-Path $plan.install_dir '.started-ok'
if (Test-Path $startedOk) { Remove-Item -LiteralPath $startedOk -Force -ErrorAction SilentlyContinue }
Start-App

# ------------------------------------------- watchdog: auto-rollback if it never starts
$wait = 15
if ($plan.watchdog_sec) { $wait = [int]$plan.watchdog_sec }
Write-Log "watchdog: waiting ${wait}s for .started-ok"
$limit = (Get-Date).AddSeconds($wait)
$ok = $false
while ((Get-Date) -lt $limit) {
    if (Test-Path $startedOk) { $ok = $true; break }
    Start-Sleep -Milliseconds 500
}

if ($ok) {
    Write-Log "new version $($plan.new_version) started successfully"
    try {
        if (Test-Path $plan.stage_dir) { Remove-Item -LiteralPath $plan.stage_dir -Recurse -Force }
        Write-Log 'staging directory cleaned up'
    } catch { Write-Log 'could not clean staging directory' 'WARN' }
    Write-Log '=== update complete ==='
    exit 0
}

Fail-And-Rollback "the new version did not start within ${wait}s (no .started-ok)"
