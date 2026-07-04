@echo off
REM Persona learning tick runner for Windows Task Scheduler
cd /d "%~dp0"
uv run python persona_learning_tick.py
set EXITCODE=%ERRORLEVEL%
if %EXITCODE% EQU 0 (
    echo %date% %time% - Persona learning tick completed >> persona_learning_runs.log
) else (
    echo %date% %time% - Persona learning tick FAILED exit=%EXITCODE% >> persona_learning_runs.log
)
exit /b %EXITCODE%
