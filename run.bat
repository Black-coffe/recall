@echo off
REM Whisper UI v3.1.0 - Simple Run Script
echo Starting Whisper UI...
echo.

REM Check if virtual environment exists
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Virtual environment not found at .venv\Scripts\python.exe
    echo Please create virtual environment first
    pause
    exit /b 1
)

REM Run the application
.venv\Scripts\python.exe app.py

REM If app exits, pause to see error messages
if errorlevel 1 (
    echo.
    echo Application exited with error code %errorlevel%
    pause
)
