@echo off
chcp 65001 >nul
title Recall
cd /d "%~dp0"
rem One launch path. Recall runs in the background as the scheduled task RecallAutostart;
rem this button only opens it, starting the task when Recall is not up. It never starts its own
rem python app.py and never kills processes: a second copy held port 5050 with old code after a
rem release (13.10.2026), and killing app.py blindly would cut a live recording. Restart after a
rem release: powershell -NoProfile -File scripts\restart-recall.ps1
powershell -NoProfile -Command "try { Invoke-RestMethod http://127.0.0.1:5050/api/health -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 goto open
echo Starting Recall in the background (scheduled task RecallAutostart)...
powershell -NoProfile -Command "Start-ScheduledTask -TaskName RecallAutostart; $d=(Get-Date).AddSeconds(240); while ((Get-Date) -lt $d) { try { Invoke-RestMethod http://127.0.0.1:5050/api/health -TimeoutSec 3 | Out-Null; exit 0 } catch { Start-Sleep -Seconds 2 } }; exit 1"
if errorlevel 1 (
    echo Recall did not answer within 4 minutes - see whisper_ui.log
    pause
    exit /b 1
)
:open
start "" http://localhost:5050
