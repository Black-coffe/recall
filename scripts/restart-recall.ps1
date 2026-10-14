<#
Restart the running Recall (scheduled task RecallAutostart) so it serves the code on disk.

A release is only a merge into main: the owner works in the running app.py, which loaded its code
at start-up (docs/defects/shipped-but-not-restarted.md). This is the one safe way to restart it:
  - refuses while a recording is live (GET /api/recordings/active);
  - stops only the PID tree of the app.py that RecallAutostart launches, matched by the task's own
    executable path, never by image name (memory never-taskkill-by-image-name: a kill by image
    name once took down Recall and the MCP server). The tree holds the base interpreter child and
    the Telegram listener (memory venv-shim-doubles-process-list);
  - relaunches through the scheduled task and waits for /api/ready;
  - compares the running version with app/__init__.py in the task's working directory.

  powershell -NoProfile -File scripts/restart-recall.ps1          # restart
  powershell -NoProfile -File scripts/restart-recall.ps1 -Check   # compare versions only; exit 1 if they differ
Exit codes: 0 ok, 1 not ready or version mismatch, 2 refused (live recording, unreadable state, extra roots).
#>
param([switch]$Check, [string]$Base = 'http://127.0.0.1:5050', [int]$TimeoutSec = 240)
$ErrorActionPreference = 'Stop'

$action = (Get-ScheduledTask -TaskName RecallAutostart).Actions[0]
$appDir = $action.WorkingDirectory
$want = (Select-String -Path (Join-Path $appDir 'app\__init__.py') -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value

function Get-Json([string]$Path) {
    try { Invoke-RestMethod -Uri "$Base$Path" -TimeoutSec 5 } catch { $null }
}

$health = Get-Json '/api/health'
$have = if ($health) { $health.version } else { $null }
Write-Output "restart-recall: code $want in $appDir, running $(if ($have) { $have } else { 'nothing' })"
if ($Check) { exit [int]($have -ne $want) }

if ($have) {
    $rec = Get-Json '/api/recordings/active'
    if ($null -eq $rec) { Write-Output 'restart-recall: cannot read /api/recordings/active - not restarting'; exit 2 }
    if ($rec.active) { Write-Output "restart-recall: $($rec.count) recording(s) live - not restarting; run again after they end"; exit 2 }
}

$roots = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {
    $_.ExecutablePath -eq $action.Execute -and $_.CommandLine -match "\s$([regex]::Escape($action.Arguments))\s*$"
})
if ($roots.Count -gt 1) {
    Write-Output "restart-recall: $($roots.Count) app.py roots ($($roots.ProcessId -join ', ')) - look before stopping any"
    exit 2
}
foreach ($p in $roots) {
    & taskkill.exe /PID $p.ProcessId /T /F | Out-Null
    Write-Output "restart-recall: stopped PID tree $($p.ProcessId)"
}

# The port can be held by a copy that is not the task's: an old Recall.bat start (13.10.2026 the
# restart brought up a new task copy that never got the port, and the owner kept working on the
# old code). Stop the PID tree of whatever app.py still listens - never by image name.
Start-Sleep -Seconds 2
$port = ([uri]$Base).Port
$owner = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
if ($owner) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
    if ($parent -and $parent.CommandLine -match 'app\.py') { $p = $parent }   # venv shim -> base interpreter child
    if ($p.CommandLine -match 'app\.py') {
        & taskkill.exe /PID $p.ProcessId /T /F | Out-Null
        Write-Output "restart-recall: stopped another app.py holding port $port (PID tree $($p.ProcessId))"
    } else {
        Write-Output "restart-recall: port $port is held by PID $owner ($($p.Name)), not Recall - not touching it"
        exit 2
    }
}

Start-ScheduledTask -TaskName RecallAutostart
$deadline = (Get-Date).AddSeconds($TimeoutSec)
do {
    Start-Sleep -Seconds 2
    $ready = Get-Json '/api/ready'
} until (($ready -and $ready.ready) -or ((Get-Date) -gt $deadline))
if (-not ($ready -and $ready.ready)) { Write-Output "restart-recall: not ready after $TimeoutSec s - see whisper_ui.log"; exit 1 }

Write-Output "restart-recall: ready, version $($ready.version), telegram $($ready.checks.telegram)"
if ($ready.version -ne $want) { Write-Output "restart-recall: running $($ready.version), code says $want - check which branch $appDir is on"; exit 1 }
exit 0
