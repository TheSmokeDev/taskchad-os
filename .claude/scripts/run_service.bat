@echo off
REM Start The Homie bot service wrapper
cd /d "%~dp0"
uv run python service.py
