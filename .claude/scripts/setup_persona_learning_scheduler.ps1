# Setup Windows Task Scheduler for The Homie Persona Learning Tick
# Run this script as the operator user (no admin required)
#
# The tick (persona_learning_tick.py) fans out one memory_reflect.py -p <name>
# subprocess per learning-enabled profile. It carries its own per-persona
# recency guard (tick_interval_hours, default 12h) and a PERSONA_REFLECT_SILENT
# fast-path when a persona has no new attributed turns — so the twice-daily
# trigger is safe and cheap (background model tiers, never the interactive
# flagship model).

$TaskName = "SecondBrain-PersonaLearning"
$TaskPath = Join-Path $PSScriptRoot "run_persona_learning.bat"
$Description = "The Homie - Persona learning tick (per-persona belief extraction) at 9:30 AM + 9:30 PM"

# Check if task already exists
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Task '$TaskName' already exists. Removing old task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Create the action
$action = New-ScheduledTaskAction `
    -Execute $TaskPath `
    -WorkingDirectory $PSScriptRoot

# Create triggers - twice daily (the tick's 12h recency guard makes
# double-fire idempotent per persona)
$triggerMorning = New-ScheduledTaskTrigger `
    -Daily `
    -At "09:30"
$triggerEvening = New-ScheduledTaskTrigger `
    -Daily `
    -At "21:30"

# Create settings - 60 min limit (fan-out across all enabled profiles)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60)

# Create principal (run as current user)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Register the task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggerMorning, $triggerEvening `
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
