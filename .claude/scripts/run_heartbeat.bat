@echo off
REM Heartbeat runner for Windows Task Scheduler
REM Activates the UV environment, runs heartbeat, always logs a status line

cd /d "%~dp0"

uv run python heartbeat.py
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - Heartbeat completed >> heartbeat_runs.log
) else (
    echo %date% %time% - Heartbeat FAILED exit=%EXITCODE% >> heartbeat_runs.log
)

exit /b %EXITCODE%
