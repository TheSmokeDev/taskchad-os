@echo off
REM Weekly synthesis runner for Windows Task Scheduler
REM Schedule: Sunday 20:00 (8 PM)

cd /d "%~dp0"

REM Run weekly synthesis using UV
uv run python memory_weekly.py
set EXITCODE=%ERRORLEVEL%

REM Log the run AND propagate the exit code. Without the propagation Task
REM Scheduler recorded 0x0 for every crash, which is how ten straight weeks of
REM UnboundLocalError failures (2026-W27..W36) looked like clean successes.
if %EXITCODE% EQU 0 (
    echo %date% %time% - Weekly synthesis completed >> weekly_runs.log
) else (
    echo %date% %time% - Weekly synthesis FAILED exit=%EXITCODE% >> weekly_runs.log
)

exit /b %EXITCODE%
