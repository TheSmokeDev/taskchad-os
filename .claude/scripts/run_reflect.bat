@echo off
REM Reflection runner for Windows Task Scheduler
REM This script runs the daily reflection via UV

cd /d "%~dp0"

REM Run reflection using UV
uv run python memory_reflect.py

REM Log the run
echo %date% %time% - Reflection completed >> reflection_runs.log
