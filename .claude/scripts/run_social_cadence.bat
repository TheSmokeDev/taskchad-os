@echo off
REM Social cadence runner for Windows Task Scheduler
REM Runs the /social post cadence tick: auto-DRAFTS content for cadence-enabled
REM channels (LinkedIn by default) and dispatches ONLY operator-approved +
REM scheduled posts through the default-deny gate. It never auto-approves and
REM never auto-posts an unapproved draft. Gated by SOCIAL_CADENCE_ENABLED in .env
REM (the tick no-ops when that flag is false).

cd /d "%~dp0"

uv run python social/cadence.py
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - Social cadence completed >> social_cadence_runs.log
) else (
    echo %date% %time% - Social cadence FAILED exit=%EXITCODE% >> social_cadence_runs.log
)

exit /b %EXITCODE%
