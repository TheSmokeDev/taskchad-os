@echo off
REM Persona dream tick runner for Windows Task Scheduler (nightly, ~3:30 AM).
REM Fans the FULL 5-phase dream cycle out over every named profile by spawning
REM memory_dream.py -p <name> once each. Staggered 30 min after the main dream
REM (SecondBrain-Dream at 03:00) so the two never contend for the runtime.
REM No --force anywhere: each child keeps its own DREAM_MIN_INTERVAL_HOURS guard
REM and its own DREAM_SILENT fast path, so a persona with no signal costs zero.

cd /d "%~dp0"

REM -p default is the boot-shim's rank-1 force-default sentinel (personas/boot.py).
REM Without it, an operator's sticky ~/.homie/active_profile (rank 3) or an
REM inherited HOMIE_HOME (rank 2) silently makes run_tick's default-profile
REM guard refuse and the WHOLE fan-out no-ops for the night.
uv run python persona_dream_tick.py -p default
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - Persona dream tick completed exit=%EXITCODE% >> persona_dream_runs.log
) else (
    echo %date% %time% - Persona dream tick FAILED exit=%EXITCODE% >> persona_dream_runs.log
)

exit /b %EXITCODE%
