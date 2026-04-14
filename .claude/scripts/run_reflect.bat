@echo off
REM Reflection runner for Windows Task Scheduler
REM Runs daily reflection via UV, always logs a status line

cd /d "%~dp0"

uv run python memory_reflect.py
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - Reflection completed >> reflection_runs.log
) else (
    echo %date% %time% - Reflection FAILED exit=%EXITCODE% >> reflection_runs.log
)

exit /b %EXITCODE%
