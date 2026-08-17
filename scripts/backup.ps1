# backup.ps1 — еженедельный шифрованный бэкап Recall (медиа + вся директория)
# на Google Drive.
#
# Что делает:
#   1) Делает консистентный снимок SQLite БД (VACUUM INTO → отдельный файл).
#   2) restic backup всех релевантных путей (recordings, uploads, db_backups,
#      .env, meeting_archive) в gdrive:WhisperBackup/restic.
#   3) restic forget --keep-weekly 8 --keep-monthly 12 --prune (ротация).
#   4) Пишет лог в logs\backup-YYYYMMDD.log.
#
# ЧАСТОТА: цей скрипт — ВАЖКИЙ, тижневий бекап (media + все нижче ProjectRoot).
# БД коштовна (вікно втрати транскриптів до 7 днів неприйнятне) — тому окремо
# є scripts\backup_db.ps1, легкий і призначений для ЩОДЕННОГО scheduled task
# (VACUUM INTO snapshot локально + опційно швидкий offsite restic --tag daily
# лише по db_backups\, без важких медіа). Дивись коментарі на початку
# backup_db.ps1.
#
# Зависимости: C:\bin\restic.exe, C:\bin\rclone.exe, DPAPI-зашифрованный
# пароль в %LOCALAPPDATA%\WhisperBackup\repo.password.dpapi.
#
# Запускать вручную:  powershell -ExecutionPolicy Bypass -File .\backup.ps1
# Из scheduled task:  pwsh.exe или powershell.exe + те же аргументы.
#
# ProjectRoot параметризован: передай -ProjectRoot <path>, або виставь
# env RECALL_PROJECT_ROOT — за замовчуванням береться поточний шлях проєкту
# E:\Projects\Recall (раніше тут був хардкод застарілого шляху
# E:\Projects\Whisper — прибрано, непереносимо між профілями/машинами).
param(
    [string]$ProjectRoot = $(if ($env:RECALL_PROJECT_ROOT) { $env:RECALL_PROJECT_ROOT } else { 'E:\Projects\Recall' })
)

# НЕ ставимо 'Stop' глобально: restic у нормальному режимі ретраю
# часто пише warning'и в stderr. З 'Stop' PowerShell приймає це за fatal error
# і обриває скрипт на першому ж 500-у від Google, не давши restic-у відретраїти.
$ErrorActionPreference = 'Continue'
$LogDir      = "$ProjectRoot\logs"
$DbBackupDir = "$ProjectRoot\db_backups"
$LiveDb      = "$ProjectRoot\whisper_history.db"
$PasswordDpapiFile = "$env:LOCALAPPDATA\WhisperBackup\repo.password.dpapi"
$ResticRepo  = 'rest:http://127.0.0.1:8765/'  # rclone serve restic в окремому процесі
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
    throw "restic.exe не найден ни в C:\bin\, ни в PATH"
}

function Resolve-RclonePath {
    foreach ($p in @('C:\bin\rclone.exe', 'rclone')) {
        if (Get-Command $p -ErrorAction SilentlyContinue) { return $p }
    }
    throw "rclone.exe не найден ни в C:\bin\, ни в PATH"
}

