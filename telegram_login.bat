@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Telegram login for Recall
cd /d "%~dp0"
echo ============================================================
echo   Telegram login for Recall (one-time, via QR)
echo.
echo   1) A QR image will POP UP on the screen
echo   2) On phone: Telegram - Settings - Devices -
echo      - Link Desktop Device  (camera opens)
echo   3) Point the phone camera at the QR image
echo   4) If asked, TYPE your 2FA cloud password here + Enter
echo ============================================================
echo.
".venv\Scripts\python.exe" telegram_login.py
echo.
pause
