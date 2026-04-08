@echo off
REM Heartbeat runner for Windows Task Scheduler
REM This script activates the UV environment and runs the heartbeat

cd /d "%~dp0"

REM Run heartbeat using UV
uv run python heartbeat.py

REM Log the run
echo %date% %time% - Heartbeat completed >> heartbeat_runs.log
