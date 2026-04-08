@echo off
REM Weekly synthesis runner for Windows Task Scheduler
REM Schedule: Sunday 20:00 (8 PM)

cd /d "%~dp0"

REM Run weekly synthesis using UV
uv run python memory_weekly.py

REM Log the run
echo %date% %time% - Weekly synthesis completed >> weekly_runs.log
