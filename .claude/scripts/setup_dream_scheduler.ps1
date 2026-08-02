# Setup Windows Task Scheduler for The Homie Dream Cycle (nightly, ~3 AM)
# Run this script as Administrator.
#
# Promotes memory_dream.py from Sunday-only (chained via memory_weekly.py) to a
# real nightly cadence (Living Self Act 4 autonomy — issue #170). Belief
# evolution (Phase 5) rides this SAME cadence, gated by
# HOMIE_KILLSWITCH_BELIEF_AUTONOMY (ships enabled) and EVOLVE_ENABLED.

$TaskName = "SecondBrain-Dream"
$TaskPath = Join-Path $PSScriptRoot "run_dream.bat"
$Description = "The Homie - Nightly dream cycle (consolidate/prune/belief-evolve)"

# Check if task already exists (idempotent re-register, matching autostart.py Rule 2)
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Task '$TaskName' already exists. Removing old task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Create the action
$action = New-ScheduledTaskAction `
    -Execute $TaskPath `
    -WorkingDirectory $PSScriptRoot

# Create trigger - daily at 3 AM
$trigger = New-ScheduledTaskTrigger `
    -Daily `
    -At "03:00"

# Create settings. 30-minute limit (matches setup_evolve_scheduler.ps1) — an LLM
# consolidate + prune + belief-evolve chain needs more headroom than reflection's
# 10-minute limit.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
# -MultipleInstances IgnoreNew (Kimi gate MAJOR on PR #181): the Task
# Scheduler must never launch a 2nd concurrent dream (e.g. a StartWhenAvailable
# catch-up overlapping the 3AM run) — concurrent runs would each read the same
# retry attempts=N and both write N+1 (budget undercount) and each adopt up to
# max_adoptions_per_night (2x throttle escape on identity files). run_dream's
# file_lock(DREAM_STATE_FILE) covers the manual+scheduled race; this covers the
# scheduler-vs-scheduler race before a process even starts.

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
Write-Host "To disable: Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove: Unregister-ScheduledTask -TaskName '$TaskName'"
