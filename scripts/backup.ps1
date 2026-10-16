# backup.ps1 - weekly encrypted Recall backup (media + the whole directory)
# to Google Drive.
#
# What it does:
#   1) Makes a consistent snapshot of the SQLite DB (VACUUM INTO -> a separate file).
#   2) restic backup of all relevant paths (recordings, uploads, db_backups,
#      .env, meeting_archive) to gdrive:WhisperBackup/restic.
#   3) restic forget --keep-weekly 8 --keep-monthly 12 --prune (rotation).
#   4) Writes a log to logs\backup-YYYYMMDD.log.
#
# FREQUENCY: this script is the HEAVY, weekly backup (media + everything under ProjectRoot).
# The DB is valuable (a window of up to 7 days of lost transcripts is unacceptable) - so there is
# a separate scripts\backup_db.ps1, light and meant for a DAILY scheduled task
# (a local VACUUM INTO snapshot + an optional fast offsite restic --tag daily
# only over db_backups\, without the heavy media). See the comments at the start of
# backup_db.ps1.
#
# Dependencies: C:\bin\restic.exe, C:\bin\rclone.exe, a DPAPI-encrypted
# password in %LOCALAPPDATA%\WhisperBackup\repo.password.dpapi.
#
# Run manually:  powershell -ExecutionPolicy Bypass -File .\backup.ps1
# From a scheduled task:  pwsh.exe or powershell.exe + the same arguments.
#
# ProjectRoot is parameterized: pass -ProjectRoot <path>, or set the
# env RECALL_PROJECT_ROOT - by default the current project path is used,
# E:\Projects\Recall (a hardcode of an outdated path used to be here,
# E:\Projects\Whisper - removed, not portable between profiles/machines).
param(
    [string]$ProjectRoot = $(if ($env:RECALL_PROJECT_ROOT) { $env:RECALL_PROJECT_ROOT } else { 'E:\Projects\Recall' })
)

# We do NOT set 'Stop' globally: restic in normal operation with retries
# often writes warnings to stderr. With 'Stop' PowerShell takes this for a fatal error
# and aborts the script on the very first 500 from Google, not letting restic retry.
$ErrorActionPreference = 'Continue'
$LogDir      = "$ProjectRoot\logs"
$DbBackupDir = "$ProjectRoot\db_backups"
$LiveDb      = "$ProjectRoot\whisper_history.db"
$PasswordDpapiFile = "$env:LOCALAPPDATA\WhisperBackup\repo.password.dpapi"
$ResticRepo  = 'rest:http://127.0.0.1:8765/'  # rclone serve restic in a separate process
$RclonePort  = 8765
$RcloneTarget = 'gdrive:WhisperBackup/restic'

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory $LogDir | Out-Null }
$LogFile = Join-Path $LogDir "backup-$Stamp.log"

