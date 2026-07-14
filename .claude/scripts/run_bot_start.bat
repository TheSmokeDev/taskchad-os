@echo off
REM At-logon bot starter for Windows Task Scheduler.
REM The watchdog only RECOVERS a dead bot; nothing STARTED one after a reboot
REM until this task existed — on 2026-07-14 a 6:19 AM reboot orphaned the bot
REM for a whole morning. Runs run_chat.sh (never run_chat.bat: it hardcodes
REM --telegram) via Git Bash EXPLICITLY — PATH bash under Task Scheduler is
REM WSL's System32 shim, which mangles Windows-path scripts (the same trap
REM that broke every watchdog restart on 2026-07-14).

cd /d "%~dp0..\chat"

set "GITBASH=C:\Program Files\Git\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=C:\Program Files (x86)\Git\bin\bash.exe"
if not exist "%GITBASH%" (
    echo %date% %time% - Bot start FAILED: Git Bash not found >> "%~dp0bot_start_runs.log"
    exit /b 1
)

"%GITBASH%" run_chat.sh
set EXITCODE=%ERRORLEVEL%

echo %date% %time% - Bot start at logon exit=%EXITCODE% >> "%~dp0bot_start_runs.log"
exit /b %EXITCODE%
