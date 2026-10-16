# backup_db.ps1 - DAILY light backup of the Recall SQLite DB (whisper_history.db).
#
# Why a separate script from backup.ps1:
#   backup.ps1 - the heavy weekly run (recordings/uploads/db_backups/...).
#   A data-loss window of up to 7 days is unacceptable for the DB specifically - it is small (tens to
#   hundreds of MB) and holds transcripts/entities/action items that cannot be
#   obtained again (unlike media files, which physically stay on disk).
#   So the DB is backed up by this script DAILY (a separate Scheduled Task), and the heavy
#   restic run of the media stays weekly in backup.ps1.
#
# Consistency:
#   It is copied NOT as a raw file (Copy-Item of a live .db next to -wal/-shm - in
#   WAL mode this can give a broken copy under active writing), but via the SQLite
#   backup mechanism VACUUM INTO - an atomic snapshot at the level of the DB itself, correct
#   even if a write is in progress at that moment. Runs through
#   .venv\Scripts\python.exe (sqlite3 from the stdlib) - python.exe itself is guaranteed
#   to be in the project, a separate sqlite3.exe is absent from PATH on this machine.
#
# What it does:
#   1) VACUUM INTO -> db_backups\whisper_history-<stamp>.db (an atomic snapshot).
#   2) PRAGMA integrity_check on the freshly created snapshot - if NOT "ok",
#      the snapshot is considered broken, deleted, and the script ends with an error
#      (better no backup for that day than silently keeping a broken copy).
#   3) Rotation of local snapshots: keeps the last $KeepLocal (default 30
#      -> a month of daily snapshots), deletes the rest.
#   4) (optional, -OffsiteBackup, enabled by default) A fast offsite
#      restic backup --tag daily ONLY over db_backups\ (not the heavy media) into
#      the same repository as backup.ps1 (rest:http://127.0.0.1:8765/
#      via rclone serve restic -> gdrive:WhisperBackup/restic). restic deduplication
#      makes this cheap day-to-day (only the DB delta, not the whole folder).
#      forget --keep-daily $KeepDailyOffsite --prune rotates the offsite versions.
#      If restic/rclone or the DPAPI password is missing - the offsite step is SKIPPED
#      with a warning in the log (the local VACUUM snapshot is still created).
#   5) Log to logs\backup-db-YYYYMMDD-HHmmss.log.
#
# ProjectRoot is parameterized the same way as in backup.ps1: -ProjectRoot <path>
# or env RECALL_PROJECT_ROOT, by default E:\Projects\Recall.
#
# Run manually:      pwsh -NoProfile -File .\scripts\backup_db.ps1
# Local only
# (without offsite):       pwsh -NoProfile -File .\scripts\backup_db.ps1 -OffsiteBackup:$false
# Scheduled Task (daily, for example 03:15):
#   pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "E:\Projects\Recall\scripts\backup_db.ps1"

param(
    [string]$ProjectRoot = $(if ($env:RECALL_PROJECT_ROOT) { $env:RECALL_PROJECT_ROOT } else { 'E:\Projects\Recall' }),
    [int]$KeepLocal = 30,
    [int]$KeepDailyOffsite = 14,
    [bool]$OffsiteBackup = $true
)

$ErrorActionPreference = 'Continue'
$LogDir      = "$ProjectRoot\logs"
$DbBackupDir = "$ProjectRoot\db_backups"
$LiveDb      = "$ProjectRoot\whisper_history.db"
$PasswordDpapiFile = "$env:LOCALAPPDATA\WhisperBackup\repo.password.dpapi"
$ResticRepo  = 'rest:http://127.0.0.1:8765/'
$RclonePort  = 8765
$RcloneTarget = 'gdrive:WhisperBackup/restic'

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory $LogDir | Out-Null }
if (-not (Test-Path $DbBackupDir)) { New-Item -ItemType Directory $DbBackupDir | Out-Null }
$LogFile = Join-Path $LogDir "backup-db-$Stamp.log"

