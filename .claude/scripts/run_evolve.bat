@echo off
REM Evolve-loop runner for Windows Task Scheduler (Living Self Act 4)
REM Runs the SAFE recall `propose` (no identity mutation) via UV, always logs a
REM status line. The belief rail (propose-belief) is Archon-driven, NOT this cron.

cd /d "%~dp0"

uv run python evolve/evolve_loop.py propose
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - Evolve propose completed exit=%EXITCODE% >> evolve_runs.log
) else (
    echo %date% %time% - Evolve propose returned exit=%EXITCODE% >> evolve_runs.log
)

exit /b %EXITCODE%
