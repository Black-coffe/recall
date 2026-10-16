# Autostart of Recall at Windows logon (Wave 2 of the Telegram roadmap).
#
# Why. The Telegram listener is a child process of app.py, so a "24/7 listener" is impossible
# without app.py 24/7. On live data this cost 30 lost workdays over 8 months:
# the gaps started in the evening (14:30-19:05) and ended in the morning of the next
# workday - exactly when the machine was turned on. The catch-up at start
# (TELEGRAM_CATCHUP) picks up what was missed, the autostart removes the pause itself.
#
# Install:   .\scripts\install_autostart.ps1
# Remove:     .\scripts\install_autostart.ps1 -Remove
# Check:   Get-ScheduledTask -TaskName RecallAutostart

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
        Write-Output "Autostart removed: $TaskName"
    } else {
        Write-Output "Task $TaskName not found - nothing to remove"
    }
    return
}

# pythonw.exe - without a console window; if it is missing, python.exe remains.
if (-not (Test-Path $python)) {
    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $python)) {
    throw "Interpreter not found in .venv: $python"
}
if (-not (Test-Path $appPy)) {
    throw "app.py not found: $appPy"
}

$action = New-ScheduledTaskAction -Execute $python -Argument "app.py" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# RestartCount: if the process died - bring it back, do not wait for the next logon.
# StartWhenAvailable: a missed start (the machine was off) runs later.
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
    -Settings $settings -Description "Recall: Flask + Telegram listener at user logon" | Out-Null

Write-Output "Autostart installed: $TaskName"
Write-Output "  interpreter: $python"
Write-Output "  directory:   $projectRoot"
Write-Output ""
Write-Output "Check now: Start-ScheduledTask -TaskName $TaskName"
Write-Output "Remove:    .\scripts\install_autostart.ps1 -Remove"