function Log {
    param([string]$msg)
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

function Resolve-ToolPath {
    param([string]$Name, [string]$PreferredFull)
    foreach ($p in @($PreferredFull, $Name)) {
        if ($p -and (Get-Command $p -ErrorAction SilentlyContinue)) { return $p }
    }
    return $null
}

$rcloneProc = $null
try {
    Log "=== Recall DB backup started (ProjectRoot=$ProjectRoot) ==="

    if (-not (Test-Path $LiveDb)) {
        throw "Live DB not found: $LiveDb"
    }

    $pyExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $pyExe)) {
        throw ".venv\Scripts\python.exe not found; cannot do VACUUM INTO"
    }

    # 1) A consistent snapshot via VACUUM INTO (atomic, no locks,
    #    correct even under an active WAL write).
    $DbSnap = Join-Path $DbBackupDir "whisper_history-$Stamp.db"
    Log "VACUUM INTO $DbSnap"
    $vacuumSql = "VACUUM INTO '$($DbSnap.Replace('\','/').Replace("'","''"))';"
    & $pyExe -c "import sqlite3,sys; c=sqlite3.connect(r'$LiveDb'); c.execute(sys.argv[1]); c.close()" $vacuumSql
    if ($LASTEXITCODE -ne 0) { throw "VACUUM INTO finished with code $LASTEXITCODE" }
    if (-not (Test-Path $DbSnap)) { throw "VACUUM INTO did not create the file $DbSnap" }
    Log "DB snapshot size: $([math]::Round((Get-Item $DbSnap).Length/1MB,1)) MB"

    # 2) integrity_check on the freshly created snapshot - a guarantee that this is not
    #    just "the file exists" but a valid SQLite DB.
    Log "PRAGMA integrity_check on $DbSnap"
    $checkResult = & $pyExe -c "import sqlite3; c=sqlite3.connect(r'$DbSnap'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
    Log "integrity_check result: $checkResult"
    if ($checkResult -ne 'ok') {
        Remove-Item $DbSnap -Force -ErrorAction SilentlyContinue
        throw "integrity_check failed ($checkResult) - the broken copy was deleted, NO backup was created for this run"
    }

    # 3) Rotation of local snapshots.
    $oldSnaps = Get-ChildItem $DbBackupDir -Filter 'whisper_history-*.db' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $KeepLocal
    foreach ($s in $oldSnaps) {
        Log "Cleanup old local DB snap: $($s.Name)"
        Remove-Item $s.FullName -Force -ErrorAction SilentlyContinue
    }

    # 4) Offsite (optional) - a light restic backup only over db_backups\, tag daily.
    if (-not $OffsiteBackup) {
        Log "OffsiteBackup disabled by the parameter - the local snapshot is ready, offsite skipped."
        Log "=== Recall DB backup OK (local-only) ==="
        exit 0
    }

    if ($env:PATH -notlike '*C:\bin*') { $env:PATH = "C:\bin;" + $env:PATH }
    $Restic = Resolve-ToolPath -Name 'restic' -PreferredFull 'C:\bin\restic.exe'
    $Rclone = Resolve-ToolPath -Name 'rclone' -PreferredFull 'C:\bin\rclone.exe'
    if (-not $Restic -or -not $Rclone -or -not (Test-Path $PasswordDpapiFile)) {
        Log "WARN: restic/rclone/DPAPI password unavailable - offsite step skipped. The local VACUUM snapshot is in $DbBackupDir."
        Log "=== Recall DB backup OK (local-only, offsite skipped) ==="
        exit 0
    }
    Log "restic: $Restic"
    Log "rclone: $Rclone"

    $secure = Get-Content $PasswordDpapiFile | ConvertTo-SecureString
    $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $env:RESTIC_PASSWORD = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) | Out-Null

    Log "Starting rclone serve restic on 127.0.0.1:$RclonePort"
    $rcloneArgs = @('serve','restic','--addr',"127.0.0.1:$RclonePort",
        '--cache-dir',"$env:TEMP\rclone-cache-db",
        '--tpslimit','10','--tpslimit-burst','20',
        '--drive-pacer-min-sleep','10ms','--drive-pacer-burst','200',
        $RcloneTarget)
    $rcloneProc = Start-Process -FilePath $Rclone -ArgumentList $rcloneArgs `
        -PassThru -NoNewWindow -RedirectStandardOutput "$LogDir\rclone-serve-db-$Stamp.log" `
        -RedirectStandardError "$LogDir\rclone-serve-db-$Stamp.err"

    $alive = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $tc = New-Object System.Net.Sockets.TcpClient
            $tc.Connect('127.0.0.1', $RclonePort)
            $tc.Close()
            $alive = $true; break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $alive) { throw "rclone serve is not responding on 127.0.0.1:$RclonePort" }
    Log "rclone serve PID=$($rcloneProc.Id) ready"

    $resticOut = "$LogDir\restic-backup-db-$Stamp.out"
    $resticErr = "$LogDir\restic-backup-db-$Stamp.err"
    $resticArgs = @('-r',$ResticRepo,'backup','--tag','daily','--verbose=1',
        '--exclude','*.tmp','--exclude','*.partial', $DbBackupDir)
    Log "restic backup --tag daily (only db_backups\, output -> $(Split-Path -Leaf $resticOut))"
    $proc = Start-Process -FilePath $Restic -ArgumentList $resticArgs `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $resticOut -RedirectStandardError $resticErr
    if (Test-Path $resticOut) { Get-Content $resticOut -Tail 20 | ForEach-Object { Log "[stdout] $_" } }
    if (Test-Path $resticErr) {
        $errLines = Get-Content $resticErr
        if ($errLines) { $errLines | Select-Object -Last 10 | ForEach-Object { Log "[stderr] $_" } }
    }
    if ($proc.ExitCode -ne 0) { throw "restic backup (daily) exited with code $($proc.ExitCode)" }

    Log "restic unlock (cleaning stale locks if any)"
    Start-Process -FilePath $Restic -ArgumentList @('-r',$ResticRepo,'unlock') `
        -NoNewWindow -Wait -RedirectStandardOutput "$LogDir\restic-unlock-db-$Stamp.out" `
        -RedirectStandardError "$LogDir\restic-unlock-db-$Stamp.err" | Out-Null

    # Rotation of offsite daily-tagged snapshots. The --keep-tag keeps only the
    # daily tag bounded, the weekly tag (from backup.ps1) is not touched.
    Log "--- forget (daily) + prune ---"
    $forgetOut = "$LogDir\restic-forget-db-$Stamp.out"
    $forgetProc = Start-Process -FilePath $Restic -ArgumentList @('-r',$ResticRepo,'forget',
        '--tag','daily','--keep-daily',"$KeepDailyOffsite",'--prune') `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $forgetOut -RedirectStandardError "$LogDir\restic-forget-db-$Stamp.err"
    if (Test-Path $forgetOut) { Get-Content $forgetOut | ForEach-Object { Log $_ } }
    if ($forgetProc.ExitCode -ne 0) { throw "restic forget/prune (daily) exited with code $($forgetProc.ExitCode)" }

    Log "=== Recall DB backup OK (local + offsite) ==="
}
catch {
    Log "ERROR: $($_.Exception.Message)"
    Log $_.ScriptStackTrace
    exit 1
}
finally {
    $env:RESTIC_PASSWORD = $null
    if ($rcloneProc -and -not $rcloneProc.HasExited) {
        Log "Stopping rclone serve (PID=$($rcloneProc.Id))"
        Stop-Process -Id $rcloneProc.Id -Force -ErrorAction SilentlyContinue
    }
}
