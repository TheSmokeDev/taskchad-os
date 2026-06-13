# Setup Windows Task Scheduler for The Homie Evolve Loop (Living Self Act 4)
# Run this script as Administrator.
#
# The scheduled cadence runs the SAFE recall `propose` (no identity mutation) —
# it proves the test-and-keep machinery runs on a cadence and writes a decision
# artifact. The identity rail (`propose-belief`, the LLM judge) is Archon-driven
# (operator-/Archon-triggered) so the judge's provider cost is gated behind a
# deliberate run, NOT the bare cron.

$TaskName = "SecondBrain-Evolve"
$TaskPath = Join-Path $PSScriptRoot "run_evolve.bat"
$Description = "The Homie - Evolve loop (recall safe-first propose) weekly"

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

# Create trigger - weekly, Sunday 9 PM (after the weekly synthesis + dream at 8 PM)
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Sunday `
    -At 9pm

# Create settings
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Create principal (run as current user, limited)
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
