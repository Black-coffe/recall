# backup_db.ps1 — ЩОДЕННИЙ легкий бекап SQLite БД Recall (whisper_history.db).
#
# Навіщо окремий скрипт від backup.ps1:
#   backup.ps1 — важкий тижневий прогін (recordings/uploads/db_backups/...).
#   Вікно втрати даних до 7 днів неприйнятне саме для БД — вона мала (десятки-
#   сотні МБ) і містить транскрипти/сутності/action items, які повторно не
#   отримати (на відміну від медіафайлів, які фізично лишаються на диску).
#   Тому БД бекапиться цим скриптом ЩОДНЯ (окремий Scheduled Task), а важкий
#   restic-прогін медіа лишається тижневим у backup.ps1.
#
# Консистентність:
#   Копіюється НЕ сирим файлом (Copy-Item живого .db поруч із -wal/-shm — у
#   WAL-режимі це може дати биту копію під активним записом), а через SQLite
#   backup-механізм VACUUM INTO — атомарний знімок на рівні самої БД, коректний
#   навіть якщо в цей момент триває запис. Виконується через
#   .venv\Scripts\python.exe (sqlite3 з stdlib) — сам python.exe гарантовано
#   є в проєкті, окремий sqlite3.exe в PATH на цій машині відсутній.
#
# Що робить:
#   1) VACUUM INTO -> db_backups\whisper_history-<stamp>.db (атомарний знімок).
#   2) PRAGMA integrity_check на щойно створеному знімку — якщо НЕ "ok",
#      знімок вважається битим, видаляється, скрипт завершується з помилкою
#      (краще без бекапу за цей день, ніж тихо зберегти биту копію).
#   3) Ротація локальних знімків: лишає $KeepLocal останніх (за замовчуванням 30
#      -> місяць щоденних знімків), решту видаляє.
#   4) (опційно, -OffsiteBackup, за замовчуванням увімкнено) Швидкий offsite
#      restic backup --tag daily ЛИШЕ по db_backups\ (не по важких медіа) у
#      той самий репозиторій, що й backup.ps1 (rest:http://127.0.0.1:8765/
#      через rclone serve restic -> gdrive:WhisperBackup/restic). Дедуплікація
#      restic робить це дешевим day-to-day (тільки дельта БД, не вся тека).
#      forget --keep-daily $KeepDailyOffsite --prune для ротації offsite-версій.
#      Якщо restic/rclone або DPAPI-пароль відсутні — offsite-крок ПРОПУСКАЄТЬСЯ
#      з попередженням у лог (локальний VACUUM-знімок все одно створюється).
#   5) Лог у logs\backup-db-YYYYMMDD-HHmmss.log.
#
# ProjectRoot параметризовано так само, як у backup.ps1: -ProjectRoot <path>
# або env RECALL_PROJECT_ROOT, за замовчуванням E:\Projects\Recall.
#
# Запуск вручну:      pwsh -NoProfile -File .\scripts\backup_db.ps1
# Тільки локально
# (без offsite):       pwsh -NoProfile -File .\scripts\backup_db.ps1 -OffsiteBackup:$false
# Scheduled Task (щодня, наприклад 03:15):
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
        throw "Live DB не знайдено: $LiveDb"
    }

    $pyExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $pyExe)) {
        throw ".venv\Scripts\python.exe не найден; не можу зробити VACUUM INTO"
    }

    # 1) Консистентний знімок через VACUUM INTO (атомарно, без блокувань,
    #    коректно навіть під активним WAL-записом).
    $DbSnap = Join-Path $DbBackupDir "whisper_history-$Stamp.db"
    Log "VACUUM INTO $DbSnap"
    $vacuumSql = "VACUUM INTO '$($DbSnap.Replace('\','/').Replace("'","''"))';"
    & $pyExe -c "import sqlite3,sys; c=sqlite3.connect(r'$LiveDb'); c.execute(sys.argv[1]); c.close()" $vacuumSql
    if ($LASTEXITCODE -ne 0) { throw "VACUUM INTO завершився з кодом $LASTEXITCODE" }
    if (-not (Test-Path $DbSnap)) { throw "VACUUM INTO не створив файл $DbSnap" }
    Log "DB snapshot size: $([math]::Round((Get-Item $DbSnap).Length/1MB,1)) MB"

    # 2) integrity_check на щойно створеному знімку — гарантія, що це не
    #    просто "файл існує", а валідна SQLite БД.
    Log "PRAGMA integrity_check на $DbSnap"
    $checkResult = & $pyExe -c "import sqlite3; c=sqlite3.connect(r'$DbSnap'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
    Log "integrity_check result: $checkResult"
    if ($checkResult -ne 'ok') {
        Remove-Item $DbSnap -Force -ErrorAction SilentlyContinue
        throw "integrity_check провалено ($checkResult) — биту копію видалено, бекап за цей запуск НЕ створено"
    }

    # 3) Ротація локальних знімків.
    $oldSnaps = Get-ChildItem $DbBackupDir -Filter 'whisper_history-*.db' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $KeepLocal
    foreach ($s in $oldSnaps) {
        Log "Cleanup old local DB snap: $($s.Name)"
        Remove-Item $s.FullName -Force -ErrorAction SilentlyContinue
    }

    # 4) Offsite (опційно) — легкий restic backup лише по db_backups\, тег daily.
    if (-not $OffsiteBackup) {
        Log "OffsiteBackup вимкнено параметром — локальний знімок готовий, offsite пропущено."
        Log "=== Recall DB backup OK (local-only) ==="
        exit 0
    }

    if ($env:PATH -notlike '*C:\bin*') { $env:PATH = "C:\bin;" + $env:PATH }
    $Restic = Resolve-ToolPath -Name 'restic' -PreferredFull 'C:\bin\restic.exe'
    $Rclone = Resolve-ToolPath -Name 'rclone' -PreferredFull 'C:\bin\rclone.exe'
    if (-not $Restic -or -not $Rclone -or -not (Test-Path $PasswordDpapiFile)) {
        Log "WARN: restic/rclone/DPAPI-пароль недоступні — offsite-крок пропущено. Локальний VACUUM-знімок у $DbBackupDir є."
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
    if (-not $alive) { throw "rclone serve не відповідає на 127.0.0.1:$RclonePort" }
    Log "rclone serve PID=$($rcloneProc.Id) ready"

    $resticOut = "$LogDir\restic-backup-db-$Stamp.out"
    $resticErr = "$LogDir\restic-backup-db-$Stamp.err"
    $resticArgs = @('-r',$ResticRepo,'backup','--tag','daily','--verbose=1',
        '--exclude','*.tmp','--exclude','*.partial', $DbBackupDir)
    Log "restic backup --tag daily (лише db_backups\, виведення -> $(Split-Path -Leaf $resticOut))"
    $proc = Start-Process -FilePath $Restic -ArgumentList $resticArgs `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $resticOut -RedirectStandardError $resticErr
    if (Test-Path $resticOut) { Get-Content $resticOut -Tail 20 | ForEach-Object { Log "[stdout] $_" } }
    if (Test-Path $resticErr) {
        $errLines = Get-Content $resticErr
        if ($errLines) { $errLines | Select-Object -Last 10 | ForEach-Object { Log "[stderr] $_" } }
    }
    if ($proc.ExitCode -ne 0) { throw "restic backup (daily) exited with code $($proc.ExitCode)" }

    Log "restic unlock (чистимо stale locks якщо є)"
    Start-Process -FilePath $Restic -ArgumentList @('-r',$ResticRepo,'unlock') `
        -NoNewWindow -Wait -RedirectStandardOutput "$LogDir\restic-unlock-db-$Stamp.out" `
        -RedirectStandardError "$LogDir\restic-unlock-db-$Stamp.err" | Out-Null

    # Ротація offsite daily-тегованих знімків. Тег --keep-tag тримає лише
    # daily-тег обмеженим, weekly-тег (з backup.ps1) не чіпаємо.
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
