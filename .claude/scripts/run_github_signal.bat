@echo off
REM GitHub Signal weekly digest runner for Windows Task Scheduler.
REM Starred backlog resurfacing + trending; silent-exits when nothing new.

cd /d "%~dp0"

uv run python -m github_signal.engine >> "%~dp0github_signal_runs.log" 2>&1
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - GitHub signal completed >> "%~dp0github_signal_runs.log"
) else (
    echo %date% %time% - GitHub signal FAILED exit=%EXITCODE% >> "%~dp0github_signal_runs.log"
)

exit /b %EXITCODE%
