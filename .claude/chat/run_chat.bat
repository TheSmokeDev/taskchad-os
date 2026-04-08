@echo off
REM Start The Homie Telegram bot.
REM Uses venv Python directly (uv run breaks log redirection).

set "SCRIPT_DIR=%~dp0"
set "SCRIPTS_DIR=%SCRIPT_DIR%..\scripts"
set "VENV_PYTHON=%SCRIPTS_DIR%\.venv\Scripts\python.exe"
set "LOG_FILE=%SCRIPT_DIR%bot.log"
set "PID_FILE=%SCRIPT_DIR%bot.pid"

if not exist "%VENV_PYTHON%" (
    echo Creating venv...
    cd /d "%SCRIPTS_DIR%" && uv sync
)

cd /d "%SCRIPTS_DIR%"

set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

if "%1"=="--fg" (
    "%VENV_PYTHON%" "%SCRIPT_DIR%main.py" --telegram %2 %3 %4
) else (
    start /b "" "%VENV_PYTHON%" "%SCRIPT_DIR%main.py" --telegram %* > "%LOG_FILE%" 2>&1
    echo Telegram bot started. Logs: %LOG_FILE%
)
