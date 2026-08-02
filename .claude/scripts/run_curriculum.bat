@echo off
REM Persona curriculum tick runner for Windows Task Scheduler
cd /d "%~dp0"
uv run python curriculum_tick.py
set EXITCODE=%ERRORLEVEL%
if %EXITCODE% EQU 0 (
    echo %date% %time% - Curriculum tick completed >> curriculum_runs.log
) else (
    echo %date% %time% - Curriculum tick FAILED exit=%EXITCODE% >> curriculum_runs.log
)
exit /b %EXITCODE%
