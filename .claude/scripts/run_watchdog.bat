@echo off
REM Bot watchdog runner for Windows Task Scheduler.
REM Polls the bot's /health endpoint once and restarts the bot if it is wedged.
REM Always logs a status line so a silently-failing watchdog is itself visible
REM (a monitor with no receipts is how the 6-week wedge stayed invisible).

cd /d "%~dp0"

uv run python bot_watchdog.py --once
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - Watchdog OK >> watchdog_runs.log
) else (
    echo %date% %time% - Watchdog found bot UNHEALTHY exit=%EXITCODE% >> watchdog_runs.log
)

exit /b %EXITCODE%
