@echo off
REM Coordinated GEO Authority runner for Windows Task Scheduler.
REM One task invokes this at 06:30 PT daily and 07:00 PT on Mon/Tue/Wed/Fri.
REM social.authority_cadence determines which work is due and is inert unless
REM AUTHORITY_ENGINE_ENABLED=true. It never auto-approves or auto-posts.

cd /d "%~dp0"

uv run python -m social.authority_cadence --mode auto
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - Authority cadence completed >> authority_cadence_runs.log
) else (
    echo %date% %time% - Authority cadence FAILED exit=%EXITCODE% >> authority_cadence_runs.log
)

exit /b %EXITCODE%
