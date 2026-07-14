@echo off
REM Daily LinkedIn authority + network-growth packet.
REM Draft-only: writes a local packet and toast; never opens a browser or writes to LinkedIn.

cd /d "%~dp0"

uv run python -m social.linkedin_growth
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% EQU 0 (
    echo %date% %time% - LinkedIn growth packet completed >> linkedin_growth_runs.log
) else (
    echo %date% %time% - LinkedIn growth packet FAILED exit=%EXITCODE% >> linkedin_growth_runs.log
)

exit /b %EXITCODE%
