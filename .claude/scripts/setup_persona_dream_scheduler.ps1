# Setup Windows Task Scheduler for The Homie Persona Dream Tick (nightly, ~3:30 AM)
# Run this script as the operator user (no admin required).
#
# The equality doctrine made real: the tick (persona_dream_tick.py) fans the FULL
# 5-phase dream cycle out over EVERY named profile by spawning memory_dream.py
# -p <name> once each. Belief evolution (Phase 5) rides along per persona, gated
# by HOMIE_KILLSWITCH_BELIEF_AUTONOMY exactly as it is for the main homie.
#
# Staggered 30 minutes after SecondBrain-Dream (03:00) so the main dream and the
# persona fan-out never contend for the runtime. Spend is bounded by SIGNAL, not
# roster size: each child exits DREAM_SILENT with zero LLM calls when its own
# vault has nothing new.

$TaskName = "SecondBrain-PersonaDream"
$TaskPath = Join-Path $PSScriptRoot "run_persona_dream.bat"
$Description = "The Homie - Nightly per-persona dream cycle fan-out (consolidate/prune/belief-evolve per profile)"

# Check if task already exists (idempotent re-register, matching setup_dream_scheduler.ps1)
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Task '$TaskName' already exists. Removing old task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Create the action
$action = New-ScheduledTaskAction `
    -Execute $TaskPath `
    -WorkingDirectory $PSScriptRoot

# Create trigger - daily at 3:30 AM (30 min after the main dream)
$trigger = New-ScheduledTaskTrigger `
    -Daily `
    -At "03:30"

# Create settings. 8-hour limit: the fan-out is SERIAL across every named
# profile and the doctrine is EVERY persona EVERY night, so the ceiling has to
# clear the worst case rather than truncate it — a 28-persona roster at the
# per-child PERSONA_DREAM_TIMEOUT (900s) is 7 hours if every single child runs
# to its timeout. A real night is minutes: children with no fresh signal exit
# DREAM_SILENT in seconds with zero LLM calls. This is a backstop against a
# wedged run, not a budget; the tick's own PERSONA_DREAM_MAX_WALL_CLOCK
# defaults to unlimited for the same reason.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)
# -MultipleInstances IgnoreNew: same reasoning as the main dream task. A
# StartWhenAvailable catch-up overlapping the 03:30 run would spawn a SECOND
# fan-out, and two concurrent children for the same persona would each read the
# same retry attempts=N and both write N+1 (budget undercount) while each adopts
# up to max_adoptions_per_night (2x throttle escape on that persona's SELF.md).
# The child's own file_lock(DREAM_STATE_FILE) covers the manual-vs-scheduled
# race per persona; this covers scheduler-vs-scheduler before a process starts.

# Create principal (run as current user)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Register the task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description $Description

Write-Host ""
Write-Host "Task '$TaskName' created successfully!"
Write-Host ""
Write-Host "To verify: Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "To run now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To dry-run first: uv run python persona_dream_tick.py --test"
Write-Host "To prove the fan-out end to end without writing anything: uv run python persona_dream_tick.py --child-test"
Write-Host "To disable: Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove: Unregister-ScheduledTask -TaskName '$TaskName'"
