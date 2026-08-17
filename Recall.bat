@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Recall
cd /d "%~dp0"
rem Auto-cleanup: kill stale Recall instances (app.py / listener) before launch,
rem so a leftover process never holds port 5050 with old code ("Recall stopped").
echo Cleaning up stale Recall instances if any...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and ($_.CommandLine -like '*app.py*' -or $_.CommandLine -like '*telegram_listener.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 >nul
echo ============================================================
echo.
echo   RECALL is running.
echo.
echo   Open in browser:  http://localhost:5050
echo   (the browser opens automatically in a few seconds)
echo.
echo   To STOP Recall - just close this window.
echo   Detailed logs are written to files:
echo       whisper_ui.log       (application log)
echo       recall_console.log   (console output)
echo.
echo   You do NOT need to read this window - keep it minimized.
echo ============================================================
start "" /min powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 6; Start-Process 'http://localhost:5050'"
rem Send the noisy output to a file so the window stays calm.
".venv\Scripts\python.exe" app.py > "%~dp0recall_console.log" 2>&1
echo.
echo Recall stopped. If it closed unexpectedly, see recall_console.log
pause >nul
