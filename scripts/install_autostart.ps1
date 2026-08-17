# Автозапуск Recall при вході в Windows (Волна 2 роадмапу Telegram).
#
# Навіщо. Слухач Telegram — дочірній процес app.py, тож «слухач 24/7» неможливий
# без app.py 24/7. На живих даних це коштувало 30 втрачених буднів за 8 місяців:
# розриви починались увечері (14:30-19:05) і закінчувались уранці наступного
# робочого дня — тобто рівно тоді, коли вмикали машину. Догонка при старті
# (TELEGRAM_CATCHUP) добирає пропущене, автозапуск прибирає саму паузу.
#
# Встановити:   .\scripts\install_autostart.ps1
# Прибрати:     .\scripts\install_autostart.ps1 -Remove
# Перевірити:   Get-ScheduledTask -TaskName RecallAutostart

param(
    [switch]$Remove,
    [string]$TaskName = "RecallAutostart"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$appPy = Join-Path $projectRoot "app.py"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Автозапуск прибрано: $TaskName"
    } else {
        Write-Output "Завдання $TaskName не знайдено — нічого прибирати"
    }
    return
}

# pythonw.exe — без консольного вікна; якщо його немає, лишається python.exe.
if (-not (Test-Path $python)) {
    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $python)) {
    throw "Не знайдено інтерпретатор у .venv: $python"
}
if (-not (Test-Path $appPy)) {
    throw "Не знайдено app.py: $appPy"
}

$action = New-ScheduledTaskAction -Execute $python -Argument "app.py" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# RestartCount: якщо процес упав — підняти, а не чекати наступного входу.
# StartWhenAvailable: пропущений запуск (машина була вимкнена) відпрацює пізніше.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Recall: Flask + слухач Telegram при вході користувача" | Out-Null

Write-Output "Автозапуск встановлено: $TaskName"
Write-Output "  інтерпретатор: $python"
Write-Output "  каталог:       $projectRoot"
Write-Output ""
Write-Output "Перевірити зараз: Start-ScheduledTask -TaskName $TaskName"
Write-Output "Прибрати:         .\scripts\install_autostart.ps1 -Remove"
