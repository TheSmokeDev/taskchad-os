@echo off
REM Start The Homie Telegram bot.
REM Uses venv Python directly (uv run breaks log redirection).
REM
REM PRP-7c Phase 3: pid path / log dir resolved through personas.services so
REM the script follows the active profile.

REM F2 — enable delayed expansion BEFORE the parenthesized FOR loop so the
REM ``!_LINE!`` reads inside the block resolve to the per-iteration value
REM rather than the literal ``!_LINE!``. Without this, PID_FILE / LOG_DIR
REM stay empty and the resolver output is dropped on the floor.
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPTS_DIR=%SCRIPT_DIR%..\scripts"
set "VENV_PYTHON=%SCRIPTS_DIR%\.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo Creating venv...
    REM Issue #34 — `^&^&` inside a parenthesized block makes cd receive the
    REM literal `&& uv sync` ("The system cannot find the path specified"),
    REM so the venv bootstrap never ran. Unescaped && works inside parens.
    cd /d "%SCRIPTS_DIR%" && uv sync
)

set "PYTHONPATH=%SCRIPTS_DIR%;%PYTHONPATH%"

REM F1 (R2) — pre-parse --profile/-p/--profile=NAME from the wrapper's argv
REM and set HOMIE_HOME BEFORE the resolver subprocess runs. Without this the
REM resolver's `python -c` invocation has its own argv, so apply_persona_override()
REM inside that subprocess never sees the wrapper's --profile flag — it would
REM resolve DEFAULT-profile paths while the bot itself (launched at the bottom)
REM DOES see the flag and switches to the named profile.
REM
REM We MUST NOT use ``shift`` here because the rest of the batch references
REM %1 (the --fg arm) and %* (the bot launch line). ``shift`` would
REM permanently consume those args. Instead we run the parse loop inside a
REM CALL to a subroutine that uses its own argv frame — exits leave %1..%9
REM and %* in the parent intact.
set "_HOMIE_PROFILE_OVERRIDE="
call :_homie_parse_profile %*

REM Issue #34 — --dry-run parse. Separate subroutine (NOT folded into the
REM profile loop) because :_homie_parse_profile returns on the FIRST
REM --profile match — `--profile X --dry-run` must detect both flags.
set "_HOMIE_DRY_RUN="
call :_homie_parse_dryrun %*

if not "!_HOMIE_PROFILE_OVERRIDE!"=="" (
    if /i "!_HOMIE_PROFILE_OVERRIDE!"=="default" (
        set "HOMIE_HOME="
    ) else if "!_HOMIE_PROFILE_OVERRIDE!"=="-" (
        set "HOMIE_HOME="
    ) else (
        set "_HOMIE_TARGET=%USERPROFILE%\.homie\profiles\!_HOMIE_PROFILE_OVERRIDE!"
        if not exist "!_HOMIE_TARGET!" (
            echo ERROR: Profile '!_HOMIE_PROFILE_OVERRIDE!' not found at !_HOMIE_TARGET! 1>^&2
            echo   Create it via: thehomie profile create !_HOMIE_PROFILE_OVERRIDE! 1>^&2
            exit /b 1
        )
        set "HOMIE_HOME=!_HOMIE_TARGET!"
    )
)
goto _homie_parse_done

:_homie_parse_profile
REM Subroutine — walk %1..%n in our own argv frame. Sets the parent's
REM _HOMIE_PROFILE_OVERRIDE via delayed expansion, then returns.
if "%~1"=="" goto :eof
set "_HOMIE_ARG=%~1"
if /i "%_HOMIE_ARG%"=="--profile" (
    set "_HOMIE_PROFILE_OVERRIDE=%~2"
    goto :eof
)
if /i "%_HOMIE_ARG%"=="-p" (
    set "_HOMIE_PROFILE_OVERRIDE=%~2"
    goto :eof
)
REM Match --profile=NAME (no space). Strip the prefix.
if /i "%_HOMIE_ARG:~0,10%"=="--profile=" (
    set "_HOMIE_PROFILE_OVERRIDE=%_HOMIE_ARG:~10%"
    goto :eof
)
shift
goto _homie_parse_profile

:_homie_parse_dryrun
REM Issue #34 — scan our own argv frame for --dry-run; sets the parent's
REM _HOMIE_DRY_RUN. `shift` here cannot consume the parent's %1/%* because
REM this runs inside a CALL'd subroutine frame.
if "%~1"=="" goto :eof
if /i "%~1"=="--dry-run" (
    set "_HOMIE_DRY_RUN=1"
    goto :eof
)
shift
goto _homie_parse_dryrun

