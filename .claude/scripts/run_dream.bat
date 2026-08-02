@echo off
REM Dream-cycle runner for Windows Task Scheduler (nightly, ~3 AM) — issue #170.
REM Runs the 5-phase dream cycle (Orient/Gather/Consolidate/Prune/Belief-Evolve)
REM via UV, always logs a status line. No --force: DREAM_MIN_INTERVAL_HOURS
REM naturally dedupes against the Sunday-8PM memory_weekly.py-triggered run.

cd /d "%~dp0"

uv run python memory_dream.py
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - Dream cycle completed exit=%EXITCODE% >> dream_runs.log
) else (
    echo %date% %time% - Dream cycle returned exit=%EXITCODE% >> dream_runs.log
)

exit /b %EXITCODE%