try {
    Log "=== Whisper backup started ==="

    # restic шукає rclone у PATH — додамо C:\bin без перезапису.
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

    # 2a) Запускаємо rclone serve restic у фоновому процесі. REST-mode надійніший
    #     за --stdio: restic ↔ rclone не діляться пайпами parent-shell'а, тому
    #     pipe не закривається при моніторингу батьком (виявлено експериментально:
    #     stdio-варіант повис 0 байт за 8 хв під run_in_background).
    Log "Starting rclone serve restic on 127.0.0.1:$RclonePort"
    # tpslimit=10 + drive-pacer-min-sleep=10ms — особистий Google OAuth client_id
    # має квоту 1000 req/s, але навіть так тримаємо помірно (10 req/s з burst=20),
    # бо restic паралельно посилає ще й через паки. Раніше з shared client_id було
    # 5, ловили 403 Quota exceeded; з особистим client_id скейл цього не потребує.
    $rcloneArgs = @('serve','restic','--addr',"127.0.0.1:$RclonePort",
        '--cache-dir',"$env:TEMP\rclone-cache",
        '--tpslimit','10','--tpslimit-burst','20',
        '--drive-pacer-min-sleep','10ms','--drive-pacer-burst','200',
        $RcloneTarget)
    $rcloneProc = Start-Process -FilePath $Rclone -ArgumentList $rcloneArgs `
        -PassThru -NoNewWindow -RedirectStandardOutput "$LogDir\rclone-serve-$Stamp.log" `
        -RedirectStandardError "$LogDir\rclone-serve-$Stamp.err"

    # Чекаємо, поки порт відповідає (до 15с)
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

    # 2) Консистентный snapshot БД через SQLite VACUUM INTO (atomic, без блокировок).
    #    Без этого restic мог бы поймать половину WAL → битый файл при restore.
    if (Test-Path $LiveDb) {
        $DbSnap = Join-Path $DbBackupDir "whisper_history-$Stamp.db"
        Log "VACUUM INTO $DbSnap"
        $sqliteScript = "VACUUM INTO '$($DbSnap.Replace('\','/').Replace("'","''"))';"
        # Используем встроенный sqlite3 из .venv, если есть, иначе Python.
        $pyExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
        if (Test-Path $pyExe) {
            & $pyExe -c "import sqlite3,sys; c=sqlite3.connect(r'$LiveDb'); c.execute(sys.argv[1]); c.close()" $sqliteScript
        } else {
            throw ".venv\Scripts\python.exe не найден; не могу сделать VACUUM"
        }
        Log "DB snapshot size: $([math]::Round((Get-Item $DbSnap).Length/1MB,1)) MB"

        # Чистим старые DB-снэпшоты (>3 шт); restic сам хранит историю.
        Get-ChildItem $DbBackupDir -Filter 'whisper_history-*.db' |
            Sort-Object LastWriteTime -Descending |
            Select-Object -Skip 3 |
            ForEach-Object { Log "Cleanup old DB snap: $($_.Name)"; Remove-Item $_.FullName -Force }
    } else {
        Log "WARN: $LiveDb не существует, пропускаю VACUUM"
    }

    # 3) restic backup. Список путей; --exclude для шума.
    $BackupPaths = @(
        "$ProjectRoot\recordings",
        "$ProjectRoot\uploads",
        "$ProjectRoot\db_backups",
        "$ProjectRoot\.env",
        'E:\Projects\WORK\meeting_archive'
    ) | Where-Object { Test-Path $_ }

    Log "Paths to backup:"
    $BackupPaths | ForEach-Object { Log "  $_" }

    # Start-Process з редіректом у файли — PowerShell-пайп ловить stderr restic-а
    # як NativeCommandError і обриває скрипт на ретраї. Файлове перенаправлення —
    # robusno, перевіряємо тільки ExitCode.
    $resticOut = "$LogDir\restic-backup-$Stamp.out"
    $resticErr = "$LogDir\restic-backup-$Stamp.err"
    $resticArgs = @('-r',$ResticRepo,'backup','--tag','weekly','--verbose=1',
        '--exclude','*.tmp','--exclude','*.partial','--exclude','Thumbs.db') + $BackupPaths
    Log "restic backup (виведення → $(Split-Path -Leaf $resticOut))"
    $proc = Start-Process -FilePath $Restic -ArgumentList $resticArgs `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $resticOut -RedirectStandardError $resticErr
    if (Test-Path $resticOut) { Get-Content $resticOut -Tail 30 | ForEach-Object { Log "[stdout] $_" } }
    if (Test-Path $resticErr) {
        $errLines = Get-Content $resticErr
        if ($errLines) { $errLines | Select-Object -Last 10 | ForEach-Object { Log "[stderr] $_" } }
    }
    if ($proc.ExitCode -ne 0) { throw "restic backup exited with code $($proc.ExitCode)" }

    # 4a) Зняти stale locks (від прерваних попередніх запусків). Без цього
    #     forget+prune падає з exit 11 "repo already locked".
    Log "restic unlock (чистимо stale locks якщо є)"
    $unlockOut = "$LogDir\restic-unlock-$Stamp.out"
    Start-Process -FilePath $Restic -ArgumentList @('-r',$ResticRepo,'unlock') `
        -NoNewWindow -Wait -RedirectStandardOutput $unlockOut -RedirectStandardError "$LogDir\restic-unlock-$Stamp.err" | Out-Null
    if (Test-Path $unlockOut) { Get-Content $unlockOut | ForEach-Object { Log $_ } }

    # 4) Ротация: 8 еженедельных + 12 месячных снэпшотов + prune (физическое удаление).
    Log "--- forget + prune ---"
    $forgetOut = "$LogDir\restic-forget-$Stamp.out"
    $forgetProc = Start-Process -FilePath $Restic -ArgumentList @('-r',$ResticRepo,'forget',
        '--keep-weekly','8','--keep-monthly','12','--prune') `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $forgetOut -RedirectStandardError "$LogDir\restic-forget-$Stamp.err"
    if (Test-Path $forgetOut) { Get-Content $forgetOut | ForEach-Object { Log $_ } }
    if ($forgetProc.ExitCode -ne 0) { throw "restic forget/prune exited with code $($forgetProc.ExitCode)" }

    # 5) Краткая сводка
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