:_homie_parse_done

REM Resolve profile-aware paths via personas.services. Single python -c call
REM emits two newline-separated paths. We parse with FOR /F "delims=" to
REM preserve spaces and Windows path separators.
REM
REM F1 (R3) — forward the wrapper's argv (%*) to the subprocess so
REM apply_persona_override() can pre-parse rank-1 (CLI flag) symmetrically
REM with the bot launch. Without this, the resolver subprocess sees
REM sys.argv=['-c'] and falls through to rank-3 (sticky
REM ~/.homie/active_profile), so `run_chat.bat --profile default` resolves
REM sticky-sales paths while the actual bot launch correctly forces default.
REM Forwarding %* closes the asymmetry — both resolver and bot see the same
REM flag.
set "_TMPFILE=%TEMP%\homie-paths-%RANDOM%.txt"
"%VENV_PYTHON%" -c "import sys; sys.path.insert(0, r'%SCRIPTS_DIR%'); from personas import apply_persona_override; apply_persona_override(); from personas.services import get_bot_pid_path, get_log_dir; print(get_bot_pid_path()); print(get_log_dir())" %* > "%_TMPFILE%" 2>nul

set "PID_FILE="
set "LOG_DIR="
set "_LINE=1"
for /f "usebackq delims=" %%P in ("%_TMPFILE%") do (
    if "!_LINE!"=="1" set "PID_FILE=%%P"
    if "!_LINE!"=="2" set "LOG_DIR=%%P"
    set /a _LINE=!_LINE!+1
)
del "%_TMPFILE%" >nul 2>&1

REM F4 — fail loudly if the service resolver could not run. The hardcoded
REM install-dir fallback paths were removed (they ship the wrong location
REM for named profiles and silently corrupt the default profile's PID
REM file). Better to fail fast.
REM cmd.exe parens MUST be escaped inside echo lines that live INSIDE another
REM parenthesized block, otherwise the parser closes the IF block early and
REM the next plain text gets read as a command (e.g. "PYTHONPATH was
REM unexpected at this time."). Use ^( and ^) for literal parentheses.
if "!PID_FILE!"=="" (
    echo ERROR: Service resolver failed -- Phase 3 helper unreachable. 1>^&2
    echo   Could not resolve bot pid path / log dir via personas.services. 1>^&2
    echo   Check %SCRIPTS_DIR%\.venv ^(uv sync^), PYTHONPATH, and that 1>^&2
    echo   personas.services is importable. Re-run after fixing. 1>^&2
    exit /b 1
)
if "!LOG_DIR!"=="" (
    echo ERROR: Service resolver failed -- Phase 3 helper unreachable. 1>^&2
    echo   Could not resolve log dir via personas.services. 1>^&2
    exit /b 1
)
set "LOG_FILE=!LOG_DIR!\bot.log"

REM Issue #34 — DRY RUN exit. MUST stay ABOVE the `start /b` spawn so a
REM dry run can never fork the bot or acquire main.py's global mutex.
REM `exit /b 0` (implicit endlocal) — a bare `exit 0` would kill the parent
REM cmd of an interactive operator. Delayed-expansion reads only: a literal
REM `)` in a %-expanded value would close this block at parse time.
if "!_HOMIE_DRY_RUN!"=="1" (
    echo DRY RUN: no spawn
    echo PYTHON: !VENV_PYTHON!
    echo MAIN_PY: !SCRIPT_DIR!main.py
    echo PID_FILE: !PID_FILE!
    echo LOG_FILE: !LOG_FILE!
    exit /b 0
)

cd /d "%SCRIPTS_DIR%"

set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

if "%1"=="--fg" (
    "%VENV_PYTHON%" "%SCRIPT_DIR%main.py" --telegram %2 %3 %4
) else (
    REM Delayed-expansion form so the values set at lines 35-36 (inside the
    REM FOR loop) resolve correctly here without needing to escape the
    REM parenthesized ELSE.
    if not exist "!LOG_DIR!" mkdir "!LOG_DIR!" >nul 2>&1
    start /b "" "%VENV_PYTHON%" "%SCRIPT_DIR%main.py" --telegram %* > "!LOG_FILE!" 2>&1
    echo Telegram bot started. Logs: !LOG_FILE!
    echo PID file: !PID_FILE!
)
endlocal