function Log {
    param([string]$msg)
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

function Resolve-ResticPath {
    foreach ($p in @('C:\bin\restic.exe', 'restic')) {
        if (Get-Command $p -ErrorAction SilentlyContinue) { return $p }
    }
    throw "restic.exe not found in C:\bin\ or in PATH"
}

function Resolve-RclonePath {
    foreach ($p in @('C:\bin\rclone.exe', 'rclone')) {
        if (Get-Command $p -ErrorAction SilentlyContinue) { return $p }
    }
    throw "rclone.exe not found in C:\bin\ or in PATH"
}

try {
    Log "=== Whisper backup started ==="

    # restic looks for rclone in PATH - we add C:\bin without overwriting.
    if ($env:PATH -notlike '*C:\bin*') { $env:PATH = "C:\bin;" + $env:PATH }
    $Restic = Resolve-ResticPath
    $Rclone = Resolve-RclonePath
    Log "restic: $Restic"
    Log "rclone: $Rclone"

    # 1) Read DPAPI password into $env:RESTIC_PASSWORD
    if (-not (Test-Path $PasswordDpapiFile)) {
        throw "DPAPI password file not found: $PasswordDpapiFile"
    }
    $secure = Get-Content $PasswordDpapiFile | ConvertTo-SecureString
    $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $env:RESTIC_PASSWORD = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) | Out-Null

    # 2a) Start rclone serve restic in a background process. REST mode is more reliable
    #     than --stdio: restic <-> rclone do not share the parent shell's pipes, so the
    #     pipe is not closed when monitored by the parent (found experimentally:
    #     the stdio variant hung with 0 bytes for 8 min under run_in_background).
    Log "Starting rclone serve restic on 127.0.0.1:$RclonePort"
    # tpslimit=10 + drive-pacer-min-sleep=10ms - a personal Google OAuth client_id
    # has a quota of 1000 req/s, but even so we stay moderate (10 req/s with burst=20),
    # because restic also sends in parallel through the packs. Earlier, with a shared client_id it was
    # 5, and we hit 403 Quota exceeded; with a personal client_id scale does not need it.
    $rcloneArgs = @('serve','restic','--addr',"127.0.0.1:$RclonePort",
        '--cache-dir',"$env:TEMP\rclone-cache",
        '--tpslimit','10','--tpslimit-burst','20',
        '--drive-pacer-min-sleep','10ms','--drive-pacer-burst','200',
        $RcloneTarget)
    $rcloneProc = Start-Process -FilePath $Rclone -ArgumentList $rcloneArgs `
        -PassThru -NoNewWindow -RedirectStandardOutput "$LogDir\rclone-serve-$Stamp.log" `
        -RedirectStandardError "$LogDir\rclone-serve-$Stamp.err"

    # Wait until the port responds (up to 15s)
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

    # 2) Consistent DB snapshot via SQLite VACUUM INTO (atomic, no locks).
    #    Without it restic could catch half of the WAL -> a broken file on restore.
    if (Test-Path $LiveDb) {
        $DbSnap = Join-Path $DbBackupDir "whisper_history-$Stamp.db"
        Log "VACUUM INTO $DbSnap"
        $sqliteScript = "VACUUM INTO '$($DbSnap.Replace('\','/').Replace("'","''"))';"
        # Use the built-in sqlite3 from .venv if present, otherwise Python.
        $pyExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
        if (Test-Path $pyExe) {
            & $pyExe -c "import sqlite3,sys; c=sqlite3.connect(r'$LiveDb'); c.execute(sys.argv[1]); c.close()" $sqliteScript
        } else {
            throw ".venv\Scripts\python.exe not found; cannot do VACUUM"
        }
        Log "DB snapshot size: $([math]::Round((Get-Item $DbSnap).Length/1MB,1)) MB"

        # Clean old DB snapshots (>3); restic keeps the history itself.
        Get-ChildItem $DbBackupDir -Filter 'whisper_history-*.db' |
            Sort-Object LastWriteTime -Descending |
            Select-Object -Skip 3 |
            ForEach-Object { Log "Cleanup old DB snap: $($_.Name)"; Remove-Item $_.FullName -Force }
    } else {
        Log "WARN: $LiveDb does not exist, skipping VACUUM"
    }

    # 3) restic backup. A list of paths; --exclude for noise.
    $BackupPaths = @(
        "$ProjectRoot\recordings",
        "$ProjectRoot\uploads",
        "$ProjectRoot\db_backups",
        "$ProjectRoot\.env",
        'E:\Projects\WORK\meeting_archive'
    ) | Where-Object { Test-Path $_ }

    Log "Paths to backup:"
    $BackupPaths | ForEach-Object { Log "  $_" }

    # Start-Process with redirection to files - a PowerShell pipe catches restic's stderr
    # as NativeCommandError and aborts the script on a retry. File redirection is
    # robust, we check only the ExitCode.
    $resticOut = "$LogDir\restic-backup-$Stamp.out"
    $resticErr = "$LogDir\restic-backup-$Stamp.err"
    $resticArgs = @('-r',$ResticRepo,'backup','--tag','weekly','--verbose=1',
        '--exclude','*.tmp','--exclude','*.partial','--exclude','Thumbs.db') + $BackupPaths
    Log "restic backup (output -> $(Split-Path -Leaf $resticOut))"
    $proc = Start-Process -FilePath $Restic -ArgumentList $resticArgs `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $resticOut -RedirectStandardError $resticErr
    if (Test-Path $resticOut) { Get-Content $resticOut -Tail 30 | ForEach-Object { Log "[stdout] $_" } }
    if (Test-Path $resticErr) {
        $errLines = Get-Content $resticErr
        if ($errLines) { $errLines | Select-Object -Last 10 | ForEach-Object { Log "[stderr] $_" } }
    }
    if ($proc.ExitCode -ne 0) { throw "restic backup exited with code $($proc.ExitCode)" }

    # 4a) Remove stale locks (from interrupted earlier runs). Without this
    #     forget+prune fails with exit 11 "repo already locked".
    Log "restic unlock (cleaning stale locks if any)"
    $unlockOut = "$LogDir\restic-unlock-$Stamp.out"
    Start-Process -FilePath $Restic -ArgumentList @('-r',$ResticRepo,'unlock') `
        -NoNewWindow -Wait -RedirectStandardOutput $unlockOut -RedirectStandardError "$LogDir\restic-unlock-$Stamp.err" | Out-Null
    if (Test-Path $unlockOut) { Get-Content $unlockOut | ForEach-Object { Log $_ } }

    # 4) Rotation: 8 weekly + 12 monthly snapshots + prune (physical deletion).
    Log "--- forget + prune ---"
    $forgetOut = "$LogDir\restic-forget-$Stamp.out"
    $forgetProc = Start-Process -FilePath $Restic -ArgumentList @('-r',$ResticRepo,'forget',
        '--keep-weekly','8','--keep-monthly','12','--prune') `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $forgetOut -RedirectStandardError "$LogDir\restic-forget-$Stamp.err"
    if (Test-Path $forgetOut) { Get-Content $forgetOut | ForEach-Object { Log $_ } }
    if ($forgetProc.ExitCode -ne 0) { throw "restic forget/prune exited with code $($forgetProc.ExitCode)" }

    # 5) Short summary
    Log "--- snapshots ---"
    $snapOut = "$LogDir\restic-snapshots-$Stamp.out"
    Start-Process -FilePath $Restic -ArgumentList @('-r',$ResticRepo,'snapshots','--compact') `
        -NoNewWindow -Wait -RedirectStandardOutput $snapOut -RedirectStandardError "$LogDir\restic-snapshots-$Stamp.err" | Out-Null
    if (Test-Path $snapOut) { Get-Content $snapOut | ForEach-Object { Log $_ } }

    Log "=== Whisper backup OK ==="
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
